import sys
from pathlib import Path

# Add backend directory to path
BACKEND_DIR = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient
from app import app, STORE

client = TestClient(app)

def test_api_suite():
    errors = []

    def check(desc, method, url, expected_status=None, **kwargs):
        fn = getattr(client, method.lower())
        try:
            r = fn(url, **kwargs)
            if expected_status and r.status_code != expected_status:
                errors.append(f"{desc}: Expected {expected_status}, got {r.status_code} ({r.text[:200]})")
            elif r.status_code >= 500:
                errors.append(f"{desc}: 500 Server Error ({r.text[:200]})")
            return r
        except Exception as e:
            errors.append(f"{desc}: Exception {e}")
            return None

    # HTML Pages
    check("Admin Page", "GET", "/admin", 200)
    check("Expert Page", "GET", "/expert", 200)
    check("ProcOps Page", "GET", "/procops", 200)
    check("Upload Page", "GET", "/upload", 200)
    check("Workspace Page", "GET", "/workspace", 200)

    # Theme Assets
    check("Theme CSS", "GET", "/theme/vr-theme.css", 200)
    check("Theme JS", "GET", "/theme/vr-shell.js", 200)

    # RAG Endpoints
    check("RAG Index", "GET", "/rag/index.json", 200)
    check("Index Version", "GET", "/api/index/version", 200)
    check("Files List", "GET", "/api/files", 200)
    check("Machines List", "GET", "/api/machines", 200)
    check("Machines Resolve", "POST", "/api/machines/resolve", 200, json={"text": "SEBI"})
    check("Search", "POST", "/api/search", 200, json={"query": "penalty", "k": 3})

    # Provider & Vision
    check("Grok Status", "GET", "/api/grok/status", 200)
    check("Grok Models", "GET", "/api/grok/models", 200)
    check("Vision Status", "GET", "/api/vision/status", 200)

    # Notes Lifecycle
    rn = check("Create Note", "POST", "/api/notes", 200, json={"text": "Test Note"})
    if rn and rn.status_code == 200:
        nid = rn.json().get("id")
        check("Delete Note", "DELETE", f"/api/notes/{nid}", 200)

    # Reminders Lifecycle
    rr = check("Create Reminder", "POST", "/api/reminders", 200, json={"text": "Test Reminder", "due": "2026-11-25T10:00:00Z"})
    if rr and rr.status_code == 200:
        rid = rr.json().get("id")
        check("Patch Reminder", "PATCH", f"/api/reminders/{rid}", 200, json={"done": True})
        check("Delete Reminder", "DELETE", f"/api/reminders/{rid}", 200)

    # Expert Calling Lifecycle
    rc = check("Create Call", "POST", "/api/expert/call", 200, json={"question": "Help needed"})
    if rc and rc.status_code == 200:
        room_id = rc.json().get("room")
        check("Poll Call (with role)", "GET", f"/api/expert/{room_id}/poll?since=0&role=expert", 200)
        check("Poll Call (default)", "GET", f"/api/expert/{room_id}/poll?since=0", 200)
        check("End Call", "POST", f"/api/expert/{room_id}/end", 200)

    # Navigation & Places & Weather
    check("Directions Empty", "GET", "/api/directions", 400)
    check("Directions Valid", "GET", "/api/directions?to=Delhi&frm=Noida&mode=walk", 200)
    check("Places Empty", "GET", "/api/places", 400)
    check("Places Valid", "GET", "/api/places?q=Delhi", 200)
    check("Weather Empty", "GET", "/api/weather", 400)
    check("Weather Valid", "GET", "/api/weather?city=Delhi", 200)

    # MCP Protocol
    check("MCP ping", "POST", "/mcp", 200, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    check("MCP list tools", "POST", "/mcp", 200, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})

    assert len(errors) == 0, f"Encountered {len(errors)} errors:\n" + "\n".join(errors)
    print("All API tests passed cleanly with 0 errors!")

if __name__ == "__main__":
    test_api_suite()
