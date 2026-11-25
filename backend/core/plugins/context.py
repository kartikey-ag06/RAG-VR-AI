"""ModuleContext - the real CoreContext every capability and pack gets in register(ctx) (Phase 5, ADR 0003).

Every call passes the policy gate before it does anything:
    module switched ON for the caller's tenant?  ->  caller holds the scope?  ->  module DECLARED that scope?
and the AI calls also spend the tenant's AI budget (core.ai gateway). A module never sees the AI key, the database,
or another module's records.
"""
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter

from core.data.tenancy import TENANT_ID_RE, current_tenant, use_tenant
from core.sdk.context import Answer, ModuleDisabled, Passage, PolicyDenied, Principal
from core.sdk.events import Event
from core.sdk.manifest import Manifest
from core.sdk.version import SDK_VERSION

LOCAL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
UID = re.compile(r"[0-9a-f]{12}")
PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@dataclass
class Kernel:
    """The Core services the contexts are built on. Filled by the composition root (app.py)."""
    identity: Any
    modules: Any                       # core.modules.ModuleRegistry
    config: Any                        # core.config.ConfigStore
    prompts: Any                       # core.ai.prompts.PromptRegistry
    ai: Any                            # core.ai.AIGateway
    bus: Any                           # core.plugins.EventBus
    kv: Any                            # core.data KeyValueStore
    search: Callable                   # (query, k, tag, user) -> {"hits": [...], ...}
    page_url: Callable = lambda uid, page: ""
    answer: Optional[Callable] = None  # (question, tag, style, lang, user, door) -> dict; set once /api/v1 exists
    functions: Dict[str, tuple] = field(default_factory=dict)    # "module.name" -> (module_id, fn)
    tools: Dict[str, tuple] = field(default_factory=dict)        # MCP tool name -> (module_id, McpTool, handler)
    # ---- SDK 1.2 (Phase 6)
    store: Any = None                  # document store (pages, text, revisions)
    can_see: Callable = lambda entry, user: True                 # tenant + group check for one document
    journal: Any = None                # core.data journal (append-only logs)
    emit: Callable = lambda name, data, tenant_id=None: None     # publish a Core event
    report_kinds: Dict[str, dict] = field(default_factory=dict)  # kind -> {module, label, instructions}
    aliases: Dict[str, tuple] = field(default_factory=dict)      # old prefix -> (module_id, new full prefix)


def _tenant_of(request_or_tenant) -> str:
    if isinstance(request_or_tenant, str):
        if not TENANT_ID_RE.match(request_or_tenant):
            raise ValueError(f"bad tenant id {request_or_tenant!r}")
        return request_or_tenant
    return current_tenant()                      # set for this request by the TenantContext middleware


# ---------------------------------------------------------------- the parts
class _Llm:
    def __init__(self, ctx: "ModuleContext"):
        self.ctx = ctx

    def _messages(self, prompt: str, variables: dict) -> list:
        vars_ = {k: str(v) for k, v in (variables or {}).items()}
        # only {name} placeholders that were passed are filled; JSON examples like {"a": 1} in a prompt stay as they are
        system = PLACEHOLDER.sub(lambda m: vars_.get(m.group(1), m.group(0)), self.ctx.prompts.get(prompt))
        return [{"role": "system", "content": system}, {"role": "user", "content": str((variables or {}).get("input", ""))[:16000]}]

    def complete(self, request, *, prompt: str, variables: dict, max_tokens: int = 700) -> str:
        self.ctx.require(request, "ai:ask")
        return self.ctx.kernel.ai.complete(self._messages(prompt, variables), max(1, min(4000, max_tokens)))

    def stream(self, request, *, prompt: str, variables: dict, max_tokens: int = 700):
        self.ctx.require(request, "ai:ask")
        return self.ctx.kernel.ai.stream(self._messages(prompt, variables), max(1, min(4000, max_tokens)))


class _Prompts:
    """Prompts the module owns, stored in the Core prompt registry as '<module_id>.<name>' (tenant-overridable)."""

    def __init__(self, ctx: "ModuleContext"):
        self.ctx = ctx

    def _id(self, name: str) -> str:
        if not LOCAL_NAME.match(name):
            raise ValueError(f"bad prompt name {name!r}")
        return f"{self.ctx.module_id}.{name}"

    def register(self, name: str, default: str, description: str = "") -> None:
        self.ctx.kernel.prompts.register(self._id(name), default, description)

    def get(self, name: str) -> str:
        pid = self._id(name)
        if pid not in self.ctx.kernel.prompts.defaults:
            raise KeyError(f"prompt {name!r} is not registered by {self.ctx.module_id} (ctx.prompts.register first)")
        return self.ctx.kernel.prompts.get(pid)


