"""CoreContext - the ONLY way a capability or pack reaches the Core (ADR 0003).

This file defines the interface (what you can call), not the implementation. The Core builds one CoreContext per
module at start-up (Phase 5) and passes it to `register(ctx)`.

Every method that takes `request` resolves the caller's tenant and principal from it and runs the policy gate:
    tenant resolved -> module enabled for that tenant -> scope allowed -> AI budget left -> audit
If the gate says no, the method raises PolicyDenied (the Core turns it into 401 / 403 / 429).

Example pack (illustrative):

    from core.sdk import CoreContext

    def register(ctx: CoreContext):
        @ctx.router.post("/checklists")                 # -> /api/v1/packs/<id>/checklists
        def make_checklist(request: Request, body: dict):
            ctx.require(request, "ai:ask")
            hits = ctx.search(request, body["topic"], k=6)
            text = ctx.llm.complete(request, prompt="checklist", variables={"topic": body["topic"], "hits": hits})
            ctx.kv.put(request, "checklist", new_id(), {"topic": body["topic"], "text": text})
            return {"text": text}

        @ctx.events.subscribe("question.asked")
        def on_question(event):
            ...                                         # runs with event.tenant_id; no request needed
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Optional, Protocol, runtime_checkable

from .events import Event


# ---------------------------------------------------------------- errors
class SdkError(Exception):
    """Base class for errors raised through the SDK."""


class PolicyDenied(SdkError):
    """The policy gate refused the call. `status` is the HTTP status the Core will answer with."""

    def __init__(self, reason: str, status: int = 403):
        super().__init__(reason)
        self.reason, self.status = reason, status


class ModuleDisabled(PolicyDenied):
    def __init__(self, module_id: str):
        super().__init__(f"{module_id} is not enabled for this tenant", 404)


class BudgetExceeded(PolicyDenied):
    def __init__(self, reason: str = "AI budget used up - try again later"):
        super().__init__(reason, 429)


# ---------------------------------------------------------------- who is calling
@dataclass(frozen=True)
class Principal:
    """The caller of one request, after the Core has checked the key or token."""
    tenant_id: str
    kind: str                                   # "user" | "service" | "anonymous"
    id: str = ""
    name: str = ""
    groups: tuple = ()                          # document groups (today's RBAC)
    scopes: frozenset = field(default_factory=frozenset)

    def has(self, scope: str) -> bool:
        return scope in self.scopes or "admin" in self.scopes


# ---------------------------------------------------------------- what search and answers return
@dataclass(frozen=True)
class Passage:
    doc_uid: str
    title: str
    page: int
    text: str
    score: float
    image_url: str


@dataclass(frozen=True)
class Answer:
    text: str
    mode: str                                   # "pdf" | "general" | "offline"
    citations: tuple = ()                       # Passages actually cited
    confidence: Optional[float] = None


# ---------------------------------------------------------------- the parts of the context
@runtime_checkable
class Llm(Protocol):
    """Metered AI access. The pack never sees the provider key or picks the provider."""

    def complete(self, request, *, prompt: str, variables: dict, max_tokens: int = 700) -> str:
        """`prompt` = a prompt id from the Core prompt registry (e.g. 'answer', or one the module declared).
        Client-supplied system prompts are not accepted (ADR 0004)."""

    def stream(self, request, *, prompt: str, variables: dict, max_tokens: int = 700) -> Iterator[str]:
        ...


@runtime_checkable
class Kv(Protocol):
    """Small JSON records, automatically namespaced to (tenant, this module). Another module's records are
    unreachable; to change them, use ctx.call() on a function that module publishes."""

    def get(self, request_or_tenant, kind: str, id: str) -> Optional[dict]: ...
    def put(self, request_or_tenant, kind: str, id: str, data: dict) -> None: ...
    def list(self, request_or_tenant, kind: str, limit: int = 100) -> list: ...
    def delete(self, request_or_tenant, kind: str, id: str) -> None: ...


@runtime_checkable
class Config(Protocol):
    """Read-only view of config, resolved: manifest default < global < tenant < tenant + module."""

    def get(self, request_or_tenant, key: str, default: Any = None) -> Any: ...


@runtime_checkable
class Events(Protocol):
    def publish(self, request_or_tenant, name: str, data: dict) -> Event:
        """Only names declared in the manifest's `publishes` are allowed."""

    def subscribe(self, name: str) -> Callable[[Callable[[Event], None]], Callable[[Event], None]]:
        """Decorator. Only names declared in the manifest's `subscribes` are allowed."""


@runtime_checkable
class CoreContext(Protocol):
    module_id: str
    sdk_version: str
    router: Any                                 # a FastAPI APIRouter mounted at /api/v1/packs/<module_id>
    llm: Llm
    kv: Kv
    config: Config
    events: Events
    log: Any                                    # a logging.Logger named "vr_rag.<module_id>"

    def principal(self, request) -> Principal:
        """Who is calling (tenant, user or service, scopes). Raises PolicyDenied(401) if the key is invalid."""

    def require(self, request, scope: str) -> Principal:
        """Raise PolicyDenied(403) unless the caller holds `scope` and this module may use it."""

    def search(self, request, query: str, *, k: int = 6, filters: Optional[dict] = None) -> list:
        """Hybrid search inside the documents this caller and this module may see -> [Passage].
        `filters` = metadata filters, e.g. {"tags": "PUMP-7731"} (replaces today's machine special case)."""

    def answer(self, request, question: str, *, style: str = "full", lang: str = "",
               filters: Optional[dict] = None) -> Answer:
        """The one RAG pipeline (search -> prompt -> AI -> citations). Emits 'question.asked'."""

    def call(self, request, function: str, **kwargs) -> Any:
        """Call a function another module published, e.g. ctx.call(req, "assistant.create_reminder", text=...).
        The other module must be listed in this module's depends_on and be switched ON for the tenant."""

    # ---- added in SDK 1.1 (Phase 5)
    prompts: Any                                # .register(name, default) / .get(name): '<module_id>.<name>' prompts

    def publish_function(self, name: str) -> Callable:
        """Decorator: let other modules call fn(request, **kwargs) as '<module_id>.<name>'."""

    def mcp_tool(self, name: str) -> Callable:
        """Decorator: the handler fn(request, arguments) -> text for an MCP tool declared in the manifest.
        The tool is listed and callable only while the module is ON for the caller's tenant."""


def iter_scopes(principals: Iterable[Principal]) -> set:
    """Small helper for tests and the admin UI: every scope held by any of the principals."""
    out = set()
    for p in principals:
        out |= set(p.scopes)
    return out
