"""The real backend with a fake Groq: answers stream slowly and every prompt is written to <out>/last.json.
Used by tests/test_glasses_client.py.  Usage: python fake_groq_server.py <repo_root> <port> <out_dir>"""
import json
import os
import sys
import tempfile
import time

root, port, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
os.environ.update(VR_RAG_DATA_DIR=tempfile.mkdtemp(prefix="vr_rag_e2e_"), VR_RAG_NO_DB="1", GROQ_API_KEY="gsk_fake")
for k in ("ADMIN_TOKEN", "MCP_TOKEN", "RENDER", "DATABASE_URL", "XAI_API_KEY"):
    os.environ.pop(k, None)
sys.path.insert(0, os.path.join(root, "backend"))
import app as backend  # noqa: E402
import uvicorn  # noqa: E402

backend.KEY_FILE = backend.MODEL_FILE = backend.Path(os.environ["VR_RAG_DATA_DIR"]) / "none.txt"
ANSWER = "SOURCE: PDF [2]\n\nThe answer is on page two [2]. More words follow here. And more."


class Resp:
    def __init__(self, status=200, payload=None, lines=None):
        self.status_code, self._p, self._lines, self.encoding, self.text = status, payload, lines or [], None, "{}"

    def json(self):
        return self._p

    def iter_lines(self, decode_unicode=True):
        for line in self._lines:
            time.sleep(0.15)                       # slow enough for the Stop test
            yield line

    def close(self):
        pass


def post(url, json=None, stream=False, **kw):
    with open(os.path.join(out, "last.json"), "w", encoding="utf-8") as f:
        f.write(__import__("json").dumps(json["messages"]))
    if "boom" in json["messages"][1]["content"]:
        return Resp(500)
    if stream:
        parts = [ANSWER[i:i + 12] for i in range(0, len(ANSWER), 12)]
        return Resp(lines=["data: " + __import__("json").dumps({"choices": [{"delta": {"content": p}}]}) for p in parts]
                    + ["data: [DONE]"])
    return Resp(payload={"choices": [{"message": {"content": ANSWER}}]})


backend.HTTP.post = post
backend.HTTP.get = lambda url, **kw: Resp(payload={"data": [{"id": "llama-3.3-70b-versatile"}]})
uvicorn.run(backend.app, host="127.0.0.1", port=port, log_level="warning")
