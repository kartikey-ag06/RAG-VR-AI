"""Phase 11 Two-Instance Gate: simulates Instance A and Instance B sharing Blobs, AILedger, EventQueue, KV, and DB metadata."""
import shutil
import threading
import time

import pytest

from core.ai import AIGateway, AILedger
from core.config import ConfigStore
from core.data import LocalKV
from core.data.blobs import LocalBlobStore
from core.data.tenancy import use_tenant
from core.plugins import EventBus
from core.sdk.context import BudgetExceeded
from core.sdk.events import Event
from indexer import Store
from session_state import ScanSessionStore
from tests.conftest import FakeResp


class SharedTestEventQueue:
    """Thread-safe shared queue backend with atomic claim semantics (mirrors Postgres SKIP LOCKED)."""

    def __init__(self):
        self._lock = threading.Lock()
        self._rows = []
        self._next_id = 1

    def enqueue(self, event: Event) -> None:
        with self._lock:
            self._rows.append({
                "id": self._next_id,
                "name": event.name,
                "tenant_id": event.tenant_id,
                "source": event.source,
                "data": dict(event.data),
                "status": "pending",
                "attempts": 0,
                "ts": time.time(),
            })
            self._next_id += 1

    def claim_batch(self, limit: int = 20):
        with self._lock:
            claimed = []
            for row in self._rows:
                if row["status"] == "pending":
                    row["status"] = "processing"
                    row["attempts"] += 1
                    claimed.append((
                        row["id"],
                        Event(row["name"], row["tenant_id"], dict(row["data"]), source=row["source"]),
                        row["attempts"],
                    ))
                    if len(claimed) >= limit:
                        break
            return claimed

    def ack(self, row_id: int) -> None:
        with self._lock:
            for row in self._rows:
                if row["id"] == row_id:
                    row["status"] = "done"

    def nack(self, row_id: int, attempts: int, max_attempts: int = 3) -> None:
        with self._lock:
            for row in self._rows:
                if row["id"] == row_id:
                    row["status"] = "pending" if attempts < max_attempts else "failed"

    def recent(self, tenant_id=None, limit: int = 50) -> list:
        with self._lock:
            rows = [r for r in reversed(self._rows) if tenant_id in (None, r["tenant_id"])]
            return rows[:limit]


# ---------------------------------------------------------------- 1. Shared Blobs across 2 instances
def test_gate_two_instances_shared_blobs(tmp_path):
    blobs = LocalBlobStore(tmp_path / "shared_blobs")
    inst_a = Store(tmp_path / "inst_a", blobs=blobs)
    inst_b = Store(tmp_path / "inst_b", blobs=blobs)

    entry = inst_a.add("manual.txt", b"Two-instance blob sharing verification step one. " * 20)
    uid = entry["uid"]
    t0 = time.time()
    while time.time() - t0 < 20:
        cur = inst_a.doc_entry(uid)
        if cur and cur["status"] == "ready" and blobs.exists(f"pages/{uid}/p001.jpg"):
            break
        time.sleep(0.05)

    assert not (tmp_path / "inst_b" / "pages" / uid).exists()
    jpg = inst_b.read_page_bytes(uid, 1)
    assert jpg is not None and jpg.startswith(b"\xff\xd8")
    shutil.rmtree(tmp_path, ignore_errors=True)


