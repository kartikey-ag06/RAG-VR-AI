"""ProcOps logic without any web code: question log entries -> friction heatmap + knowledge-gap analytics.

"DevOps for physical work": every job done is a test run of the manual. Each question a worker asks while on a
page is a bug report for that page. The heatmap shows which pages cause the most questions since their last fix.
"""
import json
import re
from collections import defaultdict
from datetime import datetime, timezone

TAG_PREFIX = re.compile(r"^\s*\[[^\]]{0,80}\]\s*")     # "[live ai: pump] how do I..." (older glasses builds)


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def norm_q(q):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", str(q).lower())).strip()


def dedupe_key(q):
    return norm_q(TAG_PREFIX.sub("", str(q)))


def norm_code(x) -> str:
    """'PUMP-7731 ' -> 'pump7731': machine tags compare without case, spaces or punctuation."""
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def parse_json(text):
    """The model is asked for JSON only; this also survives ```json fences or a short preface."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


def next_version(v):
    major, minor = (int(x) for x in v.split("."))
    return f"{major}.{minor + 1}"


def entry_from_event(data: dict) -> dict:
    """A Core 'question.asked' event -> one question-log entry. The page is the first page the answer cited."""
    ev = {"ts": now_iso(), "q": str(data.get("question", ""))[:500], "mode": str(data.get("mode") or "")[:10],
          "door": str(data.get("door") or "")[:40], "source": "server", "hits": len(data.get("candidates") or [])}
    if data.get("confidence") is not None:
        try:
            ev["conf"] = round(float(data["confidence"]), 3)
        except (TypeError, ValueError):
            pass
    if data.get("tag"):
        ev["machine"] = str(data["tag"])[:80]
    cited = data.get("cited") or []
    if cited and cited[0].get("doc_uid") and int(cited[0].get("page") or 0) >= 1:
        ev.update(uid=str(cited[0]["doc_uid"]), page=int(cited[0]["page"]))
    return ev


def summarize(events, docs, revisions_of, low_confidence=0.62):
    """events: question-log entries; docs: visible documents; revisions_of(uid) -> {version, pages, history}."""
    by_page = defaultdict(list)
    gaps = defaultdict(int)
    for ev in events:
        if ev.get("uid"):
            by_page[(ev["uid"], ev["page"])].append(ev)
        else:
            gaps[ev["q"]] += 1
    out = []
    for e in docs:
        revs = revisions_of(e["uid"]) or {"version": "1.0", "pages": {}, "history": []}
        pages, total_open = [], 0
        for p in range(1, (e.get("pages") or 0) + 1):
            evs = by_page.get((e["uid"], p), [])
            rv = revs["pages"].get(str(p))
            since = rv["approved"] if rv else ""
            open_evs = [x for x in evs if x["ts"] > since]
            qs = defaultdict(lambda: {"n": 0, "q": "", "last": ""})
            for x in open_evs:
                k = qs[norm_q(x["q"])]
                k["n"] += 1
                k["q"] = x["q"]
                k["last"] = x["ts"]
            if evs or rv:
                pages.append({"page": p, "open": len(open_evs), "before": len(evs) - len(open_evs),
                              "distinct": len(qs), "rev": rv["version"] if rv else None,
                              "questions": sorted(qs.values(), key=lambda k: (-k["n"], k["last"]))[:25]})
            total_open += len(open_evs)
        out.append({"uid": e["uid"], "title": e["title"], "type": e["type"], "pages": e["pages"],
                    "version": revs["version"], "open": total_open, "hot": pages, "history": revs["history"][::-1]})
    out.sort(key=lambda d: (-d["open"], -len(d["history"]), d["title"]))
    most = defaultdict(lambda: {"n": 0, "q": ""})
    low, zero = [], defaultdict(int)
    for ev in events:
        k = most[norm_q(ev["q"])]
        k["n"] += 1
        k["q"] = ev["q"]
        if ev.get("hits") == 0 or (ev.get("mode") in ("general", "offline") and not ev.get("uid")):
            zero[ev["q"]] += 1
        elif ev.get("conf") is not None and ev["conf"] < low_confidence:
            low.append({"q": ev["q"], "conf": ev["conf"], "ts": ev["ts"], "page": ev.get("page"), "uid": ev.get("uid")})
    titles = {d["uid"]: d["title"] for d in out}
    for x in low:
        x["title"] = titles.get(x.get("uid"), "")
    analytics = {"mostAsked": sorted(most.values(), key=lambda k: -k["n"])[:15],
                 "lowConfidence": sorted(low, key=lambda x: x["conf"])[:20],
                 "zeroHit": sorted(({"q": q, "n": n} for q, n in zero.items()), key=lambda g: -g["n"])[:20]}
    return {"events": len(events), "docs": out, "analytics": analytics,
            "gaps": sorted(({"q": q, "n": n} for q, n in gaps.items()), key=lambda g: -g["n"])[:30]}


def resolve_machine(text, docs):
    """Text from a QR code / barcode / label -> (machine_id or None, its documents)."""
    tags = {t for d in docs for t in (d.get("tags") or [])}
    nt = norm_code(text)
    words = {norm_code(w) for w in re.split(r"[\s,;:/|]+", text) if len(norm_code(w)) >= 3}
    best = None
    for t in sorted(tags, key=lambda t: -len(norm_code(t))):
        n = norm_code(t)
        if n and (n in words or (len(n) >= 4 and n in nt)):
            best = t
            break
    found = [{"uid": d["uid"], "title": d["title"], "id": d["id"]} for d in docs if best in (d.get("tags") or [])] if best else []
    return best, found
