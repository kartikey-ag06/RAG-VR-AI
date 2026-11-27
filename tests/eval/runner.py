"""Phase 9 eval harness: scores citation recall, format compliance, fact hit rate, and latency.

Usage (live comparison):
    python -m tests.eval.runner --provider groq
    python -m tests.eval.runner --provider bedrock --region us-east-1
    python -m tests.eval.runner --mock
"""
import argparse
import json
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

QUESTIONS_FILE = Path(__file__).resolve().parent / "questions.json"


def load_questions(path: Path = QUESTIONS_FILE) -> list:
    return json.loads(path.read_text("utf-8"))


def check_format(answer: str, style: str, mode: str) -> bool:
    txt = (answer or "").strip()
    if not txt or mode not in ("pdf", "general", "offline"):
        return False
    words = len(txt.split())
    if style == "spoken":
        return words <= 35
    if style == "glance":
        lines = [l for l in txt.splitlines() if l.strip()]
        return len(lines) <= 4 and words <= 65
    return words <= 180


def score_item(item: dict, out: dict, latency_ms: float) -> dict:
    answer = str(out.get("answer") or "")
    mode = str(out.get("mode") or "")
    cites = out.get("citations") or []
    cited_pages = {c.get("page") for c in cites if isinstance(c, dict)}
    exp_pages = set(item.get("expected_pages") or [])
    cit_recall = 1.0 if (not exp_pages or bool(cited_pages & exp_pages)) else 0.0
    terms = [t.lower() for t in (item.get("required_terms") or [])]
    low = answer.lower()
    fact_hit = (sum(1 for t in terms if t in low) / len(terms)) if terms else 1.0
    fmt_ok = check_format(answer, item.get("style", "full"), mode)
    return {
        "id": item["id"],
        "style": item.get("style", "full"),
        "mode": mode,
        "citation_recall": cit_recall,
        "format_ok": fmt_ok,
        "fact_hit_rate": round(fact_hit, 3),
        "latency_ms": round(latency_ms, 1),
    }


def evaluate(ask_fn: Callable[[str, str], dict], items: Optional[List[dict]] = None, provider: str = "mock") -> dict:
    rows = items if items is not None else load_questions()
    scored = []
    for item in rows:
        t0 = time.perf_counter()
        out = ask_fn(item["question"], item.get("style", "full"))
        ms = (time.perf_counter() - t0) * 1000
        scored.append(score_item(item, out, ms))
    n = max(1, len(scored))
    return {
        "provider": provider,
        "count": len(scored),
        "citation_recall": round(sum(r["citation_recall"] for r in scored) / n, 3),
        "format_compliance": round(sum(1 for r in scored if r["format_ok"]) / n, 3),
        "fact_hit_rate": round(sum(r["fact_hit_rate"] for r in scored) / n, 3),
        "avg_latency_ms": round(sum(r["latency_ms"] for r in scored) / n, 1),
        "items": scored,
    }


def _mock_ask(question: str, style: str) -> dict:
    text = {
        "spoken": "The SEBI order imposed a penalty under Section 15H.",
        "glance": "1. SEBI order on Reliance Industries.\n2. Penalty under Section 15H.\n3. EPS and trading window.",
    }.get(style, "The SEBI adjudication order on Reliance Industries discusses EPS, trading window, court rulings, abate proceedings, and Section 15H penalty.")
    return {
        "answer": text,
        "mode": "pdf",
        "citations": [{"n": 1, "page": 31}, {"n": 2, "page": 1}, {"n": 3, "page": 11}],
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Run Phase 9 RAG evaluation harness.")
    ap.add_argument("--provider", default="groq", choices=("groq", "grok", "bedrock", "foundry"))
    ap.add_argument("--region", default="")
    ap.add_argument("--mock", action="store_true", help="Run offline with deterministic mock answers")
    args = ap.parse_args(argv)

    if args.mock:
        print(json.dumps(evaluate(_mock_ask, provider="mock"), indent=2))
        return 0

    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "backend"))
    import app as backend  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    backend.CONFIG.set("ai.provider", args.provider, "default")
    if args.region:
        backend.CONFIG.set("ai.region", args.region, "default")
    client = TestClient(backend.app)

    def ask_live(q: str, style: str) -> dict:
        return client.post("/api/v1/ask", json={"question": q, "style": style}).json()

    print(json.dumps(evaluate(ask_live, provider=args.provider), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
