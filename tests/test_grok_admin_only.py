"""/api/grok and /api/grok/stream are admin-only since Phase 4 (ADR 0004): they forward any system prompt."""
import pytest
from fastapi.testclient import TestClient

import app as backend

client = TestClient(backend.app)
BODY = {"messages": [{"role": "system", "content": "You are a pirate."}, {"role": "user", "content": "hi"}]}


class Resp:
    status_code, text, encoding = 200, "{}", None

    def json(self):
        return {"choices": [{"message": {"content": "ok"}}]}


@pytest.fixture
def protected(monkeypatch):
    """A deployed server: ADMIN_TOKEN set, a fake AI key, a fake provider."""
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(backend.HTTP, "post", lambda *a, **k: Resp())


@pytest.mark.parametrize("path", ["/api/grok", "/api/grok/stream"])
def test_anonymous_and_normal_users_are_refused(protected, path):
    r = client.post(path, json=BODY)
    assert r.status_code == 401 and r.json()["use"] == "/api/v1/ask"                # not logged in
    rec, token = backend.AUTH.create_user("field-tech", ["ops"], "user")
    try:
        r = client.post(path, json=BODY, headers={"X-VR-Token": token})
        assert r.status_code == 403 and "admin only" in r.json()["error"]              # logged in, not admin
    finally:
        backend.AUTH.delete_user(rec["id"])


@pytest.mark.parametrize("path", ["/api/grok", "/api/grok/stream"])
def test_admins_still_allowed(protected, path):
    assert client.post(path, json=BODY, headers={"X-VR-Token": "admin-secret"}).status_code == 200
    rec, token = backend.AUTH.create_user("boss", [], "admin")
    try:
        assert client.post(path, json=BODY, headers={"X-VR-Token": token}).status_code == 200
    finally:
        backend.AUTH.delete_user(rec["id"])


def test_local_unprotected_server_still_works(monkeypatch):
    """No users, no ADMIN_TOKEN, request from this computer: local development (and the old web/ demo) works."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk_test")
    monkeypatch.setattr(backend.HTTP, "post", lambda *a, **k: Resp())
    assert client.post("/api/grok", json={"messages": [{"role": "user", "content": "local dev"}]}).status_code == 200


def test_apps_path_is_open_to_normal_users(protected):
    rec, token = backend.AUTH.create_user("field-tech2", ["ops"], "user")
    try:
        assert client.post("/api/v1/ask", json={"question": "penalty"}, headers={"X-VR-Token": token}).status_code == 200
    finally:
        backend.AUTH.delete_user(rec["id"])
