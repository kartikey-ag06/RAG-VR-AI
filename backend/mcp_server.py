"""
mcp_server.py - your documents as an MCP tool server, so other AI assistants (Claude, Meta AI connectors,
IDE agents...) can search them directly.

Transport: MCP "Streamable HTTP" at POST /mcp (JSON-RPC 2.0, JSON responses).
Tools: list_documents, search_documents, get_page, get_page_image, ask_documents.
Answers come from the same pipeline as /api/v1 (api_v1.py), so Meta AI, Claude and the glasses all get the same answer.
PUBLIC_URL (environment, e.g. https://vr-rag-backend.onrender.com) makes page-image links absolute.
Security: set MCP_TOKEN (environment) and clients must send  Authorization: Bearer <MCP_TOKEN>.
Without MCP_TOKEN the endpoint only answers requests from this computer.
"""
from net import HTTP, JSONB
import base64
import json
import os
import secrets

from fastapi import Request
from core.sdk.context import PolicyDenied
from fastapi.responses import JSONResponse, Response

VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]

TOOLS = [
    {"name": "list_documents", "description": "List the documents in the VR_RAG library (title, pages, machine ids).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "search_documents",
     "description": "Hybrid (keyword + semantic) search in the VR_RAG documents. Returns the best passages with document and page.",
     "inputSchema": {"type": "object", "required": ["query"], "properties": {
         "query": {"type": "string", "description": "What to look for"},
         "k": {"type": "integer", "description": "How many passages (1-20)", "default": 6},
         "machine_id": {"type": "string", "description": "Only documents tagged with this machine id / QR code"}}}},
    {"name": "get_page", "description": "Full text of one page of a document.",
     "inputSchema": {"type": "object", "required": ["doc_uid", "page"], "properties": {
         "doc_uid": {"type": "string"}, "page": {"type": "integer"}}}},
    {"name": "get_page_image",
     "description": "Picture of one document page (JPEG, sized for small glasses displays) plus its link.",
     "inputSchema": {"type": "object", "required": ["doc_uid", "page"], "properties": {
         "doc_uid": {"type": "string"}, "page": {"type": "integer"},
         "width": {"type": "integer", "description": "Image width in pixels (200-1600). Default 600."}}}},
    {"name": "ask_documents",
     "description": "Answer a question from the VR_RAG documents, with citations [n] to document pages. "
                    "Use style 'spoken' when the answer will be read aloud on glasses.",
     "inputSchema": {"type": "object", "required": ["question"], "properties": {
         "question": {"type": "string"}, "machine_id": {"type": "string"},
         "style": {"type": "string", "description": "full (default), glance (3 short points) or spoken (1-2 sentences)"},
         "lang": {"type": "string", "description": "Answer language, e.g. Hindi. Default English."}}}},
]
for _t in TOOLS:                                     # every tool only reads: lets clients skip confirmation prompts
    _t["annotations"] = {"readOnlyHint": True, "openWorldHint": False}