class _Kv:
    """Records namespaced to (tenant, module): kind 'report' of module 'x' is stored as 'x.report'."""

    def __init__(self, ctx: "ModuleContext"):
        self.ctx = ctx

    def _kind(self, kind: str) -> str:
        if not LOCAL_NAME.match(kind or ""):
            raise ValueError(f"bad record kind {kind!r}")
        return f"{self.ctx.module_id}.{kind}"

    def _run(self, rot, fn):
        self.ctx.check_enabled(rot)
        with use_tenant(_tenant_of(rot)):
            return fn()

    def get(self, rot, kind, id):
        return self._run(rot, lambda: self.ctx.kernel.kv.kv_get(self._kind(kind), str(id)))

    def put(self, rot, kind, id, data):
        return self._run(rot, lambda: self.ctx.kernel.kv.kv_put(self._kind(kind), str(id), data))

    def list(self, rot, kind, limit=100):
        return self._run(rot, lambda: self.ctx.kernel.kv.kv_list(self._kind(kind))[:max(1, min(1000, limit))])

    def delete(self, rot, kind, id):
        return self._run(rot, lambda: self.ctx.kernel.kv.kv_delete(self._kind(kind), str(id)))


class _Config:
    """Read-only. The module's own keys from its manifest, stored as 'packs.<module_id>.<key>'.
    Resolved: manifest default < global < tenant < tenant + module."""

    def __init__(self, ctx: "ModuleContext"):
        self.ctx = ctx

    def key(self, key: str) -> str:
        return f"packs.{self.ctx.module_id}.{key}"

    def get(self, rot, key: str, default: Any = None) -> Any:
        if key not in self.ctx.manifest.config:
            raise KeyError(f"{self.ctx.module_id} did not declare config key {key!r} in its manifest")
        v = self.ctx.kernel.config.get(self.key(key), _tenant_of(rot), self.ctx.module_id)
        return default if v is None else v


class _Events:
    def __init__(self, ctx: "ModuleContext"):
        self.ctx = ctx

    def publish(self, rot, name: str, data: dict) -> Event:
        if name not in self.ctx.manifest.publishes:
            raise PolicyDenied(f"{self.ctx.module_id} did not declare event {name!r} in 'publishes'", 403)
        self.ctx.check_enabled(rot)
        return self.ctx.kernel.bus.publish(Event(name, _tenant_of(rot), dict(data or {}), source=self.ctx.module_id))

    def subscribe(self, name: str):
        if name not in self.ctx.manifest.subscribes:
            raise ValueError(f"{self.ctx.module_id} did not declare event {name!r} in 'subscribes'")

        def deco(fn):
            self.ctx.kernel.bus.subscribe(name, fn, self.ctx.module_id)
            return fn
        return deco


