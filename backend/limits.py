"""
limits.py - production load protection for the VR_RAG backend (no extra packages, no Redis).

What it does, in the order a request meets it:
  1. RequestLog      - gives every request an id (X-Request-ID), times it, logs slow (>2 s) and failed (>=500, 429) calls.
  2. BodyLimit       - refuses bodies that are too big BEFORE they are read into memory (413).
                       Uploads 120 MB per request, speech 25 MB (Groq's own limit), everything else 1 MB.
  3. RateLimit       - token buckets per caller (user token, else IP) and per route group. Over the limit -> 429 +
                       Retry-After. AI routes also share a daily budget (per caller and for the whole server) and a
                       cap on how many AI calls run at the same time (extra calls wait up to AI_QUEUE_WAIT s, then 503).

Every number can be changed with an environment variable (see LIMITS below), so Render can be tuned without a deploy
of new code. Counters live in this process's memory: right for one server instance (Render free/starter). If you ever
run several instances, move the buckets to Redis.

Who is "the caller"?
  - a request with a user/admin token  -> that token (hashed): each worker has their own limit, even behind one Wi-Fi
  - otherwise                          -> the client IP. Behind proxies set PROXY_HOPS: 0 = direct connection,
                                          1 = Render (uses the last X-Forwarded-For entry, which Render itself adds).
"""
import asyncio
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict

log = logging.getLogger("vr_rag")


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# ------------------------------------------------------------------------------------------------- configuration
# per-minute rate and burst for each group of routes
LIMITS = {
    #  group      per minute                                   burst (requests allowed at once)
    "ai":      (_env_int("RATE_AI_PER_MIN", 20),        _env_int("RATE_AI_BURST", 8)),
    "stt":     (_env_int("RATE_STT_PER_MIN", 30),       _env_int("RATE_STT_BURST", 10)),
    "upload":  (_env_int("RATE_UPLOAD_PER_MIN", 10),    _env_int("RATE_UPLOAD_BURST", 5)),
    # MCP clients send several quick setup calls (initialize, tools/list, ...); /mcp also needs MCP_TOKEN
    "mcp":     (_env_int("RATE_MCP_PER_MIN", 60),       _env_int("RATE_MCP_BURST", 30)),
    "write":   (_env_int("RATE_WRITE_PER_MIN", 120),    _env_int("RATE_WRITE_BURST", 40)),
    "read":    (_env_int("RATE_READ_PER_MIN", 600),     _env_int("RATE_READ_BURST", 120)),
}
AI_DAILY_PER_CALLER = _env_int("AI_DAILY_PER_CALLER", 400)     # AI calls a day for one user / IP
AI_DAILY_TOTAL = _env_int("AI_DAILY_TOTAL", 5000)              # AI calls a day for the whole server (cost cap)
AI_MAX_CONCURRENT = _env_int("AI_MAX_CONCURRENT", 6)           # AI calls running at the same moment
AI_QUEUE_WAIT = _env_int("AI_QUEUE_WAIT", 20)                  # seconds a call may wait for a free slot
PROXY_HOPS = _env_int("PROXY_HOPS", 1 if os.environ.get("RENDER") else 0)
SLOW_MS = _env_int("LOG_SLOW_MS", 2000)

MB = 1024 * 1024
BODY_LIMITS = [                     # (path prefix, max bytes) - first match wins
    ("/api/files", 120 * MB),       # several documents at once; each file is still capped at 50 MB by the indexer
    ("/api/stt", 25 * MB),          # Groq Whisper refuses bigger files anyway
    ("/api/meetings/", 25 * MB),
    ("/api/vision/", 8 * MB),       # a camera frame as base64
    ("/api/connectors/", 2 * MB),
    ("", 1 * MB),                   # everything else is small JSON
]

# AI routes: these spend Groq credit. (method, path prefix)
AI_ROUTES = (
    ("POST", "/api/grok"), ("POST", "/api/translate"), ("POST", "/api/assistant/intent"),
    ("POST", "/api/vision/"), ("POST", "/api/workflows/generate"), ("POST", "/api/teleprompter/script"),
    ("POST", "/api/meetings/", "/stop"),
    ("POST", "/api/v1/ask"), ("POST", "/api/v1/scan"),       # /api/v1/ask/stream matches the first
)
# routes never limited: static files, page images, the cheap health check
FREE_PREFIXES = ("/assets/", "/theme/", "/rag/pages/", "/rag/figures/", "/healthz", "/audio/", "/scenes/", "/favicon")


