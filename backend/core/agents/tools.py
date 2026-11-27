"""AgentTool registry: Core knowledge tools + active pack tools (Phase 12)."""
import json
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass(frozen=True)
class AgentTool:
    name: str
    description: str
    parameters: dict
    read_only: bool
    required_scope: str
    module_id: Optional[str]
    handler: Callable[[Any, dict], Any]

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }


class ToolRegistry:
    """Builds the per-tenant, per-principal tool list from Core knowledge + enabled packs."""

    def __init__(self, store, run_search, auth, kernel=None):
        self.store = store
        self.run_search = run_search
        self.auth = auth
        self.kernel = kernel
        self._custom: Dict[str, AgentTool] = {}

    def register(self, tool: AgentTool) -> None:
        self._custom[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._custom.pop(name, None)

    def _core_tools(self, user=None) -> List[AgentTool]:
        def list_docs(_req, _args):
            docs = [
                {"doc_uid": d["uid"], "title": d["title"], "pages": d["pages"], "type": d.get("type", "")}
                for d in self.store.snapshot["index"]["docs"]
                if d.get("uid") and self.auth.can_see_doc(d, user)
            ]
            return json.dumps(docs, ensure_ascii=False)

        def search_docs(_req, args):
            q = str((args or {}).get("query") or "").strip()[:500]
            k = max(1, min(10, int((args or {}).get("k") or 5)))
            tag = str((args or {}).get("tag") or "")[:80]
            res = self.run_search(q, k, tag, user)
            hits = [
                {"doc_uid": h["uid"], "title": h["title"], "page": h["page"], "text": h["text"][:500]}
                for h in res.get("hits", [])[:k]
            ]
            return json.dumps({"mode": res.get("mode"), "passages": hits}, ensure_ascii=False)

        def get_page(_req, args):
            uid = str((args or {}).get("doc_uid") or "")
            page = int((args or {}).get("page") or 1)
            e = self.store.doc_entry(uid)
            if not e or not self.auth.can_see_doc(e, user):
                raise ValueError("document not found")
            txt = self.store.page_text(uid, page)
            if txt is None:
                raise ValueError("page not found")
            return f"{e['title']} (p.{page}):\n{txt[:3000]}"

        return [
            AgentTool(
                name="list_documents",
                description="List documents visible to the caller.",
                parameters={"type": "object", "properties": {}},
                read_only=True,
                required_scope="knowledge:read",
                module_id=None,
                handler=list_docs,
            ),
            AgentTool(
                name="search_documents",
                description="Search the caller's documents and return matching passages.",
                parameters={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                        "k": {"type": "integer", "default": 5},
                        "tag": {"type": "string"},
                    },
                },
                read_only=True,
                required_scope="knowledge:read",
                module_id=None,
                handler=search_docs,
            ),
            AgentTool(
                name="get_page",
                description="Read the text of one document page.",
                parameters={
                    "type": "object",
                    "required": ["doc_uid", "page"],
                    "properties": {"doc_uid": {"type": "string"}, "page": {"type": "integer"}},
                },
                read_only=True,
                required_scope="knowledge:read",
                module_id=None,
                handler=get_page,
            ),
        ]

    def _pack_tools(self) -> List[AgentTool]:
        if self.kernel is None:
            return []
        out = []
        for name, (module_id, spec, handler) in sorted(self.kernel.tools.items()):
            req_scope = getattr(spec, "required_scope", None) or (
                "knowledge:read" if spec.read_only else f"{module_id}:approve"
            )
            out.append(
                AgentTool(
                    name=name,
                    description=spec.description,
                    parameters=dict(spec.input_schema or {"type": "object", "properties": {}}),
                    read_only=bool(spec.read_only),
                    required_scope=req_scope,
                    module_id=module_id,
                    handler=handler,
                )
            )
        return out

    def all_tools(self, user=None) -> Dict[str, AgentTool]:
        tools = {t.name: t for t in self._core_tools(user)}
        for t in self._pack_tools():
            tools[t.name] = t
        for name, t in self._custom.items():
            tools[name] = t
        return tools

    def available_for(self, tenant_id: str, principal, user=None) -> List[AgentTool]:
        out = []
        for t in self.all_tools(user).values():
            if t.module_id and self.kernel is not None and not self.kernel.modules.enabled(t.module_id, tenant_id):
                continue
            if not principal.has(t.required_scope):
                continue
            out.append(t)
        return out
