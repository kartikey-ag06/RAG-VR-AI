"""Shared HTTP plumbing.

HTTP  - one keep-alive connection pool for every outbound call (Groq, Open-Meteo, webhooks). Re-using the TLS
        connection saves ~150-300 ms per AI call compared with requests.post(), which opens a new one each time.
JSONB - a FastAPI dependency that reads the JSON body. Handlers declared with plain `def` (not `async def`) run in
        FastAPI's worker threads, so a slow Groq / database call no longer freezes the whole server.
"""
import requests
from fastapi import Depends, Request
from requests.adapters import HTTPAdapter

HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=32))
HTTP.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=16))


async def _json_body(req: Request):
    try:
        b = await req.json()
    except Exception:
        return None
    return b


JSONB = Depends(_json_body)
