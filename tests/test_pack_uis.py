"""Smoke tests for /healthcare and /warehouse pack console UIs."""
from pathlib import Path

from fastapi.testclient import TestClient

import app as backend

client = TestClient(backend.app)
ROOT = Path(__file__).resolve().parents[1]


def _check_ui_hygiene(html: str):
    assert "Authorization" in html and "Bearer" in html
    assert "X-VR-Token" in html
    assert "URL.createObjectURL" in html
    assert "history.replaceState" in html
    assert "getUserMedia(" not in html
    assert "?token=" not in html


def test_healthcare_console_ui_served_and_clean():
    r = client.get("/healthcare")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    html = r.text
    _check_ui_hygiene(html)
    assert "/api/v1/packs/healthcare_sop/topics" in html
    assert "/api/v1/packs/healthcare_sop/checklist" in html
    assert "/api/v1/ask" in html
    assert "/api/v1/agent/run" in html


def test_warehouse_supervisor_ui_served_and_clean():
    r = client.get("/warehouse")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
    html = r.text
    _check_ui_hygiene(html)
    assert "/api/v1/packs/warehouse_qc/summary" in html
    assert "/api/v1/packs/warehouse_qc/lots" in html
    assert "/release" in html
    assert "/api/v1/agent/run" in html
    assert "/api/v1/agent/approvals/" in html
    assert "/api/v1/ask" in html


def test_static_html_files_exist():
    assert (ROOT / "backend" / "packs" / "healthcare_sop" / "static" / "dashboard.html").is_file()
    assert (ROOT / "backend" / "packs" / "warehouse_qc" / "static" / "dashboard.html").is_file()
