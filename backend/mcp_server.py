"""
mcp_server.py - your documents as an MCP tool server, so other AI assistants (Claude, Meta AI connectors,
IDE agents...) can search them directly.

Transport: MCP "Streamable HTTP" at POST /mcp (JSON-RPC 2.0, JSON responses).
Tools: list_documents, search_documents, get_page, ask_documents.
Security: set MCP_TOKEN (environment) and clients must send  Authorization: Bearer <MCP_TOKEN>.
Without MCP_TOKEN the endpoint only answers requests from this computer.
"""
from net import HTTP, JSONB
import json
import os
import secrets

from fastapi import Request
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
    {"name": "ask_documents",
     "description": "Answer a question from the VR_RAG documents only, with citations [n] to document pages.",
     "inputSchema": {"type": "object", "required": ["question"], "properties": {
         "question": {"type": "string"}, "machine_id": {"type": "string"}}}},
]


def register(app, store, search, llm, auth):
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

    def call_tool(name, a):
        if name == "list_documents":
            docs = [{"doc_uid": d["uid"], "title": d["title"], "pages": d["pages"], "type": d["type"], "machine_ids": d.get("tags", [])}
                    for d in store.snapshot["index"]["docs"] if not d.get("groups")]
            return json.dumps(docs, ensure_ascii=False, indent=1)
        if name == "search_documents":
            ps, res = passages(a.get("query", ""), a.get("k", 6), a.get("machine_id"))
            return json.dumps({"mode": res.get("mode"), "confidence": res.get("conf"), "passages": ps}, ensure_ascii=False, indent=1)
        if name == "get_page":
            e = store.doc_entry(str(a.get("doc_uid", "")))
            if not e or e.get("groups"):
                raise ValueError("document not found")
            t = store.page_text(e["uid"], int(a.get("page", 1)))
            if t is None:
                raise ValueError("page not found")
            return f"{e['title']} - page {a.get('page')}\n\n{t}"
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

    def handle(msg):
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
                       "serverInfo": {"name": "vr-rag", "version": "1.0"},
                       "instructions": "Search and cite the VR_RAG document library (manuals, SOPs, orders)."})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": TOOLS})
        if method == "tools/call":
            try:
                text = call_tool(params.get("name"), params.get("arguments") or {})
                return ok({"content": [{"type": "text", "text": text}], "isError": False})
            except Exception as e:
                return ok({"content": [{"type": "text", "text": f"Error: {e}"}], "isError": True})
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}

    @app.post("/mcp")
    def mcp(req: Request, _body=JSONB):
        if not allowed(req):
            return JSONResponse({"error": "unauthorized"}, 401)
        body = _body
        if body is None:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, 400)
        if isinstance(body, list):
            out = [r for r in (handle(m) for m in body) if r is not None]
            return JSONResponse(out) if out else Response(status_code=202)
        r = handle(body)
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
