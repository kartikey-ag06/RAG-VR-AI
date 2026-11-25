"""EventBus - in-process publish/subscribe (Phase 5, plan section 5).

The Core announces what happened ("question.asked", "document.deleted", ...); modules that declared the event in
their manifest's `subscribes` get it. This removes client -> pack coupling: the glasses app no longer has to tell
a pack about every question, the Core does.

Rules:
- Publishing never blocks or breaks the caller: events go into a queue and ONE background thread delivers them.
  If the queue is full the event is dropped and counted (an answer must never wait for a listener).
- A listener that raises is logged and counted; other listeners still get the event.
- A listener only gets events of tenants for which its module is switched ON, and runs with that tenant set,
  so its data reads and writes land in the right tenant.
One process only. With more than one server instance, swap this for Redis Streams or a queue (same interface).
"""
import logging
import queue
import threading
import time
from collections import deque
from typing import Callable, Dict, List, Optional, Tuple

from core.data.tenancy import use_tenant
from core.sdk.events import Event, check_event_name

CORE = "core"


class EventBus:
    def __init__(self, enabled: Callable[[str, str], bool] = lambda module_id, tenant_id: True,
                 log: Optional[logging.Logger] = None, max_queue: int = 10000, keep_recent: int = 300):
        self.enabled, self.log = enabled, log or logging.getLogger("vr_rag.events")
        self._subs: Dict[str, List[Tuple[str, Callable]]] = {}
        self._q: "queue.Queue[Event]" = queue.Queue(max_queue)
        self._recent = deque(maxlen=keep_recent)
        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
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

    def publish(self, event: Event) -> Event:
        self.stats["published"] += 1
        self._recent.appendleft(event)
        try:
            self._q.put_nowait(event)
        except queue.Full:
            self.stats["dropped"] += 1
            self.log.warning("event queue full - dropped %s", event.name)
            return event
        self._start()
        return event

    def recent(self, tenant_id: Optional[str] = None, limit: int = 50) -> list:
        out = [e.to_dict() for e in list(self._recent) if tenant_id in (None, e.tenant_id)]
        return out[:max(1, min(300, limit))]

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued event was delivered (tests, shutdown). True if the queue emptied in time."""
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
        while True:
            event = self._q.get()
            try:
                self.deliver(event)
            finally:
                self._q.task_done()

    def deliver(self, event: Event) -> None:
        with self._lock:
            subs = list(self._subs.get(event.name, ()))
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
                self.stats["failed"] += 1
                self.errors[module_id] = {"event": event.name, "error": f"{type(e).__name__}: {e}"[:300],
                                          "ts": time.time()}
                self.log.warning("listener %s failed on %s: %s", module_id, event.name, e)
