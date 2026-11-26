"""session_state.py - thread-safe, tenant-scoped TTL+LRU session state for /api/v1/scan."""
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


def _ttl_seconds() -> int:
    try:
        return max(1, int(os.environ.get("VR_RAG_SCAN_SESSION_TTL", "900")))
    except ValueError:
        return 900


@dataclass
class ScanSession:
    session_id: str
    last_dhash: Optional[int] = None
    last_vision_summary: Optional[Dict[str, Any]] = None
    active_entity: str = ""
    last_question: str = ""
    last_answer: str = ""
    last_mode: str = ""
    last_confidence: Optional[float] = None
    last_citations: List[Dict[str, Any]] = field(default_factory=list)
    recent_turns: List[Dict[str, str]] = field(default_factory=list)
    turn_count: int = 0
    updated_at: float = field(default_factory=time.time)


class ScanSessionStore:
    """In-memory TTL + LRU cache keyed by (identity_key, session_id)."""

    def __init__(self, max_sessions: int = 256, ttl_seconds: Optional[int] = None):
        self.max_sessions = max_sessions
        self._ttl_override = ttl_seconds
        self._lock = threading.Lock()
        self._sessions: OrderedDict[Tuple[str, str], ScanSession] = OrderedDict()

    def _ttl(self) -> int:
        return self._ttl_override if self._ttl_override is not None else _ttl_seconds()

    def _purge_expired(self, now: float) -> None:
        cutoff = now - self._ttl()
        expired = [k for k, s in self._sessions.items() if s.updated_at < cutoff]
        for k in expired:
            self._sessions.pop(k, None)

    def get_session(self, identity_key: str, session_id: str) -> Optional[ScanSession]:
        key = (identity_key, session_id)
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            sess = self._sessions.get(key)
            if sess is None:
                return None
            self._sessions.move_to_end(key)
            return sess

    def upsert_session(
        self,
        identity_key: str,
        session_id: str,
        *,
        last_dhash: Optional[int] = None,
        last_vision_summary: Optional[Dict[str, Any]] = None,
        active_entity: Optional[str] = None,
        question: Optional[str] = None,
        answer: Optional[str] = None,
        mode: Optional[str] = None,
        confidence: Optional[float] = None,
        citations: Optional[List[Dict[str, Any]]] = None,
    ) -> ScanSession:
        key = (identity_key, session_id)
        now = time.time()
        with self._lock:
            self._purge_expired(now)
            sess = self._sessions.get(key)
            if sess is None:
                sess = ScanSession(session_id=session_id)
                self._sessions[key] = sess
            else:
                self._sessions.move_to_end(key)

            if last_dhash is not None:
                sess.last_dhash = last_dhash
            if last_vision_summary is not None:
                sess.last_vision_summary = dict(last_vision_summary)
            if active_entity is not None:
                sess.active_entity = active_entity
            if question is not None:
                sess.last_question = question
            if answer is not None:
                sess.last_answer = answer
            if mode is not None:
                sess.last_mode = mode
            if confidence is not None:
                sess.last_confidence = confidence
            if citations is not None:
                sess.last_citations = list(citations)

            if question is not None or answer is not None:
                sess.turn_count += 1
                sess.recent_turns.append({
                    "q": str(question or "")[:100],
                    "a": str(answer or "")[:120],
                    "entity": sess.active_entity[:60],
                })
                sess.recent_turns = sess.recent_turns[-4:]

            sess.updated_at = now
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
            return sess

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


SCAN_SESSIONS = ScanSessionStore()


def get_session(identity_key: str, session_id: str) -> Optional[ScanSession]:
    return SCAN_SESSIONS.get_session(identity_key, session_id)


def upsert_session(identity_key: str, session_id: str, **kwargs) -> ScanSession:
    return SCAN_SESSIONS.upsert_session(identity_key, session_id, **kwargs)


def build_context_hint(session: Optional[ScanSession]) -> str:
    """Compact <= 300-char context summary for follow-up RAG queries."""
    if session is None:
        return ""
    parts = []
    if session.active_entity:
        parts.append(f"Entity: {session.active_entity[:80]}")
    if session.last_vision_summary:
        txt = (session.last_vision_summary.get("text_seen") or "").strip()
        if txt:
            parts.append(f"Seen: {txt[:80]}")
    if session.recent_turns:
        last = session.recent_turns[-1]
        if last.get("q"):
            parts.append(f"Prev Q: {last['q'][:70]}")
    return " | ".join(parts)[:300]


def entity_state_dict(session: Optional[ScanSession]) -> dict:
    if session is None:
        return {"active_entity": "", "turn_count": 0, "recent_turns": 0}
    return {
        "active_entity": session.active_entity,
        "turn_count": session.turn_count,
        "recent_turns": len(session.recent_turns),
    }
