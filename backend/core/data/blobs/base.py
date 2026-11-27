"""BlobStore protocol and safe key validation (Phase 11A)."""
from typing import Optional, Protocol, runtime_checkable


def validate_blob_key(key: str) -> str:
    """Validate a relative POSIX-style blob key and reject path traversal."""
    k = str(key or "")
    if not k or k.startswith("/") or "\\" in k:
        raise ValueError(f"invalid blob key {key!r}")
    parts = k.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ValueError(f"invalid blob key {key!r}")
    return k


def validate_blob_prefix(prefix: str) -> str:
    p = str(prefix or "").rstrip("/")
    if not p:
        return ""
    return validate_blob_key(p) + "/"


@runtime_checkable
class BlobStore(Protocol):
    name: str

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...

    def get(self, key: str) -> Optional[bytes]: ...

    def exists(self, key: str) -> bool: ...

    def delete(self, key: str) -> bool: ...

    def delete_prefix(self, prefix: str) -> int: ...

    def signed_url(self, key: str, ttl_seconds: int = 300) -> Optional[str]: ...
