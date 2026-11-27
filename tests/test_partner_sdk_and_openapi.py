"""Phase 13A Gate: Partner OpenAPI 3.1.0 coverage & Python SDK contract tests."""
import base64
import io
import json
import sys
import urllib.parse
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import app as backend
from core.agents import AgentTool
from tests.conftest import FakeResp

ROOT = Path(__file__).resolve().parents[1]
SDK_PY = ROOT / "sdk" / "python"
if str(SDK_PY) not in sys.path:
    sys.path.insert(0, str(SDK_PY))

from rag_vr_sdk import RagVrApiError, RagVrClient  # noqa: E402

tc = TestClient(backend.app)


def _tc_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: float):
    parsed = urllib.parse.urlsplit(url)
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    resp = tc.request(method, target, headers=headers, content=body)
    return resp.status_code, dict(resp.headers), resp.content


def _sharp_jpeg_data_url() -> str:
    """Create a high-contrast checkerboard image so frame_gate blur_score is well above threshold."""
    im = Image.new("RGB", (128, 128), "white")
    draw = ImageDraw.Draw(im)
    for y in range(0, 128, 8):
        for x in range(0, 128, 8):
            if ((x // 8) + (y // 8)) % 2 == 0:
                draw.rectangle([x, y, x + 7, y + 7], fill="black")
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=92)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}"


def _sharp_jpeg_bytes() -> bytes:
    im = Image.new("RGB", (64, 64), (20, 120, 220))
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_phase13_test")
    monkeypatch.setattr(
        backend.HTTP,
        "get",
        lambda *a, **kw: FakeResp({"data": [{"id": "llama-3.3-70b-versatile"}]}),
    )
    yield "admin-secret"


# ---------------------------------------------------------------- 1. OpenAPI route coverage
def test_openapi_spec_covers_all_v1_and_hud_routes():
    spec_path = ROOT / "docs" / "api" / "openapi-v1.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    assert spec["openapi"] == "3.1.0"
    paths = spec["paths"]
    assert "/api/v1/documents/{uid}/pages/{page}.jpg" in paths

    app_v1_routes = []
    for route in backend.app.routes:
        path = getattr(route, "path", "")
        methods = {m.lower() for m in (getattr(route, "methods", None) or set())} - {"head", "options"}
        if (path.startswith("/api/v1") or path == "/hud") and methods:
            app_v1_routes.append((path, methods))

    assert len(app_v1_routes) >= 11
    for path, methods in app_v1_routes:
        assert path in paths, f"Missing OpenAPI path: {path}"
        for method in methods:
            assert method in paths[path], f"Missing HTTP method {method.upper()} on {path}"


# ---------------------------------------------------------------- 2. Auth header routing
def test_python_sdk_auth_header_routing():
    seen: list[dict] = []

    def spy_transport(method, url, headers, body, timeout):
        seen.append(dict(headers))
        return 200, {"content-type": "application/json"}, b'{"api":"v1","ok":true}'

    c_key = RagVrClient("http://testserver", token="vrk_abc123", transport=spy_transport)
    c_key.health()
    assert seen[-1].get("Authorization") == "Bearer vrk_abc123"
    assert "X-VR-Token" not in seen[-1]

    c_tok = RagVrClient("http://testserver", token="user-session-tok", transport=spy_transport)
    c_tok.health()
    assert seen[-1].get("X-VR-Token") == "user-session-tok"
    assert "Authorization" not in seen[-1]


# ---------------------------------------------------------------- 3. Python SDK live contract tests
def test_python_sdk_health_ask_scan_page_image_and_agent_approval(protected, monkeypatch):
    # Create a scoped vrk_ key using admin token
    r = tc.post(
        "/api/core/keys",
        json={"name": "sdk-partner", "scopes": ["knowledge:read", "knowledge:write", "ai:ask", "ai:vision"]},
        headers={"X-VR-Token": protected},
    )
    assert r.status_code == 200, r.text
    vrk_token = r.json()["secret"]

    sdk = RagVrClient("http://testserver", token=vrk_token, transport=_tc_transport)

    # health()
    h = sdk.health()
    assert h["api"] == "v1"
    assert h["ok"] is True

    # ask()
    monkeypatch.setattr(
        backend.HTTP,
        "post",
        lambda *a, **kw: FakeResp({"choices": [{"message": {"content": "SOURCE: GENERAL\n\nTorque is 45 Nm."}}]}),
    )
    ans = sdk.ask("What is the torque?", style="glance")
    assert ans["mode"] == "general"
    assert "45 Nm" in ans["answer"]

    # scan(): ok -> skipped_duplicate
    monkeypatch.setitem(
        backend.VISION,
        "describe",
        lambda img, hint="": {"object": "valve-204", "details": "brass gate valve", "text_seen": "204"},
    )
    img_url = _sharp_jpeg_data_url()
    s1 = sdk.scan(img_url, question="Inspect valve", session_id="sdk-sess-1")
    assert s1["status"] == "ok"
    assert s1["cached"] is False
    assert s1["session_id"] == "sdk-sess-1"

    s2 = sdk.scan(img_url, question="Inspect valve", session_id="sdk-sess-1")
    assert s2["status"] == "skipped_duplicate"
    assert s2["cached"] is True

    # fetch_page_image()
    uid = "abcdef012345"
    raw_jpg = _sharp_jpeg_bytes()
    monkeypatch.setattr(backend.STORE, "doc_entry", lambda u: {"uid": uid, "title": "Manual", "pages": 1} if u == uid else None)
    monkeypatch.setattr(backend.STORE, "read_page_bytes", lambda u, n: raw_jpg if (u == uid and n == 1) else None)
    img_bytes = sdk.fetch_page_image(uid, 1, width=200)
    assert img_bytes.startswith(b"\xff\xd8")

    # agent_run() -> awaiting_approval -> get_approval() -> approve_agent_action() -> 409 replay
    executed = []

    def _write_handler(_req, args):
        executed.append(args)
        return json.dumps({"updated": True, "args": args})

    tool = AgentTool(
        name="sdk_write_tool",
        description="Write tool for SDK approval contract test",
        parameters={"type": "object", "properties": {"note": {"type": "string"}}},
        read_only=False,
        required_scope="knowledge:write",
        module_id=None,
        handler=_write_handler,
    )
    backend.AGENT_TOOLS.register(tool)
    try:
        step = {"n": 0}

        def _agent_post(*a, **kw):
            step["n"] += 1
            if step["n"] == 1:
                return FakeResp({"choices": [{"message": {"content": "", "tool_calls": [
                    {"id": "tc_1", "type": "function", "function": {"name": "sdk_write_tool", "arguments": '{"note":"ok"}'}}
                ]}}]})
            return FakeResp({"choices": [{"message": {"content": "Write action completed."}}]})

        monkeypatch.setattr(backend.HTTP, "post", _agent_post)

        run_res = sdk.agent_run("Update the inspection note")
        assert run_res["status"] == "awaiting_approval"
        aid = run_res["approval_id"]
        assert executed == []

        rec = sdk.get_approval(aid)
        assert rec["status"] == "pending"
        assert rec["tool_name"] == "sdk_write_tool"

        app_res = sdk.approve_agent_action(aid)
        assert app_res["status"] == "completed"
        assert executed == [{"note": "ok"}]

        with pytest.raises(RagVrApiError) as exc_info:
            sdk.approve_agent_action(aid)
        assert exc_info.value.status == 409
    finally:
        backend.AGENT_TOOLS.unregister("sdk_write_tool")
