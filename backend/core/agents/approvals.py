"""ApprovalStore - tenant-scoped, single-use human approval records (Phase 12)."""
import secrets
import threading
import time
from typing import Any, Dict, Optional

from core.data.tenancy import use_tenant


class ApprovalConflict(Exception):
    """Raised when an approval is already resolved or expired."""


class ApprovalStore:
    KIND = "agent_approval"

    def __init__(self, kv=None, ttl_seconds: int = 900):
        self.kv = kv
        self.ttl_seconds = max(1, int(ttl_seconds))
        self._lock = threading.Lock()
        self._mem: Dict[str, dict] = {}

    def _get_raw(self, approval_id: str, tenant_id: str) -> Optional[dict]:
        if self.kv is not None:
            with use_tenant(tenant_id):
                return self.kv.kv_get(self.KIND, approval_id)
        return self._mem.get(f"{tenant_id}:{approval_id}")

    def _put_raw(self, approval_id: str, tenant_id: str, rec: dict) -> None:
        if self.kv is not None:
            with use_tenant(tenant_id):
                self.kv.kv_put(self.KIND, approval_id, rec)
        else:
            self._mem[f"{tenant_id}:{approval_id}"] = dict(rec)

    def create(
        self,
        *,
        tenant_id: str,
        actor: str,
        tool_name: str,
        module_id: Optional[str],
        required_scope: str,
        args: dict,
        messages: list,
        steps: list,
        step_count: int,
        max_steps: int,
        tool_call_id: str = "",
    ) -> dict:
        now = time.time()
        aid = f"apr_{secrets.token_hex(8)}"
        rec = {
            "approval_id": aid,
            "tenant_id": tenant_id,
            "actor": actor,
            "tool_name": tool_name,
            "module_id": module_id,
            "required_scope": required_scope,
            "args": dict(args or {}),
            "messages": list(messages),
            "steps": list(steps),
            "step_count": int(step_count),
            "max_steps": int(max_steps),
            "tool_call_id": tool_call_id,
            "status": "pending",
            "created_at": now,
            "expires_at": now + self.ttl_seconds,
        }
        with self._lock:
            self._put_raw(aid, tenant_id, rec)
        return rec

    def get(self, approval_id: str, tenant_id: str) -> Optional[dict]:
        with self._lock:
            rec = self._get_raw(approval_id, tenant_id)
            if not rec or rec.get("tenant_id") != tenant_id:
                return None
            if rec["status"] == "pending" and time.time() > float(rec.get("expires_at", 0)):
                rec = dict(rec, status="expired")
                self._put_raw(approval_id, tenant_id, rec)
            return dict(rec)

    def transition(self, approval_id: str, tenant_id: str, new_status: str) -> dict:
        with self._lock:
            rec = self._get_raw(approval_id, tenant_id)
            if not rec or rec.get("tenant_id") != tenant_id:
                raise LookupError("approval not found")
            if rec["status"] == "pending" and time.time() > float(rec.get("expires_at", 0)):
                rec = dict(rec, status="expired")
                self._put_raw(approval_id, tenant_id, rec)
                raise ApprovalConflict("approval expired")
            if rec["status"] != "pending":
                raise ApprovalConflict(f"approval already {rec['status']}")
            updated = dict(rec, status=new_status, resolved_at=time.time())
            self._put_raw(approval_id, tenant_id, updated)
            return updated
