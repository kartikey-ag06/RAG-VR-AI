"""Shared test setup: every test runs offline, on a throw-away data folder, with fresh limits.

Why this file exists
  - Isolation: the backend keeps state in module globals (rate-limit buckets, AI budget, answer cache, model list).
    Without a reset, one test file used up the AI rate limit and later tests got 429 instead of the status they check.
  - Safety: tests must never write into backend/data/ or a real Neon database, and never spend a real Groq key.
  - Offline: outbound HTTP (Groq, OpenStreetMap, Open-Meteo) is answered by small fakes; anything else is blocked.

Run:  python -m pytest -q tests
"""
import atexit
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest
import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

# ---------------------------------------------------------------- environment, BEFORE the app is imported
_DATA = tempfile.mkdtemp(prefix="vr_rag_test_")
atexit.register(shutil.rmtree, _DATA, ignore_errors=True)
os.environ["VR_RAG_DATA_DIR"] = _DATA          # documents, page images, app data -> temp folder
os.environ["VR_RAG_NO_DB"] = "1"                 # never connect to a real database, even if database_url.txt exists
os.environ["VR_RAG_NO_ENVFILE"] = "1"            # ignore a developer's .env (keys, tokens, limits)
for _k in ("GROQ_API_KEY", "XAI_API_KEY", "ADMIN_TOKEN", "MCP_TOKEN", "RENDER", "DATABASE_URL", "PUBLIC_URL",
           "TURN_URLS", "TURN_SECRET"):
    os.environ.pop(_k, None)

import app as backend  # noqa: E402
import limits  # noqa: E402

backend.KEY_FILE = Path(_DATA) / "no_key.txt"      # ignore a developer's real grok_api_key.txt
backend.MODEL_FILE = Path(_DATA) / "no_model.txt"

SEED_DOCS = sum(1 for f in (ROOT / "docs").iterdir() if f.suffix.lower() in (".pdf", ".docx", ".txt"))


def _wait_for_index(timeout=300):
    """The first run imports docs/ in a background thread; tests need the finished index."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        ready = [e for e in backend.STORE.list() if e["status"] == "ready"]
        busy = any(e["status"] == "processing" for e in backend.STORE.list())
        failed = [e for e in backend.STORE.list() if e["status"] == "error"]
        if failed and not busy:            # stop at once instead of waiting the full timeout
            raise RuntimeError(f"{len(failed)} seed document(s) failed to index, e.g. {failed[0]['name']}: "
                               f"{failed[0]['error']}  ->  run: python -m pip install -r backend/requirements.txt")
        if (len(backend.STORE.snapshot["index"]["docs"]) >= SEED_DOCS and not busy
                and backend.STORE.state == "idle" and not backend.STORE.queue and len(ready) >= SEED_DOCS):
            return
        time.sleep(0.2)
    raise RuntimeError(f"seed documents were not indexed within {timeout}s")


_wait_for_index()


# ---------------------------------------------------------------- fake outside world
class FakeResp:
    def __init__(self, payload, status=200):
        self.status_code, self._payload, self.encoding = status, payload, None
        self.text = str(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_lines(self, decode_unicode=True):
        return iter(())

    def close(self):
        pass


def fake_get(url, params=None, **kw):
    u = str(url)
    if "nominatim.openstreetmap.org" in u:
        return FakeResp([{"display_name": f"{(params or {}).get('q', 'Place')}, India", "lat": "28.6139", "lon": "77.2090"}])
    if "routing.openstreetmap.de" in u or "/route/v1/" in u:
        return FakeResp({"code": "Ok", "routes": [{"distance": 1200.0, "duration": 900.0, "legs": [{"steps": [
            {"maneuver": {"type": "depart", "bearing_after": 0, "location": [77.209, 28.6139]}, "name": "Rajpath",
             "distance": 1200.0, "duration": 900.0},
            {"maneuver": {"type": "arrive", "location": [77.22, 28.62]}, "name": "", "distance": 0, "duration": 0}]}]}]})
    if "geocoding-api.open-meteo.com" in u:
        return FakeResp({"results": [{"latitude": 28.61, "longitude": 77.21, "name": "Delhi", "country": "India"}]})
    if "api.open-meteo.com" in u:
        return FakeResp({"current": {"temperature_2m": 24.0, "weather_code": 0, "wind_speed_10m": 5.0,
                                     "relative_humidity_2m": 40},
                         "daily": {"time": ["2026-11-24"], "temperature_2m_max": [27.0], "temperature_2m_min": [14.0],
                                   "precipitation_probability_max": [0], "weather_code": [0]}})
    if u.endswith("/models"):
        return FakeResp({"data": [{"id": "llama-3.3-70b-versatile"}, {"id": "whisper-large-v3-turbo"}]})
    raise requests.ConnectionError(f"outbound HTTP is blocked in tests: GET {u}")


def fake_post(url, *a, **kw):
    raise requests.ConnectionError(f"outbound HTTP is blocked in tests: POST {url} (use a fixture that fakes it)")


@pytest.fixture(autouse=True)
def isolated_backend(monkeypatch):
    """Fresh limits, caches and a fake internet for every test."""
    saved_limits = dict(limits.LIMITS)
    limits.BUCKETS = limits.Buckets()
    limits.BUDGET = limits.DailyBudget()
    for k in limits.STATS:
        limits.STATS[k] = 0
    backend._ANSWERS.clear()
    backend._MODELS.update(ids=None, t=0.0)
    backend._cache.clear()
    backend._cache.update(chat=None, stt=None)
    monkeypatch.setattr(backend.HTTP, "get", fake_get)
    monkeypatch.setattr(backend.HTTP, "post", fake_post)
    yield
    limits.LIMITS.clear()
    limits.LIMITS.update(saved_limits)
    limits.BUCKETS = limits.Buckets()
    limits.BUDGET = limits.DailyBudget()
