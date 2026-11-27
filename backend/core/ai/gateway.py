"""AIGateway - the ONE place that talks to the AI provider (ADR 0004).

It owns: finding the API key, choosing the provider (Groq / xAI) and models, the answer cache, "single flight"
(one provider call for identical questions arriving together), and the HTTP calls for chat, streaming chat,
model list and speech-to-text.

Phase 3 moves this code out of app.py unchanged in behaviour. Phase 4 adds the prompt registry, per-tenant budgets
and metering here.

Dependencies are passed in (no imports of old backend modules - see ADR 0001):
    http        a requests.Session-like object with .get / .post   (shared keep-alive pool)
    env         a mapping read at call time (os.environ)            -> GROQ_API_KEY / XAI_API_KEY
    key_file    Path, or a function returning a Path                -> first line holds the key
    model_file  Path, or a function returning a Path                -> optional fixed chat model
"""
import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, Mapping, Optional, Union

import requests

from core.data.tenancy import current_tenant
from core.sdk.context import BudgetExceeded

from .ledger import AILedger
from .providers import Provider, resolve_provider
from .providers.openai_compat import CHAT_PREFERENCE, FAST_PREFERENCE, STT_PREFERENCE, XAI_CHAT_MODEL

PathLike = Union[Path, Callable[[], Path]]

GROQ = {"name": "groq", "base": "https://api.groq.com/openai/v1"}
XAI = {"name": "grok", "base": "https://api.x.ai/v1"}
MODEL_LIST_TTL = 600            # seconds
ANSWER_CACHE_SIZE = 200
MAX_MESSAGES, MAX_PROMPT_CHARS = 24, 60000


def _path(p: PathLike) -> Path:
    return p() if callable(p) else p


def clean_messages(body) -> tuple:
    """Only well-formed chat messages reach the provider: a list of {role, content} strings, bounded in count and
    size. Returns (messages, error)."""
    msgs = (body or {}).get("messages")
    if not isinstance(msgs, list) or not msgs or len(msgs) > MAX_MESSAGES:
        return None, f"'messages' must be a list of 1-{MAX_MESSAGES} chat messages"
    out, total = [], 0
    for m in msgs:
        if not isinstance(m, dict) or m.get("role") not in ("system", "user", "assistant") or not isinstance(m.get("content"), str):
            return None, "each message needs a role (system/user/assistant) and text content"
        total += len(m["content"])
        out.append({"role": m["role"], "content": m["content"]})
    if total > MAX_PROMPT_CHARS:
        return None, f"prompt too long ({total} characters, max {MAX_PROMPT_CHARS})"
    return out, ""


class SseReader:
    """Reads an OpenAI-style server-sent-events chat stream and yields text pieces.
    After iterating, `complete` tells whether the stream ended normally ([DONE] or end of data)."""

    def __init__(self, response):
        self.response, self.complete = response, False
        self.usage: Optional[dict] = None
        response.encoding = "utf-8"      # SSE has no charset: without this, ’ “ ₹ and Hindi text arrive garbled

    def __iter__(self) -> Iterator[str]:
        for line in self.response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                self.complete = True
                return
            try:
                obj = json.loads(data)
                if isinstance(obj.get("usage"), dict):
                    self.usage = obj["usage"]
                choices = obj.get("choices") or []
                piece = (choices[0].get("delta") or {}).get("content") or "" if choices else ""
            except Exception:
                continue
            if piece:
                yield piece
        self.complete = True


class _SpentDict(dict):
    """Keeps legacy AI._spent.clear() in tests synchronized with in-memory AILedger."""

    def __init__(self, ledger: AILedger):
        super().__init__()
        self._ledger = ledger

    def clear(self):
        super().clear()
        if self._ledger.db is None:
            self._ledger.clear_memory()


