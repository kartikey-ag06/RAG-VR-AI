"""Warehouse & Quality Control Compliance pack (Phase 13B).

Uses ONLY core.sdk through ctx, proving a third industry sector plugs in with zero Core changes.
Mounted at /api/v1/packs/warehouse_qc/...
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, Request
from fastapi.responses import FileResponse, JSONResponse

from core.sdk import PolicyDenied

DASHBOARD = Path(__file__).parent / "static" / "dashboard.html"
NO_CACHE = {"Cache-Control": "no-store"}

QUARANTINE_SCOPE = "warehouse_qc:quarantine"
LOTS_KIND = "lots"
TELEMETRY_KIND = "qc_telemetry"
TELEMETRY_ID = "questions"
AUDIT_STREAM = "audit"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_lot(lot_id: Any) -> str:
    return str(lot_id or "").strip().upper()[:64]


def _actor_label(ctx, request: Optional[Request]) -> str:
    if request is None:
        return "agent:approved"
    p = ctx.principal(request)
    return f"{p.kind}:{p.name or p.id or 'anonymous'}"


def register(ctx):
    @ctx.events.subscribe("question.asked")
    def on_question(event):
        q = str((event.data or {}).get("question") or "").strip()[:240]
        if not q:
            return
        cur = ctx.kv.get(event.tenant_id, TELEMETRY_KIND, TELEMETRY_ID) or {"total": 0, "counts": {}}
        counts = dict(cur.get("counts") or {})
        key = q.lower()
        row = dict(counts.get(key) or {"question": q, "count": 0})
        row["count"] = int(row.get("count") or 0) + 1
        row["last_ts"] = _now_iso()
        counts[key] = row
        if len(counts) > 200:
            top_items = sorted(counts.items(), key=lambda kv: -int(kv[1].get("count") or 0))[:200]
            counts = dict(top_items)
        ctx.kv.put(
            event.tenant_id,
            TELEMETRY_KIND,
            TELEMETRY_ID,
            {"total": int(cur.get("total") or 0) + 1, "counts": counts},
        )

    def _lookup_lot(rot, lot_id: str) -> dict:
        lid = _norm_lot(lot_id)
        if not lid:
            raise PolicyDenied("lot_id is required", 400)
        rec = ctx.kv.get(rot, LOTS_KIND, lid)
        if rec is None:
            return {
                "lot_id": lid,
                "status": "active",
                "reason": "",
                "severity": "",
                "actor": "",
                "updated_at": None,
                "notes": [],
            }
        return rec

    def _summary(rot) -> dict:
        lots = ctx.kv.list(rot, LOTS_KIND, limit=500)
        quarantined = [
            {
                "lot_id": r.get("lot_id"),
                "reason": r.get("reason", ""),
                "severity": r.get("severity", "high"),
                "actor": r.get("actor", ""),
                "updated_at": r.get("updated_at"),
            }
            for r in lots
            if isinstance(r, dict) and r.get("status") == "quarantined"
        ]
        telem = ctx.kv.get(rot, TELEMETRY_KIND, TELEMETRY_ID) or {"total": 0, "counts": {}}
        top_q = sorted(
            (telem.get("counts") or {}).values(),
            key=lambda x: -int(x.get("count") or 0),
        )[:10]
        return {
            "quarantined_count": len(quarantined),
            "quarantined_lots": quarantined,
            "total_questions": int(telem.get("total") or 0),
            "top_questions": top_q,
        }

    # ------------------------------------------------------------ REST routes (/api/v1/packs/warehouse_qc/...)
    @ctx.router.get("/dashboard")
    def dashboard():
        return FileResponse(DASHBOARD, headers=NO_CACHE)

    @ctx.router.get("/lots")
    def list_lots(request: Request):
        ctx.require(request, "knowledge:read")
        lots = [r for r in ctx.kv.list(request, LOTS_KIND, limit=500) if isinstance(r, dict)]
        return {"lots": lots}

    @ctx.router.get("/summary")
    def get_summary(request: Request):
        ctx.require(request, "knowledge:read")
        return _summary(request)

    @ctx.router.post("/lots/{lot_id}/release")
    def release_lot(lot_id: str, request: Request, body: Any = Body(None)):
        ctx.require(request, QUARANTINE_SCOPE)
        lid = _norm_lot(lot_id)
        existing = ctx.kv.get(request, LOTS_KIND, lid)
        if existing is None:
            return JSONResponse({"error": "lot not found"}, 404)
        note = str((body or {}).get("note") or "released after QC review").strip()[:300]
        actor = _actor_label(ctx, request)
        ts = time.time()
        notes = list(existing.get("notes") or [])
        notes.append({"ts": _now_iso(), "actor": actor, "action": "released", "note": note})
        updated = {
            **existing,
            "lot_id": lid,
            "status": "active",
            "actor": actor,
            "updated_at": ts,
            "notes": notes[-20:],
        }
        ctx.kv.put(request, LOTS_KIND, lid, updated)
        ctx.journal.append(
            request,
            AUDIT_STREAM,
            {"action": "warehouse_qc.lot_released", "lot_id": lid, "actor": actor, "note": note, "ts": _now_iso()},
        )
        ctx.events.publish(request, "warehouse_qc.lot_released", {"lot_id": lid, "actor": actor, "note": note})
        return updated

    # ------------------------------------------------------------ MCP & Agent tools
    @ctx.mcp_tool("warehouse_qc_lookup_lot")
    def tool_lookup_lot(request, args):
        if request is not None:
            ctx.require(request, "knowledge:read")
        else:
            ctx.check_enabled(None)
        lid = str((args or {}).get("lot_id") or "")
        return json.dumps(_lookup_lot(request, lid), ensure_ascii=False)

    @ctx.mcp_tool("warehouse_qc_summary")
    def tool_summary(request, _args):
        if request is not None:
            ctx.require(request, "knowledge:read")
        else:
            ctx.check_enabled(None)
        return json.dumps(_summary(request), ensure_ascii=False)

    @ctx.mcp_tool("warehouse_qc_quarantine_lot")
    def tool_quarantine_lot(request, args):
        if request is not None:
            ctx.require(request, QUARANTINE_SCOPE)
        else:
            ctx.check_enabled(None)
        a = args or {}
        lid = _norm_lot(a.get("lot_id"))
        reason = str(a.get("reason") or "").strip()[:300]
        severity = str(a.get("severity") or "high").strip().lower()[:32] or "high"
        if not lid:
            raise PolicyDenied("lot_id is required", 400)
        if not reason:
            raise PolicyDenied("reason is required", 400)
        actor = _actor_label(ctx, request)
        ts = time.time()
        existing = ctx.kv.get(request, LOTS_KIND, lid) or {}
        notes = list(existing.get("notes") or [])
        notes.append({"ts": _now_iso(), "actor": actor, "action": "quarantined", "note": reason})
        rec = {
            "lot_id": lid,
            "status": "quarantined",
            "reason": reason,
            "severity": severity,
            "actor": actor,
            "updated_at": ts,
            "notes": notes[-20:],
        }
        ctx.kv.put(request, LOTS_KIND, lid, rec)
        ctx.journal.append(
            request,
            AUDIT_STREAM,
            {
                "action": "warehouse_qc.lot_quarantined",
                "lot_id": lid,
                "reason": reason,
                "severity": severity,
                "actor": actor,
                "ts": _now_iso(),
            },
        )
        ctx.events.publish(
            request,
            "warehouse_qc.lot_quarantined",
            {"lot_id": lid, "reason": reason, "severity": severity, "actor": actor},
        )
        return json.dumps(rec, ensure_ascii=False)