def route_group(method, path):
    if path.startswith(FREE_PREFIXES) or method == "OPTIONS":
        return None
    for r in AI_ROUTES:
        if method == r[0] and path.startswith(r[1]) and (len(r) < 3 or path.endswith(r[2])):
            return "ai"
    if path.startswith("/api/stt"):
        return "stt"
    if path.startswith("/api/files") and method == "POST":
        return "upload"
    if path.startswith("/mcp"):
        return "mcp"
    if not path.startswith(("/api/", "/mcp", "/rag/")):
        return None                 # HTML pages and the built app: served from disk, cheap
    return "read" if method in ("GET", "HEAD") else "write"


def body_limit(path):
    for prefix, limit in BODY_LIMITS:
        if path.startswith(prefix):
            return limit
    return 1 * MB


# ------------------------------------------------------------------------------------------------- caller identity
def _header(scope, name):
    name = name.lower().encode()
    for k, v in scope.get("headers") or []:
        if k == name:
            return v.decode("latin-1")
    return ""


def client_ip(scope):
    """The caller's IP. With PROXY_HOPS=n, the n-th address from the right of X-Forwarded-For (added by our proxy),
    so a client cannot pick its own IP by sending a fake X-Forwarded-For header."""
    direct = (scope.get("client") or ("", 0))[0] or ""
    if PROXY_HOPS <= 0:
        return direct
    # Through the Vercel rewrite every visitor would arrive from Vercel's IP and share one bucket; Vercel puts the real
    # visitor in x-vercel-forwarded-for. (Someone calling Render directly could fake it, but that only moves an
    # anonymous caller to another bucket - AI routes are keyed by user token anyway.)
    vercel = _header(scope, "x-vercel-forwarded-for").split(",")[0].strip()
    if vercel:
        return vercel
    parts = [p.strip() for p in _header(scope, "x-forwarded-for").split(",") if p.strip()]
    return parts[-PROXY_HOPS] if len(parts) >= PROXY_HOPS else direct


def caller_key(scope):
    tok = _header(scope, "x-vr-token") or _header(scope, "authorization").removeprefix("Bearer ").strip()
    if tok:
        return "t:" + hashlib.sha256(tok.encode()).hexdigest()[:16]
    return "ip:" + client_ip(scope)


# ------------------------------------------------------------------------------------------------- token buckets
class Buckets:
    """Thread-safe token buckets, at most `max_keys` callers kept (least recently seen are dropped)."""

    def __init__(self, max_keys=20000):
        self.lock = threading.Lock()
        self.b = OrderedDict()
        self.max_keys = max_keys

    def take(self, key, per_min, burst, now=None):
        """Use one token. Returns 0 if allowed, else the seconds to wait."""
        now = now if now is not None else time.monotonic()
        rate = per_min / 60.0
        with self.lock:
            tokens, last = self.b.pop(key, (float(burst), now))
            tokens = min(float(burst), tokens + (now - last) * rate)
            if tokens >= 1:
                self.b[key] = (tokens - 1, now)
                wait = 0
            else:
                self.b[key] = (tokens, now)
                wait = max(1, int((1 - tokens) / rate + 0.999)) if rate > 0 else 3600
            while len(self.b) > self.max_keys:
                self.b.popitem(last=False)
            return wait


class DailyBudget:
    """AI calls per UTC day, per caller and in total."""

    def __init__(self):
        self.lock = threading.Lock()
        self.day = ""
        self.per = {}
        self.total = 0

    def take(self, key):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.lock:
            if day != self.day:
                self.day, self.per, self.total = day, {}, 0
            if self.total >= AI_DAILY_TOTAL:
                return "the server's daily AI limit is used up - it resets at midnight UTC"
            if self.per.get(key, 0) >= AI_DAILY_PER_CALLER:
                return "your daily AI limit is used up - it resets at midnight UTC"
            self.per[key] = self.per.get(key, 0) + 1
            self.total += 1
            return ""

    def stats(self):
        with self.lock:
            return {"day": self.day, "ai_calls_today": self.total, "ai_daily_total": AI_DAILY_TOTAL,
                    "ai_daily_per_caller": AI_DAILY_PER_CALLER, "callers_today": len(self.per)}


BUCKETS = Buckets()
BUDGET = DailyBudget()
STATS = {"requests": 0, "limited": 0, "too_large": 0, "ai_busy": 0, "errors": 0, "ai_running": 0}


# ------------------------------------------------------------------------------------------------- ASGI helpers
async def _send_json(send, status, data, headers=()):
    body = json.dumps(data).encode()
    hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
            (b"cache-control", b"no-store")] + [(k.encode(), str(v).encode()) for k, v in headers]
    await send({"type": "http.response.start", "status": status, "headers": hdrs})
    await send({"type": "http.response.body", "body": body})


