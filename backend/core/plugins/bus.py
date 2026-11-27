"""EventBus - in-process or Postgres SKIP LOCKED shared publish/subscribe (Phases 5 & 11C).

When `db` / `queue_backend` is None, uses an in-process queue.Queue + daemon thread.
When `db` (or a shared `EventQueueBackend`) is provided, enqueues to `core_event_queue` and
claims batches atomically with `SELECT ... FOR UPDATE SKIP LOCKED` across instances.
"""
import json
import logging
import queue
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Protocol, Tuple, runtime_checkable

from core.data.tenancy import current_tenant, use_tenant
from core.sdk.events import Event, check_event_name

CORE = "core"


@runtime_checkable
class EventQueueBackend(Protocol):
    def enqueue(self, event: Event) -> None: ...

    def claim_batch(self, limit: int = 20) -> List[Tuple[int, Event, int]]: ...

    def ack(self, row_id: int) -> None: ...

    def nack(self, row_id: int, attempts: int, max_attempts: int = 3) -> None: ...

    def recent(self, tenant_id: Optional[str] = None, limit: int = 50) -> list: ...


class PostgresEventQueue:
    """Shared multi-instance queue backed by Neon/Postgres `core_event_queue`."""

    def __init__(self, db):
        self.db = db

    def enqueue(self, event: Event) -> None:
        self.db.x(
            "INSERT INTO core_event_queue(name, tenant_id, source, data, status) VALUES (%s, %s, %s, %s, 'pending')",
            (event.name, event.tenant_id, event.source, json.dumps(event.data)),
        )

    def claim_batch(self, limit: int = 20) -> List[Tuple[int, Event, int]]:
        sql = (
            "WITH claimed AS ("
            "  SELECT id FROM core_event_queue WHERE status = 'pending' ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED"
            ") "
            "UPDATE core_event_queue q "
            "SET status = 'processing', attempts = q.attempts + 1, claimed_at = now() "
            "FROM claimed WHERE q.id = claimed.id "
            "RETURNING q.id, q.name, q.tenant_id, q.source, q.data, q.attempts"
        )
        rows = self.db.q(sql, (max(1, int(limit)),)) or []
        out = []
        for row_id, name, tenant_id, source, data, attempts in rows:
            ev = Event(name, tenant_id, dict(data or {}), source=source or "")
            out.append((int(row_id), ev, int(attempts)))
        return out

    def ack(self, row_id: int) -> None:
        self.db.x("UPDATE core_event_queue SET status = 'done' WHERE id = %s", (row_id,))

    def nack(self, row_id: int, attempts: int, max_attempts: int = 3) -> None:
        status = "pending" if attempts < max_attempts else "failed"
        self.db.x("UPDATE core_event_queue SET status = %s WHERE id = %s", (status, row_id))

    def recent(self, tenant_id: Optional[str] = None, limit: int = 50) -> list:
        lim = max(1, min(300, int(limit)))
        if tenant_id:
            rows = self.db.q(
                "SELECT id, name, tenant_id, source, data, created_at FROM core_event_queue "
                "WHERE tenant_id = %s ORDER BY id DESC LIMIT %s",
                (tenant_id, lim),
            ) or []
        else:
            rows = self.db.q(
                "SELECT id, name, tenant_id, source, data, created_at FROM core_event_queue "
                "ORDER BY id DESC LIMIT %s",
                (lim,),
            ) or []
        return [
            {
                "id": str(r[0]),
                "name": r[1],
                "tenant_id": r[2],
                "source": r[3],
                "data": dict(r[4] or {}),
                "ts": r[5].timestamp() if hasattr(r[5], "timestamp") else time.time(),
            }
            for r in rows
        ]


