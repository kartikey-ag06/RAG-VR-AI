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

from core.data.tenancy import current_tenant
from core.sdk.context import BudgetExceeded

PathLike = Union[Path, Callable[[], Path]]

GROQ = {"name": "groq", "base": "https://api.groq.com/openai/v1"}
XAI = {"name": "grok", "base": "https://api.x.ai/v1"}
CHAT_PREFERENCE = ["llama-3.3-70b-versatile", "openai/gpt-oss-120b", "openai/gpt-oss-20b", "llama-3.1-8b-instant"]
FAST_PREFERENCE = ["openai/gpt-oss-20b", "llama-3.1-8b-instant"]
STT_PREFERENCE = ["whisper-large-v3-turbo", "whisper-large-v3"]
XAI_CHAT_MODEL = "grok-4.7"
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
                piece = (json.loads(data)["choices"][0].get("delta") or {}).get("content") or ""
            except Exception:
                continue
            if piece:
                yield piece
        self.complete = True


class AIGateway:
    def __init__(self, http, env: Mapping[str, str], key_file: PathLike, model_file: PathLike,
                 log: Optional[logging.Logger] = None, config=None):
        self.http, self.env, self.key_file, self.model_file = http, env, key_file, model_file
        self.config = config                                  # ConfigStore: ai.chat_model, limits.ai_daily (Phase 4)
        self._spent: dict = {}                                # (tenant, UTC day) -> provider calls made
        self._spent_lock = threading.Lock()
        self.log = log or logging.getLogger("vr_rag.ai")
        self.models_cache = {"ids": None, "t": 0.0}           # provider model list, refreshed every 10 minutes
        self.model_choice = {"chat": None, "stt": None}       # chosen models (and "fast")
        self.answers: OrderedDict = OrderedDict()             # same messages + same model -> instant answer (LRU)
        self._inflight: dict = {}
        self._inflight_lock = threading.Lock()

    # ------------------------------------------------------------ key + provider
    def get_key(self) -> Optional[str]:
        for name in ("XAI_API_KEY", "GROQ_API_KEY"):
            if self.env.get(name):
                return self.env[name].strip()
        f = _path(self.key_file)
        if f.exists():
            line = f.read_text(encoding="utf-8-sig").splitlines()[:1]
            if line and re.match(r"^(xai-|gsk_)", line[0].strip()):
                return line[0].strip()
        return None

    def provider(self) -> Optional[dict]:
        k = self.get_key()
        if not k:
            return None
        return dict(GROQ) if k.startswith("gsk_") else dict(XAI)

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.get_key()}"}

    # ------------------------------------------------------------ models
    def list_models(self) -> list:
        """The account's model list, fetched once per 10 minutes (chat, voice and photo all need it)."""
        c = self.models_cache
        if c["ids"] is not None and time.time() - c["t"] < MODEL_LIST_TTL:
            return c["ids"]
        p = self.provider()
        if not p:
            raise RuntimeError("no AI key configured (set GROQ_API_KEY or put a key in grok_api_key.txt)")
        r = self.http.get(f"{p['base']}/models", headers=self._auth(), timeout=20)
        r.raise_for_status()
        c.update(ids=[m["id"] for m in r.json().get("data", [])], t=time.time())
        return c["ids"]

    def chat_model(self) -> str:
        f = _path(self.model_file)
        if f.exists() and f.read_text().strip():
            return f.read_text().strip().splitlines()[0]
        if self.config is not None and self.config.get("ai.chat_model"):
            return self.config.get("ai.chat_model")            # set per tenant in the config store
        p = self.provider()
        if not p:
            return ""
        if p["name"] == "grok":
            return XAI_CHAT_MODEL
        if not self.model_choice["chat"]:
            try:
                ids = self.list_models()
                self.model_choice["chat"] = next((m for m in CHAT_PREFERENCE if m in ids), None) or next(
                    (m for m in ids if not re.search("whisper|tts|guard|embed", m)), CHAT_PREFERENCE[0])
            except Exception:
                return CHAT_PREFERENCE[0]
        return self.model_choice["chat"]

    def stt_model(self) -> str:
        if not self.model_choice["stt"]:
            try:
                ids = self.list_models()
                self.model_choice["stt"] = next((m for m in STT_PREFERENCE if m in ids), None) or next(
                    (m for m in ids if "whisper" in m), STT_PREFERENCE[0])
            except Exception:
                return STT_PREFERENCE[0]
        return self.model_choice["stt"]

    def fast_model(self) -> str:
        """A smaller, faster model for quick jobs (voice-command routing, translation): ~3x faster than the main one."""
        if self.model_choice.get("fast") is None:
            try:
                ids = self.list_models()
                self.model_choice["fast"] = next((m for m in FAST_PREFERENCE if m in ids), "") or ""
            except Exception:
                return self.chat_model()
        return self.model_choice["fast"] or self.chat_model()

    def chat_payload(self, messages, max_tokens=700, stream=False, model=None) -> dict:
        model = model or self.chat_model()
        payload = {"model": model, "messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
        if "gpt-oss" in model:
            payload["reasoning_effort"] = "low"    # short hidden "thinking": ~2x faster first word, same answers
        if stream:
            payload["stream"] = True
        return payload

    # ------------------------------------------------------------ answer cache + single flight
    def cache_key(self, messages) -> str:
        return hashlib.sha1(json.dumps([self.chat_model(), messages], sort_keys=True).encode()).hexdigest()

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
        (config key limits.ai_daily, 0 = no limit) is reached. Answers from the cache cost nothing."""
        if self.config is None:
            return
        limit = int(self.config.get("limits.ai_daily") or 0)
        if limit <= 0:
            return
        key = (current_tenant(), time.strftime("%Y-%m-%d", time.gmtime()))
        with self._spent_lock:
            if self._spent.get(key, 0) >= limit:
                raise BudgetExceeded(f"this organisation's daily AI limit ({limit} calls) is used up - "
                                     "it resets at 00:00 UTC")
            self._spent[key] = self._spent.get(key, 0) + 1
            for old in [k for k in self._spent if k[1] != key[1]]:      # forget earlier days
                self._spent.pop(old, None)

    # ------------------------------------------------------------ provider calls
    def post_chat(self, messages, max_tokens=700, stream=False, model=None, timeout=90):
        """Raw chat call; returns the HTTP response (caller maps status codes). Raises on network errors,
        BudgetExceeded when the tenant's daily AI limit is used up."""
        self.spend()
        p = self.provider()
        extra = {"stream": True} if stream else {}
        return self.http.post(f"{p['base']}/chat/completions", json=self.chat_payload(messages, max_tokens, stream, model),
                              headers=self._auth(), timeout=timeout, **extra)

    def complete(self, messages, max_tokens=1200, fast=False) -> str:
        """Server-side chat call used by feature modules and /api/v1. Returns the reply text or raises."""
        p = self.provider()
        if not p:
            raise RuntimeError("no API key in grok_api_key.txt")
        r = self.post_chat(messages, max_tokens, model=self.fast_model() if fast else None, timeout=120)
        if r.status_code != 200:
            self.log.warning("AI provider HTTP %s: %s", r.status_code, r.text[:300])
            raise RuntimeError("the AI provider is busy - try again in a minute" if r.status_code == 429
                               else f"{p['name']} API error (HTTP {r.status_code})")
        return r.json()["choices"][0]["message"]["content"] or ""

    def stream(self, messages, max_tokens=700) -> Iterator[str]:
        """Server-side streamed chat call (used by /api/v1/ask/stream). Yields text pieces as they arrive."""
        p = self.provider()
        if not p:
            raise RuntimeError("no API key in grok_api_key.txt")
        r = self.post_chat(messages, max_tokens, stream=True, timeout=90)
        if r.status_code != 200:
            txt = r.text[:300]
            r.close()
            raise RuntimeError(f"{p['name']} API: HTTP {r.status_code} - {txt}")
        try:
            yield from SseReader(r)
        finally:
            r.close()

    def transcribe(self, audio: bytes, mime: str, ext: str, lang: str = "", hint: str = "", timeout=60):
        """Raw speech-to-text call; returns the HTTP response. Raises on network errors / BudgetExceeded."""
        self.spend()
        p = self.provider()
        data = dict({"model": self.stt_model(), "response_format": "json", "temperature": "0"},
                    **({"language": lang} if lang else {}), **({"prompt": hint} if hint else {}))
        return self.http.post(f"{p['base']}/audio/transcriptions", headers=self._auth(),
                              files={"file": (f"speech.{ext}", audio, mime)}, data=data, timeout=timeout)
