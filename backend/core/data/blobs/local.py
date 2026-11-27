"""Local filesystem BlobStore implementation (Phase 11A)."""
import os
import tempfile
from pathlib import Path
from typing import Optional

from .base import validate_blob_key, validate_blob_prefix


class LocalBlobStore:
    name = "local"

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        k = validate_blob_key(key)
        return self.root / Path(*k.split("/"))

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        k = validate_blob_key(key)
        target = self._path(k)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".blob_")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp_name, target)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return k

    def get(self, key: str) -> Optional[bytes]:
        p = self._path(key)
        return p.read_bytes() if p.is_file() else None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> bool:
        p = self._path(key)
        if not p.is_file():
            return False
        p.unlink()
        return True

    def delete_prefix(self, prefix: str) -> int:
        p = validate_blob_prefix(prefix)
        base = self.root / Path(*p.rstrip("/").split("/")) if p else self.root
        if not base.exists():
            return 0
        count = 0
        for item in sorted(base.rglob("*"), reverse=True):
            if item.is_file():
                item.unlink()
                count += 1
            elif item.is_dir() and item != self.root:
                try:
                    item.rmdir()
                except OSError:
                    pass
        if base.is_dir() and base != self.root:
            try:
                base.rmdir()
            except OSError:
                pass
        return count

    def signed_url(self, key: str, ttl_seconds: int = 300) -> Optional[str]:
        validate_blob_key(key)
        return None
