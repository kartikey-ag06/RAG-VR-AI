"""End-to-end: the REAL glasses client (glasses_app/src/rag.js) asks the REAL backend, with a fake Groq.

Checks the Phase 4 move from /api/grok to /api/v1/ask: streaming, citations pointing at the client's own hits,
the server-side prompt, answer style + language, photo questions, AI failure, Stop.
Needs Node.js 18+ (skipped otherwise). Takes ~30 s: the server indexes the 10 seed PDFs first.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
E2E = Path(__file__).resolve().parent / "e2e"
NODE = shutil.which("node")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not NODE, reason="Node.js not installed")
def test_glasses_client_against_real_backend(tmp_path):
    # rag.js is a Vite module: swap import.meta.env (Vite-only) for a global the test sets
    src = (ROOT / "glasses_app" / "src" / "rag.js").read_text("utf-8")
    rag = tmp_path / "rag.mjs"
    rag.write_text(src.replace("import.meta.env", "globalThis.__ENV"), "utf-8")
    port = free_port()
    server = subprocess.Popen([sys.executable, str(E2E / "fake_groq_server.py"), str(ROOT), str(port), str(tmp_path)],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        base = f"http://127.0.0.1:{port}"
        for _ in range(240):                                     # wait until the 10 seed PDFs are indexed
            try:
                if json.load(urllib.request.urlopen(base + "/api/index/version", timeout=2)).get("docs", 0) >= 10:
                    break
            except Exception:
                pass
            if server.poll() is not None:
                pytest.fail("server stopped:\n" + server.stdout.read().decode(errors="replace")[-3000:])
            time.sleep(0.5)
        else:
            pytest.fail("server did not index the seed documents in time")
        env = dict(os.environ, E2E_API=base, E2E_OUT=str(tmp_path), E2E_RAG=rag.as_uri())
        r = subprocess.run([NODE, str(E2E / "glasses_client_test.mjs")], env=env, capture_output=True, text=True, timeout=120)
        print(r.stdout)
        assert r.returncode == 0, r.stdout + r.stderr
        assert r.stdout.count("ok  -") == 13
    finally:
        server.terminate()
        try:
            server.wait(10)
        except subprocess.TimeoutExpired:
            server.kill()
