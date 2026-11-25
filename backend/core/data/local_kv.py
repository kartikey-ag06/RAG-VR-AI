"""LocalKV - the small-record (key-value) API on local JSON files, used when no database is configured.

One file per kind: <data_dir>/app_data/<kind>.json  ->  {id: {"data": ..., "updated": unix_time}}
(Moved from backend/db.py in Phase 3 step 4, code unchanged.)
"""
import json
import threading
import time
from pathlib import Path


class LocalKV:
    """Same small-record API on local JSON files (used when no database is configured)."""

    def __init__(self, root: Path):
        self.root = root / "app_data"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _file(self, kind):
        return self.root / f"{kind}.json"

    def _load(self, kind):
        f = self._file(kind)
        try:
            return json.loads(f.read_text("utf-8")) if f.exists() else {}
        except ValueError:
            return {}

    def _save(self, kind, d):
        tmp = self._file(kind).with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), "utf-8")
        tmp.replace(self._file(kind))

    def kv_put(self, kind, id_, data):
        with self.lock:
            d = self._load(kind)
            d[str(id_)] = {"data": data, "updated": time.time()}
            self._save(kind, d)

    def kv_get(self, kind, id_):
        with self.lock:
            r = self._load(kind).get(str(id_))
            return r["data"] if r else None

    def kv_list(self, kind):
        with self.lock:
            return [r["data"] for r in sorted(self._load(kind).values(), key=lambda r: -r["updated"])]

    def kv_delete(self, kind, id_):
        with self.lock:
            d = self._load(kind)
            d.pop(str(id_), None)
            self._save(kind, d)
