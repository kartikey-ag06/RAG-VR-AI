"""Journal - append-only logs for modules (Phase 6). E.g. every question asked, every scan, every sensor reading.

Cheaper than small records (kv) for things that only grow: one row / line per entry, read back in order.
A "stream" is "<module_id>.<name>"; every stream is per tenant. archive() hides the current entries (demo reset)
without deleting them.

    Postgres: the `events` table (column `stream`, migration 2)
    Local:    app_data/journal/<tenant>/<stream>.jsonl
"""
import json
import re
import threading
import time
from pathlib import Path

from .tenancy import current_tenant

STREAM_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}\.[a-z][a-z0-9_]{0,39}$")


def check_stream(stream: str) -> str:
    if not STREAM_RE.match(str(stream)):
        raise ValueError(f"bad journal stream {stream!r}: use '<module_id>.<name>'")
    return stream


class LocalJournal:
    def __init__(self, root: Path):
        self.root = Path(root) / "app_data" / "journal"
        self.lock = threading.Lock()

    def _file(self, stream):
        d = self.root / current_tenant()
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{check_stream(stream)}.jsonl"

    def append(self, stream, data: dict) -> None:
        f = self._file(stream)
        with self.lock, f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(data, ensure_ascii=False) + "\n")

    def list(self, stream, limit=None) -> list:
        f = self._file(stream)
        if not f.exists():
            return []
        out = []
        with self.lock:
            for line in f.read_text("utf-8").splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out[-limit:] if limit else out

    def archive(self, stream) -> None:
        f = self._file(stream)
        with self.lock:
            if f.exists():
                f.replace(f.with_name(f"{f.stem}.{int(time.time())}.bak"))


class PgJournal:
    def __init__(self, db):
        self.db = db

    def append(self, stream, data: dict) -> None:
        self.db.journal_append(check_stream(stream), data)

    def list(self, stream, limit=None) -> list:
        rows = self.db.journal_list(check_stream(stream))
        return rows[-limit:] if limit else rows

    def archive(self, stream) -> None:
        self.db.journal_archive(check_stream(stream))