# ---------------------------------------------------------------- 2. Shared DB Cost Ledger across 2 instances
def test_gate_two_instances_shared_ledger(tmp_path):
    cfg = ConfigStore(LocalKV(tmp_path / "cfg"))
    cfg.declare("limits.ai_daily", "integer", 0, min=0)
    cfg.declare("ai.daily_cost_usd", "number", 0.0, min=0.0)
    cfg.declare("ai.provider", "string", "")
    cfg.declare("ai.region", "string", "")
    cfg.declare("ai.fallback", "string", "")
    cfg.declare("ai.chat_model", "string", "")
    ledger = AILedger(db=None)

    class FakeHttp:
        def get(self, *a, **kw):
            return FakeResp({"data": [{"id": "llama-3.3-70b-versatile"}]})

        def post(self, *a, **kw):
            return FakeResp({"choices": [{"message": {"content": "ok"}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}})

    gw_a = AIGateway(FakeHttp(), {"GROQ_API_KEY": "gsk_a"}, tmp_path / "ka", tmp_path / "ma", config=cfg, ledger=ledger)
    gw_b = AIGateway(FakeHttp(), {"GROQ_API_KEY": "gsk_b"}, tmp_path / "kb", tmp_path / "mb", config=cfg, ledger=ledger)

    cfg.set("limits.ai_daily", 2, "default")
    assert gw_a.complete([{"role": "user", "content": "q1"}]) == "ok"
    assert gw_b.complete([{"role": "user", "content": "q2"}]) == "ok"
    with pytest.raises(BudgetExceeded):
        gw_a.complete([{"role": "user", "content": "q3"}])
    assert ledger.daily_summary("default")["calls"] == 2


# ---------------------------------------------------------------- 3. Shared Event Queue (exactly-once claim)
def test_gate_two_instances_shared_event_queue_exactly_once():
    shared_q = SharedTestEventQueue()
    bus_a = EventBus(queue_backend=shared_q)
    bus_b = EventBus(queue_backend=shared_q)

    # Prevent background thread from racing with explicit drain_once()
    bus_a._start = lambda: None
    bus_b._start = lambda: None

    seen_a, seen_b = [], []
    bus_a.subscribe("question.asked", lambda ev: seen_a.append((ev.tenant_id, ev.data["n"])), "pack_x")
    bus_b.subscribe("question.asked", lambda ev: seen_b.append((ev.tenant_id, ev.data["n"])), "pack_x")

    for i, tenant in enumerate(("default", "acme", "default", "acme")):
        bus_a.emit("question.asked", {"n": i}, tenant_id=tenant)

    # Instance A claims up to 2, Instance B claims the remaining 2 -> zero duplicates
    assert bus_a.drain_once(limit=2) == 2
    assert bus_b.drain_once(limit=10) == 2
    assert bus_a.drain_once(limit=10) == 0

    all_seen = sorted(seen_a + seen_b)
    assert all_seen == [("acme", 1), ("acme", 3), ("default", 0), ("default", 2)]


# ---------------------------------------------------------------- 4. Shared ScanSessionStore + tenant isolation
def test_gate_two_instances_shared_scan_sessions(tmp_path):
    shared_kv = LocalKV(tmp_path / "shared_kv")
    sess_a = ScanSessionStore(kv=shared_kv)
    sess_b = ScanSessionStore(kv=shared_kv)

    with use_tenant("default"):
        sess_a.upsert_session("default:tok1", "s1", last_dhash=123456, active_entity="Pump P-101", question="q1", answer="a1")
        got = sess_b.get_session("default:tok1", "s1")
        assert got is not None and got.last_dhash == 123456 and got.active_entity == "Pump P-101"
        assert sess_b.get_session("default:other_tok", "s1") is None

    with use_tenant("acme"):
        assert sess_b.get_session("default:tok1", "s1") is None


# ---------------------------------------------------------------- 5. Store.refresh_from_db() syncs metadata
def test_gate_two_instances_store_refresh_from_db(tmp_path):
    class FakeDB:
        def __init__(self):
            self.docs = []
            self.revs = {}

        def list_docs(self):
            return list(self.docs)

        def put_doc(self, meta, data=None):
            self.docs = [d for d in self.docs if d["uid"] != meta["uid"]] + [dict(meta)]

        def doc_data(self, uid):
            raise AssertionError("refresh_from_db must not download BYTEA blobs")

        def texts(self, uid):
            return ["page 1 text"]

        def revisions(self):
            return dict(self.revs)

    db = FakeDB()
    blobs = LocalBlobStore(tmp_path / "blobs")
    store_a = Store(tmp_path / "a", db=db, blobs=blobs)
    store_b = Store(tmp_path / "b", db=db, blobs=blobs)

    entry = store_a.add("sync.txt", b"Sync metadata across instances. " * 20)
    assert store_b.doc_entry(entry["uid"]) is None

    assert store_b.refresh_from_db() is True
    assert store_b.doc_entry(entry["uid"]) is not None
    shutil.rmtree(tmp_path, ignore_errors=True)