class LoadGuard:
    """One pure-ASGI middleware for request ids + logging, body limits, rate limits and the AI gate.
    Pure ASGI (not BaseHTTPMiddleware) so streamed answers are passed through untouched, chunk by chunk."""

    def __init__(self, app):
        self.app = app
        self.ai_slots = None        # created lazily inside the running event loop

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        t0 = time.perf_counter()
        method, path = scope.get("method", "GET"), scope.get("path", "")
        rid = _header(scope, "x-request-id")[:64] or uuid.uuid4().hex[:12]
        state = {"status": 0}
        STATS["requests"] += 1

        async def send_logged(msg):
            if msg["type"] == "http.response.start":
                state["status"] = msg["status"]
                msg["headers"] = list(msg.get("headers") or []) + [(b"x-request-id", rid.encode())]
            await send(msg)

        try:
            await self._guarded(scope, receive, send_logged, method, path)
        except Exception:
            STATS["errors"] += 1
            log.exception("unhandled error rid=%s %s %s", rid, method, path)
            if not state["status"]:
                await _send_json(send_logged, 500, {"error": "internal error", "request_id": rid})
        finally:
            ms = (time.perf_counter() - t0) * 1000
            st = state["status"]
            if st >= 500 or st in (413, 429, 503) or ms > SLOW_MS:
                log.warning("rid=%s %s %s -> %s in %.0f ms (caller %s)", rid, method, path, st, ms, caller_key(scope))

    async def _guarded(self, scope, receive, send, method, path):
        # 1) body size - refuse before reading
        limit = body_limit(path)
        cl = _header(scope, "content-length")
        if cl.isdigit() and int(cl) > limit:
            STATS["too_large"] += 1
            return await _send_json(send, 413, {"error": f"request too large (max {limit // MB} MB)"})
        seen = {"n": 0}

        async def receive_limited():
            msg = await receive()
            if msg["type"] == "http.request":
                seen["n"] += len(msg.get("body", b""))
                if seen["n"] > limit:           # chunked upload without Content-Length
                    raise _TooLarge()
            return msg

        # 2) rate limit
        group = route_group(method, path)
        key = caller_key(scope)
        if group:
            per_min, burst = LIMITS[group]
            wait = BUCKETS.take(group + "|" + key, per_min, burst)
            if wait:
                STATS["limited"] += 1
                return await _send_json(send, 429, {"error": f"too many requests - try again in {wait} s", "retry_after": wait},
                                        [("retry-after", wait)])
        # 3) AI: daily budget + limited number running at once
        if group == "ai":
            msg = BUDGET.take(key)
            if msg:
                STATS["limited"] += 1
                return await _send_json(send, 429, {"error": msg, "retry_after": 3600}, [("retry-after", 3600)])
            if self.ai_slots is None:
                self.ai_slots = asyncio.Semaphore(AI_MAX_CONCURRENT)
            try:
                await asyncio.wait_for(self.ai_slots.acquire(), timeout=AI_QUEUE_WAIT)
            except asyncio.TimeoutError:
                STATS["ai_busy"] += 1
                return await _send_json(send, 503, {"error": "the AI is busy - try again in a few seconds", "retry_after": 5},
                                        [("retry-after", 5)])
            STATS["ai_running"] += 1
            try:                                   # the slot is held until the whole (streamed) answer is sent
                return await self._run(scope, receive_limited, send, limit)
            finally:
                STATS["ai_running"] -= 1
                self.ai_slots.release()
        return await self._run(scope, receive_limited, send, limit)

    async def _run(self, scope, receive, send, limit):
        started = {"v": False}

        async def send_mark(msg):
            if msg["type"] == "http.response.start":
                started["v"] = True
            await send(msg)
        try:
            await self.app(scope, receive, send_mark)
        except _TooLarge:
            STATS["too_large"] += 1
            if not started["v"]:
                await _send_json(send, 413, {"error": f"request too large (max {limit // MB} MB)"})


class _TooLarge(Exception):
    pass


def snapshot():
    """Numbers for /api/admin/status: how much protection is kicking in."""
    return {**STATS, **BUDGET.stats(), "ai_max_concurrent": AI_MAX_CONCURRENT,
            "rates_per_min": {k: v[0] for k, v in LIMITS.items()}, "proxy_hops": PROXY_HOPS}


def setup_logging():
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    if not logging.getLogger().handlers:
        logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.setLevel(level)
