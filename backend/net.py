"""Shared HTTP plumbing.

HTTP  - one keep-alive connection pool for every outbound call (Groq, Open-Meteo, webhooks). Re-using the TLS
        connection saves ~150-300 ms per AI call compared with requests.post(), which opens a new one each time.
JSONB - a FastAPI dependency that reads the JSON body. Handlers declared with plain `def` (not `async def`) run in
        FastAPI's worker threads, so a slow Groq / database call no longer freezes the whole server.
"""
import ipaddress
import json
import socket
from urllib.parse import urlparse
import requests
from fastapi import Depends, HTTPException, Request
from requests.adapters import HTTPAdapter

HTTP = requests.Session()
HTTP.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=32))
HTTP.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=16))

MAX_BODY_SIZE = 1024 * 1024  # 1 MB


def is_safe_url(url: str, allow_http: bool = True) -> bool:
    """Validate that a URL points to a legitimate public remote host (prevent SSRF)."""
    if not url or not isinstance(url, str):
        return False
    try:
        p = urlparse(url.strip())
        schemes = ("http", "https") if allow_http else ("https",)
        if p.scheme.lower() not in schemes:
            return False
        host = p.hostname
        if not host:
            return False
        host = host.strip("[]")
        if host.lower() in ("localhost", "metadata.google.internal", "instance-data"):
            return False
        addr_info = socket.getaddrinfo(host, None)
        for _, _, _, _, sockaddr in addr_info:
            ip = ipaddress.ip_address(sockaddr[0])
            if (ip.is_loopback or ip.is_private or ip.is_link_local or
                ip.is_multicast or ip.is_reserved or ip.is_unspecified):
                return False
        return True
    except Exception:
        return False


async def _json_body(req: Request):
    content_length = req.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_BODY_SIZE:
                raise HTTPException(status_code=413, detail="Payload too large")
        except ValueError:
            pass
    try:
        body = await req.body()
    except Exception:
        return None
    if not body:
        return None
    if len(body) > MAX_BODY_SIZE:
        raise HTTPException(status_code=413, detail="Payload too large")
    try:
        b = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")
    if b is not None and not isinstance(b, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return b


JSONB = Depends(_json_body)