def register(app, store, search, llm, auth, core=None, identity=None, kernel=None):
    """`kernel` (core.plugins.Kernel): tools contributed by capabilities and packs (Phase 5). A module's tools are
    listed and callable only while that module is switched ON for the caller's tenant."""

    def module_tools():
        if kernel is None:
            return {}
        return {n: v for n, v in sorted(kernel.tools.items()) if kernel.modules.enabled(v[0])}

    def tool_spec(t):
        return {"name": t.name, "description": t.description, "inputSchema": t.input_schema,
                "annotations": {"readOnlyHint": t.read_only, "openWorldHint": False}}

    def key_principal(req):
        """An API key (Bearer vrk_...) -> its principal; None when the caller does not send one.
        A wrong or revoked key raises PolicyDenied (401)."""
        h = req.headers.get("authorization", "")
        if identity is None or not identity.keys.looks_like_key(h[7:].strip() if h.startswith("Bearer ") else ""):
            return None
        return identity.resolve(req)[0]

    def allowed(req):
        tok = os.environ.get("MCP_TOKEN", "")
        if tok:
            h = req.headers.get("authorization", "")
            return h.startswith("Bearer ") and secrets.compare_digest(h[7:].strip(), tok)
        host = req.client.host if req.client else ""
        return host in ("127.0.0.1", "::1", "localhost", "testclient")

    def passages(query, k, machine):
        res = search(query, max(1, min(20, int(k or 6))), machine or "", None)
        out = []
        for h in res["hits"]:
            out.append({"doc_uid": h["uid"], "title": h["title"], "page": h["page"], "text": h["text"],
                        "score": round(h["score"], 4), "semantic": None if h.get("sem") is None else round(h["sem"], 3)})
        return out, res

    def shown(e):
        """MCP callers see public documents of the CURRENT TENANT only (no groups, same tenant - Phase 4)."""
        return bool(e) and not e.get("groups") and auth.can_see_doc(e, None)

    def call_tool(name, a, base="", req=None):
        extra = module_tools()
        if name in extra:
            return extra[name][2](req, a)
        if name == "list_documents":
            docs = [{"doc_uid": d["uid"], "title": d["title"], "pages": d["pages"], "type": d["type"], "machine_ids": d.get("tags", [])}
                    for d in store.snapshot["index"]["docs"] if shown(d)]
            return json.dumps(docs, ensure_ascii=False, indent=1)
        if name == "search_documents":
            ps, res = passages(a.get("query", ""), a.get("k", 6), a.get("machine_id"))
            return json.dumps({"mode": res.get("mode"), "confidence": res.get("conf"), "passages": ps}, ensure_ascii=False, indent=1)
        if name == "get_page":
            e = store.doc_entry(str(a.get("doc_uid", "")))
            if not shown(e):
                raise ValueError("document not found")
            t = store.page_text(e["uid"], int(a.get("page", 1)))
            if t is None:
                raise ValueError("page not found")
            return f"{e['title']} - page {a.get('page')}\n\n{t}"
        if name == "get_page_image":
            uid, page = str(a.get("doc_uid", "")), int(a.get("page", 1))
            e = store.doc_entry(uid)
            if not shown(e):
                raise ValueError("document not found")
            p = store.page_image_uid(uid, page)
            if not p:
                raise ValueError("page not found")
            w = max(200, min(1600, int(a.get("width") or 600)))
            from io import BytesIO
            from PIL import Image
            with Image.open(p) as im:
                im = im.convert("RGB")
                if im.width > w:
                    im = im.resize((w, max(1, round(im.height * w / im.width))), Image.LANCZOS)
                buf = BytesIO()
                im.save(buf, "JPEG", quality=80, optimize=True)
            link = f"{base}/api/v1/documents/{uid}/pages/{page}.jpg?w={w}"
            return [{"type": "text", "text": f"{e['title']} - page {page}\n{link}"},
                    {"type": "image", "data": base64.b64encode(buf.getvalue()).decode(), "mimeType": "image/jpeg"}]
        if name == "ask_documents" and core:
            q = str(a.get("question", "")).strip()[:1000]
            if not q:
                raise ValueError("question is required")
            style = str(a.get("style") or "full").lower()
            out = core["answer"](q, a.get("machine_id"), style if style in ("full", "glance", "spoken") else "full",
                                 str(a.get("lang") or "")[:30], None, door="mcp")
            refs = "\n".join(f"[{c['n']}] {c['title']}, page {c['page']} (doc_uid {c['doc_uid']}) {base}{c['image_url']}"
                             for c in out["citations"])
            label = {"pdf": "From the documents", "general": "General knowledge (not in the documents)",
                     "offline": "Best matching text (no AI on the server)"}.get(out["mode"], out["mode"])
            return f"{out['answer']}\n\n{label}" + (f"\nSources:\n{refs}" if refs else "")
        if name == "ask_documents":
            ps, res = passages(a.get("question", ""), 6, a.get("machine_id"))
            if not ps:
                return "No matching passages in the documents."
            ex = "".join(f"[{i + 1}] ({p['title']}, page {p['page']})\n{p['text']}\n\n" for i, p in enumerate(ps))
            ans = llm([{"role": "system", "content": "Answer only from the numbered excerpts and cite them like [1]. "
                        "If they do not contain the answer, say so. Max 150 words."},
                       {"role": "user", "content": f"{ex}Question: {a.get('question')}"}], 1200)
            refs = "\n".join(f"[{i + 1}] {p['title']}, page {p['page']} (doc_uid {p['doc_uid']})" for i, p in enumerate(ps))
            return f"{ans.strip()}\n\nSources:\n{refs}"
        raise ValueError(f"unknown tool {name}")

    def handle(msg, base="", can_ask=True, req=None):
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        if mid is None:                                      # notification (e.g. notifications/initialized)
            return None
        def ok(result):
            return {"jsonrpc": "2.0", "id": mid, "result": result}
        if method == "initialize":
            v = params.get("protocolVersion")
            return ok({"protocolVersion": v if v in VERSIONS else VERSIONS[0],
                       "capabilities": {"tools": {"listChanged": False}},
                       "serverInfo": {"name": "vr-rag", "version": "1.1"},
                       "instructions": "Search and cite the VR_RAG document library (manuals, SOPs, orders)."})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": TOOLS + [tool_spec(v[1]) for v in module_tools().values()]})
        if method == "tools/call":
            try:
                if params.get("name") == "ask_documents" and not can_ask:
                    raise ValueError("this API key lacks the scope 'ai:ask'")
                out = call_tool(params.get("name"), params.get("arguments") or {}, base, req)
                content = out if isinstance(out, list) else [{"type": "text", "text": out}]
                return ok({"content": content, "isError": False})
            except Exception as e:
                return ok({"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}

    @app.post("/mcp")
    def mcp(req: Request, _body=JSONB):
        try:
            key = key_principal(req)
        except PolicyDenied:
            return JSONResponse({"error": "unauthorized"}, 401)
        if key is not None and not key.has("knowledge:read"):
            return JSONResponse({"error": "this API key lacks the scope 'knowledge:read'"}, 403)
        if key is None and not allowed(req):
            return JSONResponse({"error": "unauthorized"}, 401)
        can_ask = key is None or key.has("ai:ask")
        body = _body
        if body is None:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
        base = (os.environ.get("PUBLIC_URL") or str(req.base_url)).rstrip("/")
        if isinstance(body, list):
            out = [r for r in (handle(m, base, can_ask, req) for m in body) if r is not None]
            return JSONResponse(out) if out else Response(status_code=202)
        r = handle(body, base, can_ask, req)
        if r is None:
            return Response(status_code=202)
        headers = {}
        if body.get("method") == "initialize":
            headers["Mcp-Session-Id"] = secrets.token_hex(12)
        return JSONResponse(r, headers=headers)

    @app.get("/mcp")
    def mcp_get():
        return Response(status_code=405, headers={"Allow": "POST"})

    @app.delete("/mcp")
    def mcp_delete():
        return Response(status_code=200)
