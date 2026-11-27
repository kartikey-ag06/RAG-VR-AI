"""session_state.py - thread-safe, tenant-scoped TTL+LRU session state for /api/v1/scan (Phases 8 & 11C)."""
import hashlib
import os
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
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

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ScanSession":
        return cls(
            session_id=str(data.get("session_id") or ""),
            last_dhash=data.get("last_dhash"),
            last_vision_summary=data.get("last_vision_summary"),
            active_entity=str(data.get("active_entity") or ""),
            last_question=str(data.get("last_question") or ""),
            last_answer=str(data.get("last_answer") or ""),
            last_mode=str(data.get("last_mode") or ""),
            last_confidence=data.get("last_confidence"),
            last_citations=list(data.get("last_citations") or []),
            recent_turns=list(data.get("recent_turns") or []),
            turn_count=int(data.get("turn_count") or 0),
            updated_at=float(data.get("updated_at") or time.time()),
        )


class ScanSessionStore:
    """TTL + LRU cache keyed by (identity_key, session_id); backed by shared KV when provided."""

    def __init__(self, max_sessions: int = 256, ttl_seconds: Optional[int] = None, kv=None):
        self.max_sessions = max_sessions
        self._ttl_override = ttl_seconds
        self.kv = kv
        self._lock = threading.Lock()
        self._sessions: OrderedDict[Tuple[str, str], ScanSession] = OrderedDict()

    def set_kv(self, kv) -> None:
        with self._lock:
            self.kv = kv

    def _ttl(self) -> int:
        return self._ttl_override if self._ttl_override is not None else _ttl_seconds()

    @staticmethod
    def _kv_id(identity_key: str, session_id: str) -> str:
        return hashlib.sha256(f"{identity_key}:{session_id}".encode()).hexdigest()[:32]

    def _kv_get(self, identity_key: str, session_id: str) -> Optional[dict]:
        kid = self._kv_id(identity_key, session_id)
        if hasattr(self.kv, "kv_get"):
            return self.kv.kv_get("scan_session", kid)
        return self.kv.get(f"scan_session:{kid}")

    def _kv_put(self, identity_key: str, session_id: str, data: dict) -> None:
        kid = self._kv_id(identity_key, session_id)
        if hasattr(self.kv, "kv_put"):
            self.kv.kv_put("scan_session", kid, data)
        else:
            self.kv.set(f"scan_session:{kid}", data)

    def _kv_delete(self, identity_key: str, session_id: str) -> None:
        kid = self._kv_id(identity_key, session_id)
        if hasattr(self.kv, "kv_delete"):
            self.kv.kv_delete("scan_session", kid)
        elif hasattr(self.kv, "delete"):
            self.kv.delete(f"scan_session:{kid}")

    def _purge_expired(self, now: float) -> None:
        cutoff = now - self._ttl()
        expired = [k for k, s in self._sessions.items() if s.updated_at < cutoff]
        for k in expired:
            self._sessions.pop(k, None)

    def get_session(self, identity_key: str, session_id: str) -> Optional[ScanSession]:
        now = time.time()
        if self.kv is not None:
            raw = self._kv_get(identity_key, session_id)
            if not raw:
                return None
            sess = ScanSession.from_dict(raw)
            if sess.updated_at < now - self._ttl():
                self._kv_delete(identity_key, session_id)
                return None
            return sess

        key = (identity_key, session_id)
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
        now = time.time()
        with self._lock:
            if self.kv is not None:
                raw = self._kv_get(identity_key, session_id)
                sess = ScanSession.from_dict(raw) if raw else ScanSession(session_id=session_id)
            else:
                self._purge_expired(now)
                key = (identity_key, session_id)
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
            if self.kv is not None:
                self._kv_put(identity_key, session_id, sess.to_dict())
            else:
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