class AIGateway:
    def __init__(self, http, env: Mapping[str, str], key_file: PathLike, model_file: PathLike,
                 log: Optional[logging.Logger] = None, config=None, ledger: Optional[AILedger] = None):
        self.http, self.env, self.key_file, self.model_file = http, env, key_file, model_file
        self.config = config                                  # ConfigStore: ai.chat_model, limits.ai_daily (Phase 4)
        self.ledger = ledger if ledger is not None else AILedger()
        self._spent: dict = _SpentDict(self.ledger)           # (tenant, UTC day) -> provider calls made
        self._spent_lock = threading.Lock()
        self.log = log or logging.getLogger("vr_rag.ai")
        self.models_cache = {"ids": None, "t": 0.0}           # provider model list, refreshed every 10 minutes
        self.model_choice = {"chat": None, "stt": None}       # chosen models (and "fast")
        self.answers: OrderedDict = OrderedDict()             # same messages + same model -> instant answer (LRU)
        self._inflight: dict = {}
        self._inflight_lock = threading.Lock()

    # ------------------------------------------------------------ key + provider
    def _cfg(self, key: str) -> str:
        if self.config is None or key not in getattr(self.config, "schema", {}):
            return ""
        return str(self.config.get(key) or "").strip()

    def get_key(self, provider_name: Optional[str] = None) -> Optional[str]:
        pname = (self._cfg("ai.provider") if provider_name is None else provider_name).strip().lower()
        if pname == "bedrock":
            tok = (self.env.get("AWS_BEARER_TOKEN_BEDROCK") or "").strip()
            return tok or None
        if pname == "foundry":
            ep = (self.env.get("AZURE_FOUNDRY_ENDPOINT") or self.env.get("AZURE_OPENAI_ENDPOINT") or self._cfg("ai.region") or "").strip()
            if not ep:
                return None
            tok = (self.env.get("AZURE_FOUNDRY_AUTH_TOKEN") or self.env.get("AZURE_OPENAI_AUTH_TOKEN") or "").strip()
            if tok:
                return tok
            if self.env.get("AZURE_FOUNDRY_USE_ENTRA") == "1":
                return "__entra__"
            key = (self.env.get("AZURE_FOUNDRY_API_KEY") or self.env.get("AZURE_OPENAI_API_KEY") or "").strip()
            return key or None
        if pname == "groq":
            tok = (self.env.get("GROQ_API_KEY") or "").strip()
            if tok:
                return tok
            f = _path(self.key_file)
            if f.exists():
                line = f.read_text(encoding="utf-8-sig").splitlines()[:1]
                if line and line[0].strip().startswith("gsk_"):
                    return line[0].strip()
            return None
        if pname == "grok":
            tok = (self.env.get("XAI_API_KEY") or "").strip()
            if tok:
                return tok
            f = _path(self.key_file)
            if f.exists():
                line = f.read_text(encoding="utf-8-sig").splitlines()[:1]
                if line and line[0].strip().startswith("xai-"):
                    return line[0].strip()
            return None
        if pname:
            return None
        for name in ("XAI_API_KEY", "GROQ_API_KEY"):
            if self.env.get(name):
                return self.env[name].strip()
        f = _path(self.key_file)
        if f.exists():
            line = f.read_text(encoding="utf-8-sig").splitlines()[:1]
            if line and re.match(r"^(xai-|gsk_)", line[0].strip()):
                return line[0].strip()
        return None

    def _provider_tuple(self, provider_name: Optional[str] = None, region: Optional[str] = None):
        pname = self._cfg("ai.provider") if provider_name is None else provider_name
        reg = self._cfg("ai.region") if region is None else region
        key = self.get_key(pname)
        p = resolve_provider(key, self.http, pname, reg, self.env)
        eff_reg = getattr(p, "region", reg) or ""
        return p, key, eff_reg

    def active_provider(self) -> Optional[Provider]:
        return self._provider_tuple()[0]

    def supports(self, cap: str) -> bool:
        p = self.active_provider()
        return bool(p and p.supports(cap))

    def provider(self) -> Optional[dict]:
        p = self.active_provider()
        return {"name": p.name, "base": p.base_url} if p else None

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.get_key()}"}

    # ------------------------------------------------------------ models
    def _list_models_for(self, p: Provider, key: str, reg: str) -> list:
        if self.models_cache.get("t", 0.0) == 0.0:
            self.models_cache["scoped"] = {}
        scoped = self.models_cache.setdefault("scoped", {})
        entry = scoped.get((p.name, reg))
        if entry is not None and time.time() - entry["t"] < MODEL_LIST_TTL:
            return entry["ids"]
        ids = p.list_models(key)
        now = time.time()
        scoped[(p.name, reg)] = {"ids": ids, "t": now}
        self.models_cache.update(ids=ids, t=now)
        return ids

    def list_models(self) -> list:
        """The account's model list, fetched once per 10 minutes (chat, voice and photo all need it)."""
        p, key, reg = self._provider_tuple()
        if not p or not key:
            raise RuntimeError("no AI key configured (set GROQ_API_KEY or put a key in grok_api_key.txt)")
        return self._list_models_for(p, key, reg)

    def _chat_model_for(self, p: Optional[Provider], key: Optional[str], reg: str, fast: bool = False) -> str:
        cfg_model = self._cfg("ai.chat_model")
        if cfg_model and not fast:
            return cfg_model                                  # per-tenant config beats local grok_model.txt
        f = _path(self.model_file)
        if f.exists() and f.read_text().strip() and not fast:
            return f.read_text().strip().splitlines()[0]
        if not p or not key:
            return ""
        if p.name == "grok":
            return p.chat_model([])
        scoped = self.model_choice.setdefault("scoped", {})
        bucket = scoped.setdefault((p.name, reg), {})
        slot = "fast" if fast else "chat"
        if slot not in bucket:
            try:
                bucket[slot] = p.chat_model(self._list_models_for(p, key, reg), fast=fast)
            except Exception:
                return "" if fast else CHAT_PREFERENCE[0]
        return bucket[slot]

    def chat_model(self) -> str:
        p, key, reg = self._provider_tuple()
        return self._chat_model_for(p, key, reg, fast=False)

    def stt_model(self) -> str:
        p, key, reg = self._provider_tuple()
        scope_key = (p.name, reg) if p else ("", "")
        scoped = self.model_choice.setdefault("scoped", {})
        bucket = scoped.setdefault(scope_key, {})
        if "stt" not in bucket:
            try:
                ids = self._list_models_for(p, key, reg) if (p and key) else []
                bucket["stt"] = p.stt_model(ids) if hasattr(p, "stt_model") else STT_PREFERENCE[0]
            except Exception:
                return STT_PREFERENCE[0]
        return bucket["stt"]

    def fast_model(self) -> str:
        """A smaller, faster model for quick jobs (voice-command routing, translation): ~3x faster than the main one."""
        p, key, reg = self._provider_tuple()
        return self._chat_model_for(p, key, reg, fast=True) or self._chat_model_for(p, key, reg, fast=False)

    def chat_payload(self, messages, max_tokens=700, stream=False, model=None) -> dict:
        p, key, reg = self._provider_tuple()
        model = model or self._chat_model_for(p, key, reg, fast=False)
        if p:
            return p.chat_payload(messages, model, max_tokens=max_tokens, stream=stream)
        payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
        if stream:
            payload["stream"] = True
        return payload

    # ------------------------------------------------------------ answer cache + single flight
    def cache_key(self, messages) -> str:
        p, key, reg = self._provider_tuple()
        pname = p.name if p else ""
        model = self._chat_model_for(p, key, reg, fast=False)
        return hashlib.sha1(json.dumps([pname, reg, model, messages], sort_keys=True).encode()).hexdigest()

    def cached(self, key: str) -> Optional[str]:
        return self.answers.get(key)

    def remember(self, key: str, text: str) -> None:
        if text and not text.startswith("AI error"):
            self.answers[key] = text
            self.answers.move_to_end(key)
            while len(self.answers) > ANSWER_CACHE_SIZE:
                self.answers.popitem(last=False)

    @contextmanager
    def single_flight(self, key: str, wait: float = 95):
        """When the same question arrives several times at once (double tap, two workers, a retry), only the first
        caller is the 'leader' and calls the provider; the others wait for its answer instead of paying again.
        Yields True for the leader, False for a follower (which should then look in the cache)."""
        with self._inflight_lock:
            waiter = self._inflight.get(key)
            if waiter is None:
                self._inflight[key] = threading.Event()
        if waiter is not None:
            waiter.wait(wait)
            yield False
            return
        try:
            yield True
        finally:
            with self._inflight_lock:
                ev = self._inflight.pop(key, None)
            if ev:
                ev.set()

    # ------------------------------------------------------------ per-tenant daily budget
    def spend(self) -> None:
        """Count one provider call for the current tenant; raise BudgetExceeded when its daily limit
        (limits.ai_daily / ai.daily_cost_usd, 0 = no limit) is reached. Answers from the cache cost nothing."""
        call_limit = 0
        cost_limit = 0.0
        if self.config is not None:
            call_limit = int(self.config.get("limits.ai_daily") or 0)
            if "ai.daily_cost_usd" in getattr(self.config, "schema", {}):
                cost_limit = float(self.config.get("ai.daily_cost_usd") or 0.0)
        tenant = current_tenant()
        day = time.strftime("%Y-%m-%d", time.gmtime())
        key = (tenant, day)
        with self._spent_lock:
            if call_limit > 0 and self._spent.get(key, 0) >= call_limit:
                raise BudgetExceeded(f"this organisation's daily AI limit ({call_limit} calls) is used up - "
                                     "it resets at 00:00 UTC")
            calls = self.ledger.reserve_call(tenant, day, call_limit, cost_limit)
            self._spent[key] = max(self._spent.get(key, 0) + 1, calls)
            for old in [k for k in self._spent if k[1] != day]:         # forget earlier days
                self._spent.pop(old, None)

    # ------------------------------------------------------------ provider calls + failover
    def _provider_chain(self, cap: str) -> list:
        chain, seen = [], set()
        p, k, r = self._provider_tuple()
        if p and k and p.supports(cap):
            chain.append((p, k, r))
            seen.add(p.name)
        for fb in [x.strip().lower() for x in self._cfg("ai.fallback").split(",") if x.strip()]:
            fp, fk, fr = self._provider_tuple(fb, "")
            if fp and fk and fp.name not in seen and fp.supports(cap):
                chain.append((fp, fk, fr))
                seen.add(fp.name)
        return chain

    def post_chat(self, messages, max_tokens=700, stream=False, model=None, timeout=90):
        """Raw chat call; returns the HTTP response (caller maps status codes). Raises on network errors,
        BudgetExceeded when the tenant's daily AI limit is used up."""
        self.spend()
        p, k, reg = self._provider_tuple()
        if not p or not k:
            raise RuntimeError("no API key in grok_api_key.txt")
        eff_model = model or self._chat_model_for(p, k, reg, fast=False)
        payload = p.chat_payload(messages, eff_model, max_tokens=max_tokens, stream=stream)
        return p.post_chat(k, payload, stream=stream, timeout=timeout)

    def complete(self, messages, max_tokens=1200, fast=False) -> str:
        """Server-side chat call with automatic failover on outage (5xx/429/network). Spend counted once."""
        chain = self._provider_chain("chat")
        if not chain:
            raise RuntimeError("no API key in grok_api_key.txt")
        self.spend()
        last_err = None
        for idx, (p, k, reg) in enumerate(chain):
            eff_model = self._chat_model_for(p, k, reg, fast=fast) or self._chat_model_for(p, k, reg, fast=False)
            payload = p.chat_payload(messages, eff_model, max_tokens=max_tokens, stream=False)
            try:
                r = p.post_chat(k, payload, stream=False, timeout=120)
            except requests.RequestException as e:
                last_err = e
                self.log.warning("AI provider %s request failed: %s", p.name, e)
                if idx + 1 < len(chain):
                    continue
                raise
            if r.status_code == 200:
                body = r.json()
                u = body.get("usage") or {}
                self.ledger.record_usage(
                    current_tenant(),
                    provider=p.name,
                    model=eff_model,
                    capability="chat",
                    prompt_tokens=u.get("prompt_tokens", 0),
                    completion_tokens=u.get("completion_tokens", 0),
                )
                return body["choices"][0]["message"]["content"] or ""
            self.log.warning("AI provider HTTP %s: %s", r.status_code, r.text[:300])
            err = RuntimeError("the AI provider is busy - try again in a minute" if r.status_code == 429
                               else f"{p.name} API error (HTTP {r.status_code})")
            if (r.status_code == 429 or r.status_code >= 500) and idx + 1 < len(chain):
                last_err = err
                continue
            raise err
        raise last_err  # pragma: no cover

    def complete_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "auto",
        max_tokens: int = 1200,
    ) -> dict:
        """Chat completion supporting OpenAI-style tools/tool_calls with failover and ledger recording."""
        chain = self._provider_chain("chat")
        if not chain:
            raise RuntimeError("no API key in grok_api_key.txt")
        self.spend()
        last_err = None
        for idx, (p, k, reg) in enumerate(chain):
            eff_model = self._chat_model_for(p, k, reg, fast=False)
            payload = p.chat_payload(
                messages,
                eff_model,
                max_tokens=max_tokens,
                stream=False,
                tools=tools or None,
                tool_choice=tool_choice if tools else None,
            )
            try:
                r = p.post_chat(k, payload, stream=False, timeout=120)
            except requests.RequestException as e:
                last_err = e
                self.log.warning("AI tool provider %s request failed: %s", p.name, e)
                if idx + 1 < len(chain):
                    continue
                raise
            if r.status_code == 200:
                body = r.json()
                u = body.get("usage") or {}
                self.ledger.record_usage(
                    current_tenant(),
                    provider=p.name,
                    model=eff_model,
                    capability="chat",
                    prompt_tokens=u.get("prompt_tokens", 0),
                    completion_tokens=u.get("completion_tokens", 0),
                )
                msg = (body.get("choices") or [{}])[0].get("message") or {}
                return {
                    "content": msg.get("content") or "",
                    "tool_calls": msg.get("tool_calls") or [],
                    "usage": u,
                    "provider": p.name,
                    "model": eff_model,
                }
            self.log.warning("AI provider HTTP %s: %s", r.status_code, r.text[:300])
            err = RuntimeError("the AI provider is busy - try again in a minute" if r.status_code == 429
                               else f"{p.name} API error (HTTP {r.status_code})")
            if (r.status_code == 429 or r.status_code >= 500) and idx + 1 < len(chain):
                last_err = err
                continue
            raise err
        raise last_err  # pragma: no cover

    def stream(self, messages, max_tokens=700) -> Iterator[str]:
        """Server-side streamed chat call with pre-first-chunk failover. Spend counted once."""
        chain = self._provider_chain("stream")
        if not chain:
            raise RuntimeError("no API key in grok_api_key.txt")
        self.spend()
        last_err = None
        for idx, (p, k, reg) in enumerate(chain):
            eff_model = self._chat_model_for(p, k, reg, fast=False)
            payload = p.chat_payload(messages, eff_model, max_tokens=max_tokens, stream=True)
            try:
                r = p.post_chat(k, payload, stream=True, timeout=90)
            except requests.RequestException as e:
                last_err = e
                self.log.warning("AI stream provider %s request failed: %s", p.name, e)
                if idx + 1 < len(chain):
                    continue
                raise
            if r.status_code != 200:
                txt = r.text[:300]
                r.close()
                err = RuntimeError(f"{p.name} API: HTTP {r.status_code} - {txt}")
                if (r.status_code == 429 or r.status_code >= 500) and idx + 1 < len(chain):
                    last_err = err
                    continue
                raise err
            reader = SseReader(r)
            try:
                yield from reader
            finally:
                r.close()
                u = reader.usage or {}
                if reader.complete or u:
                    self.ledger.record_usage(
                        current_tenant(),
                        provider=p.name,
                        model=eff_model,
                        capability="stream",
                        prompt_tokens=u.get("prompt_tokens", 0),
                        completion_tokens=u.get("completion_tokens", 0),
                    )
            return
        raise last_err  # pragma: no cover

    def vision(self, image_data_url: str, *, system: str, user: str, model: str,
               max_tokens: int = 700, timeout: int = 90) -> tuple:
        """Server-side vision call with automatic failover on outage. Spend counted once."""
        chain = self._provider_chain("vision")
        if not chain:
            raise RuntimeError("Photo questions need a Groq key (gsk_...) in grok_api_key.txt")
        if not model:
            raise RuntimeError("No vision model is available on this Groq account")
        self.spend()
        last_err = None
        for idx, (p, k, reg) in enumerate(chain):
            eff_model = model if idx == 0 else (self._chat_model_for(p, k, reg, fast=False) or model)
            try:
                content, used_model = p.vision(k, image_data_url, system=system, user=user, model=eff_model,
                                               max_tokens=max_tokens, timeout=timeout)
                u = getattr(p, "last_usage", None) or {}
                self.ledger.record_usage(
                    current_tenant(),
                    provider=p.name,
                    model=used_model,
                    capability="vision",
                    prompt_tokens=u.get("prompt_tokens", 0),
                    completion_tokens=u.get("completion_tokens", 0),
                )
                return content, used_model
            except (requests.RequestException, RuntimeError) as e:
                msg = str(e)
                is_outage = isinstance(e, requests.RequestException) or "HTTP 5" in msg or "429" in msg or "rate limit" in msg.lower()
                if is_outage and idx + 1 < len(chain):
                    last_err = e
                    self.log.warning("AI vision provider %s failed, failing over: %s", p.name, e)
                    continue
                raise
        raise last_err  # pragma: no cover

    def transcribe(self, audio: bytes, mime: str, ext: str, lang: str = "", hint: str = "", timeout=60):
        """Raw speech-to-text call; returns the HTTP response. Raises on network errors / BudgetExceeded."""
        self.spend()
        p, k, _ = self._provider_tuple()
        if not p or not k:
            raise RuntimeError("no API key in grok_api_key.txt")
        model = self.stt_model()
        resp = p.transcribe(k, audio, mime, ext, model=model, lang=lang, hint=hint, timeout=timeout)
        if getattr(resp, "status_code", 0) == 200:
            self.ledger.record_usage(current_tenant(), provider=p.name, model=model, capability="transcribe")
        return resp
