"""Tests for core.kernel (Phase 3 step 1): settings, service container, app factory - and that the real app still
behaves the same (CORS rules, middleware order, health check, services)."""
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

import app as backend
from core.kernel import Container, ServiceError, Settings, create_app
from core.kernel.settings import DEFAULT_CORS_REGEX, DEFAULT_EMBED_MODEL

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------- Settings
def test_settings_defaults_match_old_values():
    s = Settings.from_env(ROOT, [], {})
    assert s.data_dir == ROOT / "backend" / "data" and s.seed_dir == ROOT / "docs"
    assert s.app_dir == ROOT / "glasses_app" / "dist" and s.backend_dir == ROOT / "backend"
    assert s.embed_model == DEFAULT_EMBED_MODEL and s.no_db is False and s.port is None
    assert s.cors_origin_regex == DEFAULT_CORS_REGEX


def test_settings_from_environment(tmp_path):
    s = Settings.from_env(ROOT, ["app.py", "--app=web"], {
        "VR_RAG_DATA_DIR": str(tmp_path), "VR_RAG_NO_DB": "1", "VR_RAG_EMBED_MODEL": "m", "PORT": "10000",
        "VR_RAG_CORS_REGEX": "^https://x$"})
    assert s.data_dir == tmp_path and s.no_db and s.embed_model == "m" and s.port == 10000
    assert s.app_dir == ROOT / "web" and s.cors_origin_regex == "^https://x$"
    assert Settings.from_env(ROOT, [], {"VR_RAG_NO_DB": "yes", "PORT": "abc"}).no_db is False
    with pytest.raises(Exception):
        s.no_db = False                                   # frozen


def test_real_app_uses_settings():
    assert backend.SETTINGS.data_dir == backend.DATA_DIR and backend.SETTINGS.no_db is True   # set by conftest
    assert backend.app.state.settings is backend.SETTINGS


# ---------------------------------------------------------------- Container
def test_container_builds_once_and_in_dependency_order():
    order, c = [], Container()
    c.register("a", lambda c: order.append("a") or "A")
    c.register("b", lambda c: order.append("b") or c.get("a") + "B")
    assert c.get("b") == "AB" and c.get("b") == "AB" and order == ["b", "a"]
    assert c.built("a") and c.names() == ["a", "b"]


def test_container_errors_and_override():
    c = Container()
    c.register("x", lambda c: c.get("y"))
    c.register("y", lambda c: c.get("x"))
    with pytest.raises(ServiceError, match="loop: x -> y -> x"):
        c.get("x")
    with pytest.raises(ServiceError, match="unknown service"):
        c.get("nope")
    with pytest.raises(ServiceError, match="already registered"):
        c.register("x", lambda c: 1)
    c.override("x", "fake")
    assert c.get("x") == "fake" and c.has("x")


def test_container_is_thread_safe():
    calls, c = [], Container()

    def slow(c):
        calls.append(1)
        threading.Event().wait(0.05)
        return object()
    c.register("s", slow)
    got = []
    ts = [threading.Thread(target=lambda: got.append(c.get("s"))) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 1 and len({id(g) for g in got}) == 1


def test_real_app_services_are_the_old_globals():
    s = backend.SERVICES
    assert s.get("store") is backend.STORE and s.get("auth") is backend.AUTH and s.get("kv") is backend.KV
    assert s.get("db") is None and backend.DBX is None                       # VR_RAG_NO_DB=1 in tests
    assert backend.app.state.services is s


# ---------------------------------------------------------------- app factory
def test_factory_health_and_cors_outermost():
    class Deny:                                           # inner middleware that answers 429 itself
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http" and scope["path"] == "/deny":
                return await JSONResponse({"error": "slow down"}, status_code=429)(scope, receive, send)
            return await self.app(scope, receive, send)

    s = Settings.from_env(ROOT, [], {})
    c = TestClient(create_app(s, middleware=[(Deny, {})]))
    assert c.get("/healthz").json() == {"ok": True}
    r = c.get("/deny", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 429 and r.headers["access-control-allow-origin"] == "http://localhost:5173"


ORIGINS_OK = ["http://localhost:8765", "http://127.0.0.1:5173", "https://rag-vr.vercel.app", "https://x.onrender.com"]
ORIGINS_NO = ["https://evil.com", "http://rag-vr.vercel.app", "https://vercel.app.evil.com"]


@pytest.mark.parametrize("origin", ORIGINS_OK)
def test_real_app_cors_allows(origin):
    r = TestClient(backend.app).options("/api/search", headers={"Origin": origin, "Access-Control-Request-Method": "POST"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == origin
    assert "Retry-After" in TestClient(backend.app).get("/healthz", headers={"Origin": origin}).headers["access-control-expose-headers"]


@pytest.mark.parametrize("origin", ORIGINS_NO)
def test_real_app_cors_blocks(origin):
    r = TestClient(backend.app).get("/healthz", headers={"Origin": origin})
    assert "access-control-allow-origin" not in r.headers


def test_real_app_request_id_from_loadguard():
    r = TestClient(backend.app).get("/healthz")
    assert r.status_code == 200 and r.headers.get("x-request-id")
