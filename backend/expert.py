"""
expert.py - "Escalate to remote expert": a worker in the glasses calls a senior technician.

Video/audio go directly between the two browsers (WebRTC). This server only passes the short set-up
messages between them ("signalling") and keeps the call context (question, AI answer, sources, last scan),
so the expert sees what the AI already said. Plain HTTP polling is used on purpose: it also works through
the Vercel -> Render proxy, where WebSockets would not.

  POST /api/expert/call            worker opens a call  -> {room}
  GET  /api/expert/rooms           expert console: waiting / active calls
  POST /api/expert/{room}/send     {from: "worker"|"expert", type, data}
  GET  /api/expert/{room}/poll     messages from the other side since n
  POST /api/expert/{room}/end
  GET  /expert                     the expert's page
  GET  /api/expert/ice             STUN + TURN servers for both browsers

TURN relay (for office / mobile networks that block direct calls): set
  TURN_URLS="turn:turn.example.com:3478,turns:turn.example.com:5349"
  and either TURN_USERNAME + TURN_CREDENTIAL (fixed login)
  or TURN_SECRET (coturn "use-auth-secret": a login valid for 6 hours is made per call),
or put the same keys in turn.json in the project folder (git-ignored). TURN_FORCE=1 sends everything via TURN.
"""
from net import HTTP, JSONB
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse

ROOM_TTL = 2 * 3600
STUN = [{"urls": ["stun:stun.l.google.com:19302", "stun:stun1.l.google.com:19302"]}]


def turn_config(root):
    """TURN settings from the environment, else from turn.json."""
    cfg = {k: os.environ.get(k, "") for k in ("TURN_URLS", "TURN_USERNAME", "TURN_CREDENTIAL", "TURN_SECRET", "TURN_FORCE")}
    f = root / "turn.json"
    if not cfg["TURN_URLS"] and f.is_file():
        try:
            j = json.loads(f.read_text(encoding="utf-8"))
            cfg.update({k: str(j.get(k, "")) for k in cfg})
        except Exception as e:
            print(f"[expert] turn.json unreadable: {e}", flush=True)
    return cfg


def ice_servers(root, user="vrrag"):
    cfg = turn_config(root)
    servers = list(STUN)
    urls = [u.strip() for u in cfg["TURN_URLS"].split(",") if u.strip()]
    if urls:
        if cfg["TURN_SECRET"]:                       # coturn REST API style: expiring login made with the secret
            name = f"{int(time.time()) + 6 * 3600}:{user}"
            cred = base64.b64encode(hmac.new(cfg["TURN_SECRET"].encode(), name.encode(), hashlib.sha1).digest()).decode()
            servers.append({"urls": urls, "username": name, "credential": cred})
        else:
            servers.append({"urls": urls, "username": cfg["TURN_USERNAME"], "credential": cfg["TURN_CREDENTIAL"]})
    return {"iceServers": servers, "turn": bool(urls),
            "iceTransportPolicy": "relay" if urls and cfg["TURN_FORCE"] in ("1", "true", "yes") else "all"}


def register(app, root, auth=None):
    rooms, lock = {}, threading.Lock()
    page = root / "backend" / "static" / "expert.html"

    def clean():
        now = time.time()
        for r in [r for r, v in rooms.items() if now - v["seen"] > ROOM_TTL]:
            rooms.pop(r, None)

    @app.get("/api/expert/ice")
    def ice(req: Request):
        return ice_servers(root, secrets.token_hex(3))

    @app.post("/api/expert/call")
    def call(req: Request, _body=JSONB):
        b = (_body if _body is not None else {})
        ctx = b.get("context") if isinstance(b.get("context"), dict) else {}
        rid = secrets.token_urlsafe(12)             # unguessable: knowing the id = being invited
        with lock:
            clean()
            rooms[rid] = {"id": rid, "created": time.time(), "seen": time.time(), "status": "waiting", "msgs": [], "seq": 0,
                          "context": {k: str(v)[:3000] for k, v in ctx.items()}}
        return {"room": rid, "expert_url": f"/expert?room={rid}"}

    @app.get("/api/expert/rooms")
    def list_rooms(req: Request):
        if auth is not None and auth.admin_required(req) and not (auth.is_admin(req) or auth.user_of(req)):
            return JSONResponse({"error": "experts need an access token on a protected server", "rooms": []}, 403)
        with lock:
            clean()
            return {"rooms": [{"id": r["id"], "status": r["status"], "created": r["created"], "context": r["context"]}
                              for r in sorted(rooms.values(), key=lambda r: -r["created"]) if r["status"] != "ended"]}

    @app.get("/api/expert/{rid}")
    def room(rid: str):
        r = rooms.get(rid)
        return {k: r[k] for k in ("id", "status", "created", "context")} if r else JSONResponse({"error": "no such call"}, 404)

    @app.post("/api/expert/{rid}/send")
    def send(rid: str, req: Request, _body=JSONB):
        b = (_body if _body is not None else {})
        sender = b.get("from")
        if sender not in ("worker", "expert"):
            return JSONResponse({"error": "from must be worker or expert"}, 400)
        with lock:
            r = rooms.get(rid)
            if not r:
                return JSONResponse({"error": "no such call"}, 404)
            r["seq"] += 1
            r["msgs"].append({"n": r["seq"], "from": sender, "type": str(b.get("type", ""))[:20],
                              "data": b.get("data"), "t": time.time()})
            r["msgs"] = r["msgs"][-400:]
            r["seen"] = time.time()
            if sender == "expert" and r["status"] == "waiting":
                r["status"] = "active"
        return {"ok": True}

    @app.get("/api/expert/{rid}/poll")
    def poll(rid: str, me: str, since: int = 0):
        with lock:
            r = rooms.get(rid)
            if not r:
                return JSONResponse({"error": "no such call"}, 404)
            r["seen"] = time.time()
            return {"status": r["status"], "msgs": [m for m in r["msgs"] if m["n"] > since and m["from"] != me],
                    "last": r["msgs"][-1]["n"] if r["msgs"] else 0}

    @app.post("/api/expert/{rid}/end")
    def end(rid: str):
        with lock:
            r = rooms.get(rid)
            if r:
                r["status"] = "ended"
                r["seq"] += 1
                r["msgs"].append({"n": r["seq"], "from": "server", "type": "end", "data": None, "t": time.time()})
        return {"ended": rid}

    @app.get("/expert")
    def expert_page():
        return FileResponse(page, headers={"Cache-Control": "no-store"})