class _Documents:
    """The documents the CALLER may see (same tenant, same groups), read-only unless the module declared
    'knowledge:write'. Page-level revisions are how a pack fixes a page without re-uploading the file."""

    def __init__(self, ctx: "ModuleContext"):
        self.ctx = ctx

    @property
    def _store(self):
        return self.ctx.kernel.store

    def _visible(self, request, uid, scope="knowledge:read"):
        user = self.ctx._user(request, scope)
        e = self._store.doc_entry(uid) if UID.fullmatch(str(uid or "")) else None
        return e if e and self.ctx.kernel.can_see(e, user) else None

    def list(self, request) -> list:
        user = self.ctx._user(request, "knowledge:read")
        out = []
        for e in self._store.list():
            if e.get("status") == "ready" and self.ctx.kernel.can_see(e, user):
                out.append({k: e.get(k) for k in ("uid", "id", "title", "type", "pages", "tags", "status")})
        return out

    def get(self, request, uid) -> Optional[dict]:
        e = self._visible(request, uid)
        return {k: e.get(k) for k in ("uid", "id", "title", "type", "pages", "tags", "status")} if e else None

    def page_text(self, request, uid, page, original=False) -> Optional[str]:
        return self._store.page_text(uid, int(page), original=original) if self._visible(request, uid) else None

    def page_image(self, request, uid, page, original=False):
        """Path of the page picture (JPEG), or None."""
        return self._store.page_image_uid(uid, int(page), original=original) if self._visible(request, uid) else None

    def revisions(self, request, uid) -> Optional[dict]:
        return self._store.revisions(uid) if self._visible(request, uid) else None

    def index_version(self) -> str:
        return self._store.snapshot["version"]

    def add_revision(self, request, uid, page, text, *, summary="", approver="", evidence=0) -> dict:
        if not self._visible(request, uid, "knowledge:write"):
            raise PolicyDenied("document not found", 404)
        rec = self._store.add_revision(uid, int(page), text, summary=summary, approver=approver, evidence=evidence)
        self.ctx.kernel.emit("document.updated", {"doc_uid": uid, "fields": ["revision"], "page": int(page),
                                                   "source": self.ctx.module_id})
        return rec

    def clear_revisions(self, request, uid=None) -> list:
        """Back to the original pages: one document, or every document of the caller's tenant (never another
        tenant's). Returns the uids that had revisions. Old revisions are archived, not deleted."""
        if uid is not None:
            if not self._visible(request, uid, "knowledge:write"):
                raise PolicyDenied("document not found", 404)
            uids = [uid]
        else:
            self.ctx._user(request, "knowledge:write")
            uids = [d["uid"] for d in self.list(request)]
        done = [u for u in uids if self._store.revisions(u).get("pages") or self._store.revisions(u).get("history")]
        for u in done:
            self._store.clear_revisions(u)
            self.ctx.kernel.emit("document.updated", {"doc_uid": u, "fields": ["revision"], "source": self.ctx.module_id})
        return done


class _Journal:
    """Append-only logs, per tenant, stream '<module_id>.<name>'."""

    def __init__(self, ctx: "ModuleContext"):
        self.ctx = ctx

    def _stream(self, name):
        if not LOCAL_NAME.match(name or ""):
            raise ValueError(f"bad journal name {name!r}")
        return f"{self.ctx.module_id}.{name}"

    def _run(self, rot, fn):
        self.ctx.check_enabled(rot)
        with use_tenant(_tenant_of(rot)):
            return fn()

    def append(self, rot, name, data: dict) -> None:
        self._run(rot, lambda: self.ctx.kernel.journal.append(self._stream(name), dict(data)))

    def list(self, rot, name, limit=None) -> list:
        return self._run(rot, lambda: self.ctx.kernel.journal.list(self._stream(name), limit))

    def archive(self, rot, name) -> None:
        self._run(rot, lambda: self.ctx.kernel.journal.archive(self._stream(name)))


class _Reports:
    """Report templates other capabilities offer (e.g. session reports). Shown only while this module is ON."""

    def __init__(self, ctx: "ModuleContext"):
        self.ctx = ctx

    def register_kind(self, kind: str, label: str, instructions: str = "") -> None:
        if not LOCAL_NAME.match(kind):
            raise ValueError(f"bad report kind {kind!r}")
        owner = self.ctx.kernel.report_kinds.get(kind, {}).get("module")
        if owner and owner != self.ctx.module_id:
            raise ValueError(f"report kind {kind!r} already belongs to {owner}")
        self.ctx.kernel.report_kinds[kind] = {"module": self.ctx.module_id, "label": str(label)[:80],
                                              "instructions": str(instructions)[:2000]}


class LegacyBridge:
    """Old-style dependencies for capabilities that are still moving to the SDK (manifest "legacy": true).
    Packs never get this. Every use of it is migration debt - see ADR 0006."""

    def __init__(self, ctx: "ModuleContext", services: dict, exports: dict):
        self._ctx, self._services, self._exports = ctx, services, exports

    def __getattr__(self, name):
        try:
            return self._services[name]
        except KeyError:
            raise AttributeError(f"legacy service {name!r} does not exist") from None

    def export(self, module_id: str, default=None):
        """What another module's register() returned (the old way modules shared functions)."""
        return self._exports.get(module_id, default)


