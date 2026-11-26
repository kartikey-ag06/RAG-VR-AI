"""Unit & integration tests for frame_gate, session_state, and gated /api/v1/scan."""
import base64
from io import BytesIO

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

import app as backend
from frame_gate import compute_blur_score, compute_dhash, evaluate_frame, hamming_distance
from session_state import SCAN_SESSIONS

client = TestClient(backend.app)
ADMIN = {"X-VR-Token": "admin-secret"}


def _data_url(img: Image.Image, fmt: str = "JPEG") -> str:
    buf = BytesIO()
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/{fmt.lower()};base64,{b64}"


def _sharp_image(seed_shift: int = 0) -> Image.Image:
    """High-contrast checkerboard / geometric pattern -> high Laplacian variance."""
    arr = np.zeros((128, 128, 3), dtype=np.uint8)
    for y in range(0, 128, 8):
        for x in range(0, 128, 8):
            if ((x // 8 + y // 8 + seed_shift) % 2) == 0:
                arr[y:y + 8, x:x + 8] = [245, 245, 245]
    img = Image.fromarray(arr, mode="RGB")
    draw = ImageDraw.Draw(img)
    draw.rectangle([16, 16, 110, 110], outline=(255, 40, 40), width=3)
    return img


def _blurry_image() -> Image.Image:
    """Smooth low-contrast gradient -> near-zero Laplacian variance."""
    y = np.linspace(110, 130, 128, dtype=np.uint8)[:, None]
    arr = np.broadcast_to(y, (128, 128)).copy()
    return Image.fromarray(arr, mode="L").convert("RGB")


@pytest.fixture(autouse=True)
def clear_scan_sessions():
    SCAN_SESSIONS.clear()
    yield
    SCAN_SESSIONS.clear()


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-secret")
    yield ADMIN


@pytest.fixture
def fake_vision(monkeypatch):
    calls = []

    def fake_describe(image, hint=""):
        calls.append({"hint": hint})
        return {
            "object": "Centrifugal Pump P-101",
            "details": "Blue industrial pump with pressure gauge.",
            "text_seen": "P-101 MAX 150 PSI",
            "keywords": "pump pressure gauge P-101",
        }

    monkeypatch.setitem(backend.VISION, "describe", fake_describe)
    return calls


def _key(tenant: str, scopes):
    r = client.post("/api/core/keys", json={"name": "scan-test", "scopes": scopes, "tenant_id": tenant}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["secret"]}


# ---------------------------------------------------------------- unit tests: frame_gate
def test_frame_gate_blur_and_dhash_metrics():
    sharp_bytes = BytesIO()
    _sharp_image().save(sharp_bytes, format="PNG")
    blur_bytes = BytesIO()
    _blurry_image().save(blur_bytes, format="PNG")

    res_blur = evaluate_frame(blur_bytes.getvalue())
    assert res_blur.is_blurry is True and res_blur.blur_score < 60.0

    res_sharp = evaluate_frame(sharp_bytes.getvalue())
    assert res_sharp.is_blurry is False and res_sharp.blur_score > 60.0 and res_sharp.is_duplicate is False

    res_dup = evaluate_frame(sharp_bytes.getvalue(), last_dhash=res_sharp.dhash)
    assert res_dup.is_duplicate is True and res_dup.hamming_distance == 0
    assert hamming_distance(0b1010, 0b1110) == 1
    assert compute_blur_score(_blurry_image()) < compute_blur_score(_sharp_image())
    assert compute_dhash(_sharp_image()) == res_sharp.dhash


# ---------------------------------------------------------------- integration tests: /api/v1/scan
def test_scan_blurry_frame_short_circuits_vision(protected, fake_vision):
    k = _key("default", ["knowledge:read", "ai:ask", "ai:vision"])
    r = client.post("/api/v1/scan", json={"image": _data_url(_blurry_image(), "PNG"), "session_id": "s-blur"}, headers=k)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "skipped_blur"
    assert body["cached"] is False
    assert body["gate_metrics"]["is_blurry"] is True
    assert "blurry" in body["answer"].lower()
    assert len(fake_vision) == 0


def test_scan_duplicate_frame_caches_and_reused_scene_followup(protected, fake_vision):
    k = _key("default", ["knowledge:read", "ai:ask", "ai:vision"])
    img_url = _data_url(_sharp_image(), "PNG")

    # 1. First sharp frame calls Vision once
    r1 = client.post("/api/v1/scan", json={"image": img_url, "session_id": "s-dup", "style": "glance"}, headers=k)
    assert r1.status_code == 200, r1.text
    b1 = r1.json()
    assert b1["status"] == "ok" and b1["cached"] is False
    assert b1["entity_state"]["active_entity"] == "Centrifugal Pump P-101"
    assert len(fake_vision) == 1

    # 2. Identical frame with no new question -> skipped_duplicate, Vision NOT called again
    r2 = client.post("/api/v1/scan", json={"image": img_url, "session_id": "s-dup"}, headers=k)
    assert r2.status_code == 200
    b2 = r2.json()
    assert b2["status"] == "skipped_duplicate" and b2["cached"] is True
    assert b2["answer"] == b1["answer"]
    assert len(fake_vision) == 1

    # 3. Duplicate frame + new follow-up question -> reused_scene, Vision still skipped, RAG runs
    r3 = client.post("/api/v1/scan", json={
        "image": img_url,
        "session_id": "s-dup",
        "question": "What is the max PSI rating?",
        "style": "spoken",
    }, headers=k)
    assert r3.status_code == 200
    b3 = r3.json()
    assert b3["status"] == "reused_scene" and b3["cached"] is False
    assert b3["entity_state"]["turn_count"] == 2
    assert len(fake_vision) == 1


def test_scan_session_isolated_across_keys_and_tenants(protected, fake_vision):
    tenants = backend.SERVICES.get("tenants")
    if not tenants.get("acme"):
        tenants.create("acme", "Acme")
    k1 = _key("default", ["knowledge:read", "ai:ask", "ai:vision"])
    k2 = _key("acme", ["knowledge:read", "ai:ask", "ai:vision"])
    img_url = _data_url(_sharp_image(), "PNG")

    r1 = client.post("/api/v1/scan", json={"image": img_url, "session_id": "shared-id"}, headers=k1)
    assert r1.status_code == 200 and r1.json()["status"] == "ok"

    # Same session_id under a different key/tenant must NOT hit k1's cache
    r2 = client.post("/api/v1/scan", json={"image": img_url, "session_id": "shared-id"}, headers=k2)
    assert r2.status_code == 200 and r2.json()["status"] == "ok" and r2.json()["cached"] is False
    assert len(fake_vision) == 2


def test_scan_backward_compatible_image_only(protected, fake_vision):
    k = _key("default", ["knowledge:read", "ai:ask", "ai:vision"])
    r = client.post("/api/v1/scan", json={"image": _data_url(_sharp_image(), "PNG")}, headers=k)
    assert r.status_code == 200
    body = r.json()
    assert {"answer", "citations", "photo", "session_id", "status"} <= set(body)
    assert body["photo"]["object"] == "Centrifugal Pump P-101"
