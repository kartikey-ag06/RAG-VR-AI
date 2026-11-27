"""AILedger - DB-backed per-tenant call, token, and USD cost ledger (Phase 11B).

Uses atomic Postgres SQL (ai_usage_daily + ai_ledger) when `db` is configured, and falls back
to a thread-safe in-memory store when `db is None` (offline tests / VR_RAG_NO_DB=1).
"""
import threading
import time
from typing import Dict, List, Optional, Tuple

from core.sdk.context import BudgetExceeded

# Approximate per-1M-token USD pricing: (prompt_per_1m, completion_per_1m)
PRICING_PER_1M: Dict[str, Tuple[float, float]] = {
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
    "openai.gpt-oss-120b": (0.15, 0.60),
    "openai.gpt-oss-20b": (0.075, 0.30),
    "meta-llama/llama-4-scout-17b-16e-instruct": (0.11, 0.34),
    "anthropic.claude-3-5-sonnet": (3.00, 15.00),
    "amazon.nova-pro": (0.80, 3.20),
    "amazon.nova-lite": (0.06, 0.24),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "grok-4.7": (2.00, 10.00),
    "whisper-large-v3-turbo": (0.04, 0.04),
}
DEFAULT_PRICING = (1.00, 3.00)


def estimate_cost_usd(provider: str, model: str, prompt_tokens: int, completion_tokens: int) -> float:
    m = (model or "").strip().lower()
    in_rate, out_rate = DEFAULT_PRICING
    for k, rates in PRICING_PER_1M.items():
        if k in m:
            in_rate, out_rate = rates
            break
    pt = max(0, int(prompt_tokens or 0))
    ct = max(0, int(completion_tokens or 0))
    return round((pt * in_rate + ct * out_rate) / 1_000_000.0, 8)


def _today_utc() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


class AILedger:
    def __init__(self, db=None):
        self.db = db
        self._lock = threading.Lock()
        self._daily: Dict[Tuple[str, str], dict] = {}
        self._rows: List[dict] = []

    def clear_memory(self) -> None:
        with self._lock:
            self._daily.clear()
            self._rows.clear()

    def reserve_call(self, tenant_id: str, day: Optional[str], call_limit: int, cost_limit_usd: float = 0.0) -> int:
        day = day or _today_utc()
        call_limit = int(call_limit or 0)
        cost_limit_usd = float(cost_limit_usd or 0.0)

        if self.db is not None:
            conds = []
            if call_limit > 0:
                conds.append("ai_usage_daily.calls < %s")
            if cost_limit_usd > 0.0:
                conds.append("ai_usage_daily.cost_usd < %s")
            where_sql = f" WHERE {' AND '.join(conds)}" if conds else ""
            sql = (
                "INSERT INTO ai_usage_daily(tenant_id, day, calls) VALUES (%s, %s::date, 1) "
                "ON CONFLICT (tenant_id, day) DO UPDATE SET calls = ai_usage_daily.calls + 1"
                f"{where_sql} RETURNING calls"
            )
            args = [tenant_id, day]
            if call_limit > 0:
                args.append(call_limit)
            if cost_limit_usd > 0.0:
                args.append(cost_limit_usd)
            rows = self.db.q(sql, tuple(args))
            if not rows:
                raise BudgetExceeded(
                    f"this organisation's daily AI limit ({call_limit or cost_limit_usd}) is used up - it resets at 00:00 UTC"
                )
            return int(rows[0][0])

        with self._lock:
            key = (tenant_id, day)
            rec = self._daily.setdefault(key, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0})
            if call_limit > 0 and rec["calls"] >= call_limit:
                raise BudgetExceeded(
                    f"this organisation's daily AI limit ({call_limit} calls) is used up - it resets at 00:00 UTC"
                )
            if cost_limit_usd > 0.0 and rec["cost_usd"] >= cost_limit_usd:
                raise BudgetExceeded(
                    f"this organisation's daily AI cost limit (${cost_limit_usd:.2f}) is used up - it resets at 00:00 UTC"
                )
            rec["calls"] += 1
            for old in [k for k in self._daily if k[1] != day]:
                self._daily.pop(old, None)
            return rec["calls"]

    def record_usage(
        self,
        tenant_id: str,
        day: Optional[str] = None,
        *,
        provider: str,
        model: str,
        capability: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
    ) -> float:
        day = day or _today_utc()
        pt = max(0, int(prompt_tokens or 0))
        ct = max(0, int(completion_tokens or 0))
        cost = estimate_cost_usd(provider, model, pt, ct)

        if self.db is not None:
            self.db.x(
                "INSERT INTO ai_usage_daily(tenant_id, day, calls, prompt_tokens, completion_tokens, cost_usd) "
                "VALUES (%s, %s::date, 0, %s, %s, %s) "
                "ON CONFLICT (tenant_id, day) DO UPDATE SET "
                "prompt_tokens = ai_usage_daily.prompt_tokens + EXCLUDED.prompt_tokens, "
                "completion_tokens = ai_usage_daily.completion_tokens + EXCLUDED.completion_tokens, "
                "cost_usd = ai_usage_daily.cost_usd + EXCLUDED.cost_usd",
                (tenant_id, day, pt, ct, cost),
            )
            self.db.x(
                "INSERT INTO ai_ledger(tenant_id, provider, model, capability, prompt_tokens, completion_tokens, cost_usd) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (tenant_id, provider, model, capability, pt, ct, cost),
            )
            return cost

        with self._lock:
            rec = self._daily.setdefault(
                (tenant_id, day), {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
            )
            rec["prompt_tokens"] += pt
            rec["completion_tokens"] += ct
            rec["cost_usd"] = round(rec["cost_usd"] + cost, 8)
            self._rows.append({
                "ts": time.time(),
                "tenant_id": tenant_id,
                "provider": provider,
                "model": model,
                "capability": capability,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "cost_usd": cost,
            })
            if len(self._rows) > 1000:
                self._rows = self._rows[-1000:]
        return cost

    def daily_summary(self, tenant_id: str, day: Optional[str] = None) -> dict:
        day = day or _today_utc()
        if self.db is not None:
            rows = self.db.q(
                "SELECT calls, prompt_tokens, completion_tokens, cost_usd FROM ai_usage_daily "
                "WHERE tenant_id = %s AND day = %s::date",
                (tenant_id, day),
            )
            if not rows:
                return {"day": day, "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_usd": 0.0}
            c, pt, ct, cost = rows[0]
            return {
                "day": day,
                "calls": int(c or 0),
                "prompt_tokens": int(pt or 0),
                "completion_tokens": int(ct or 0),
                "cost_usd": round(float(cost or 0.0), 8),
            }

        with self._lock:
            rec = self._daily.get((tenant_id, day)) or {}
            return {
                "day": day,
                "calls": int(rec.get("calls", 0)),
                "prompt_tokens": int(rec.get("prompt_tokens", 0)),
                "completion_tokens": int(rec.get("completion_tokens", 0)),
                "cost_usd": round(float(rec.get("cost_usd", 0.0)), 8),
            }