# ---------------------------------------------------------------- the context
class ModuleContext:
    def __init__(self, manifest: Manifest, kernel: Kernel, legacy: Optional[LegacyBridge] = None):
        self.manifest, self.kernel = manifest, kernel
        self.module_id = manifest.id
        self.sdk_version = SDK_VERSION
        self.router = APIRouter()                     # mounted at /api/v1/packs/<module_id> by the loader
        self.log = logging.getLogger(f"vr_rag.{manifest.id}")
        self.llm, self.kv, self.config = _Llm(self), _Kv(self), _Config(self)
        self.events, self.prompts = _Events(self), _Prompts(self)
        self.documents, self.journal, self.reports = _Documents(self), _Journal(self), _Reports(self)
        self._legacy = legacy

    @property
    def legacy(self) -> LegacyBridge:
        if self._legacy is None:
            raise PolicyDenied(f"{self.module_id} is not a legacy capability (manifest 'legacy': true)", 403)
        return self._legacy

    # ------------------------------------------------------------ policy gate
    def check_enabled(self, request_or_tenant=None) -> None:
        t = current_tenant() if request_or_tenant is None else _tenant_of(request_or_tenant)
        if not self.kernel.modules.enabled(self.module_id, t):
            raise ModuleDisabled(self.module_id)

    def principal(self, request) -> Principal:
        return self.kernel.identity.resolve(request)[0]

    def require(self, request, scope: str) -> Principal:
        self.check_enabled(request)
        m = self.manifest
        if scope not in m.requires_scopes and scope not in m.provides_scopes:
            raise PolicyDenied(f"{self.module_id} did not declare scope {scope!r} in its manifest", 403)
        return self.kernel.identity.require(request, scope)[0]

    def _user(self, request, scope):
        self.require(request, scope)
        return self.kernel.identity.resolve(request)[1]

    # ------------------------------------------------------------ knowledge
    def search(self, request, query: str, *, k: int = 6, filters: Optional[dict] = None) -> list:
        user = self._user(request, "knowledge:read")
        tag = str((filters or {}).get("tags") or "")[:80]
        res = self.kernel.search(str(query)[:500], max(1, min(20, int(k))), tag, user)
        return [Passage(doc_uid=h["uid"], title=h["title"], page=h["page"], text=h["text"], score=float(h["score"]),
                        image_url=self.kernel.page_url(h["uid"], h["page"])) for h in res.get("hits", [])[:k]]

    def answer(self, request, question: str, *, style: str = "full", lang: str = "",
               filters: Optional[dict] = None) -> Answer:
        user = self._user(request, "ai:ask")
        if self.kernel.answer is None:
            raise PolicyDenied("answers are not available on this server", 503)
        out = self.kernel.answer(str(question)[:2000], str((filters or {}).get("tags") or "")[:80], style, lang,
                                 user, self.module_id)
        cites = tuple(Passage(doc_uid=c["doc_uid"], title=c["title"], page=c["page"], text="", score=0.0,
                              image_url=c.get("image_url", "")) for c in out.get("citations", []))
        return Answer(text=out.get("answer", ""), mode=out.get("mode", ""), citations=cites,
                      confidence=out.get("confidence"))

    # ------------------------------------------------------------ functions other modules may call
    def publish_function(self, name: str):
        if not LOCAL_NAME.match(name):
            raise ValueError(f"bad function name {name!r}")

        def deco(fn):
            self.kernel.functions[f"{self.module_id}.{name}"] = (self.module_id, fn)
            return fn
        return deco

    def call(self, request, function: str, **kwargs) -> Any:
        owner, _, _ = function.partition(".")
        if owner not in self.manifest.depends_on:
            raise PolicyDenied(f"{self.module_id} must list {owner!r} in depends_on to call {function!r}", 403)
        if function not in self.kernel.functions:
            raise PolicyDenied(f"no function {function!r}", 404)
        target, fn = self.kernel.functions[function]
        if not self.kernel.modules.enabled(target, _tenant_of(request)):
            raise ModuleDisabled(target)
        return fn(request, **kwargs)

    # ------------------------------------------------------------ MCP tools
    def mcp_tool(self, name: str):
        """Attach the handler for a tool declared in the manifest's mcp_tools: fn(request, arguments) -> text."""
        tool = next((t for t in self.manifest.mcp_tools if t.name == name), None)
        if tool is None:
            raise ValueError(f"{self.module_id} did not declare MCP tool {name!r} in its manifest")
        if name in self.kernel.tools:
            raise ValueError(f"MCP tool {name!r} is already registered")

        def deco(fn):
            self.kernel.tools[name] = (self.module_id, tool, fn)
            return fn
        return deco