class EventBus:
    def __init__(
        self,
        enabled: Callable[[str, str], bool] = lambda module_id, tenant_id: True,
        log: Optional[logging.Logger] = None,
        max_queue: int = 10000,
        keep_recent: int = 300,
        db=None,
        queue_backend: Optional[EventQueueBackend] = None,
        poll_interval: float = 0.25,
    ):
        self.enabled, self.log = enabled, log or logging.getLogger("vr_rag.events")
        self._subs: Dict[str, List[Tuple[str, Callable]]] = {}
        self._q: "queue.Queue[Event]" = queue.Queue(max_queue)
        self._recent = deque(maxlen=keep_recent)
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._backend: Optional[EventQueueBackend] = (
            queue_backend if queue_backend is not None else (PostgresEventQueue(db) if db is not None else None)
        )
        self.poll_interval = poll_interval
        self.stats = {"published": 0, "delivered": 0, "failed": 0, "dropped": 0, "skipped_disabled": 0}
        self.errors: Dict[str, dict] = {}            # module id -> {"event", "error", "ts"} (last failure)

    # ------------------------------------------------------------ subscribe / publish
    def subscribe(self, name: str, handler: Callable[[Event], None], module_id: str = CORE) -> None:
        check_event_name(name)
        with self._lock:
            self._subs.setdefault(name, []).append((module_id, handler))

    def unsubscribe_module(self, module_id: str) -> None:
        with self._lock:
            for name in list(self._subs):
                self._subs[name] = [s for s in self._subs[name] if s[0] != module_id]

    def subscribers(self) -> Dict[str, List[str]]:
        with self._lock:
            return {n: [m for m, _ in subs] for n, subs in sorted(self._subs.items()) if subs}

    def emit(self, name: str, data: dict, source: str = "", tenant_id: Optional[str] = None) -> Event:
        return self.publish(Event(name, tenant_id or current_tenant(), dict(data or {}), source=source))

    def publish(self, event: Event) -> Event:
        self.stats["published"] += 1
        self._recent.appendleft(event)
        if self._backend is not None:
            try:
                self._backend.enqueue(event)
            except Exception as e:
                self.stats["dropped"] += 1
                self.log.warning("event enqueue failed for %s: %s", event.name, e)
                return event
            self._start()
            return event
        try:
            self._q.put_nowait(event)
        except queue.Full:
            self.stats["dropped"] += 1
            self.log.warning("event queue full - dropped %s", event.name)
            return event
        self._start()
        return event

    def recent(self, tenant_id: Optional[str] = None, limit: int = 50) -> list:
        if self._backend is not None:
            try:
                return self._backend.recent(tenant_id, limit)
            except Exception:
                pass
        out = [e.to_dict() for e in list(self._recent) if tenant_id in (None, e.tenant_id)]
        return out[:max(1, min(300, limit))]

    def drain_once(self, limit: int = 20) -> int:
        """Claim and deliver up to `limit` pending events from the shared queue backend."""
        if self._backend is None:
            return 0
        batch = self._backend.claim_batch(limit)
        for row_id, event, attempts in batch:
            had_error = self.deliver(event)
            if had_error:
                self._backend.nack(row_id, attempts, max_attempts=3)
            else:
                self._backend.ack(row_id)
        return len(batch)

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued event was delivered (tests, shutdown). True if the queue emptied in time."""
        if self._backend is not None:
            end = time.time() + timeout
            while time.time() < end:
                if self.drain_once(limit=50) == 0:
                    return True
            return False
        end = time.time() + timeout
        while time.time() < end:
            if self._q.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return False

    # ------------------------------------------------------------ delivery
    def _start(self):
        if self._worker and self._worker.is_alive():
            return
        with self._lock:
            if not (self._worker and self._worker.is_alive()):
                self._worker = threading.Thread(target=self._run, name="event-bus", daemon=True)
                self._worker.start()

    def _run(self):
        if self._backend is not None:
            while True:
                try:
                    n = self.drain_once(limit=20)
                except Exception as e:
                    self.log.warning("event queue poll failed: %s", e)
                    n = 0
                if n == 0:
                    time.sleep(self.poll_interval)
        while True:
            event = self._q.get()
            try:
                self.deliver(event)
            finally:
                self._q.task_done()

    def deliver(self, event: Event) -> bool:
        """Deliver `event` to matching subscribers. Returns True if any subscriber raised."""
        with self._lock:
            subs = list(self._subs.get(event.name, ()))
        had_error = False
        for module_id, handler in subs:
            if module_id == event.source:
                continue                                  # a module does not get its own events back
            if module_id != CORE and not self.enabled(module_id, event.tenant_id):
                self.stats["skipped_disabled"] += 1
                continue
            try:
                with use_tenant(event.tenant_id):
                    handler(event)
                self.stats["delivered"] += 1
            except Exception as e:                         # one bad listener must not stop the others
                had_error = True
                self.stats["failed"] += 1
                self.errors[module_id] = {"event": event.name, "error": f"{type(e).__name__}: {e}"[:300],
                                          "ts": time.time()}
                self.log.warning("listener %s failed on %s: %s", module_id, event.name, e)
        return had_error
