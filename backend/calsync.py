"""
calsync.py - show your real Google / Outlook calendar on the glasses, with no OAuth app to set up.

Both calendars publish a private "iCal / ICS" link:
  Google:  Calendar settings -> your calendar -> "Secret address in iCal format"
  Outlook: Settings -> Calendar -> Shared calendars -> Publish a calendar -> ICS link
Paste that link once (Workspace page or POST /api/calendar/sources). The server downloads it every 10 minutes,
expands repeating events (daily / weekly / monthly / yearly, with COUNT, UNTIL, BYDAY, EXDATE) and merges the
events of the asked day into /api/calendar next to your reminders.

Endpoints
  GET    /api/calendar/sources            list (links are masked)
  POST   /api/calendar/sources            {name, url, color?}
  DELETE /api/calendar/sources/{id}
  POST   /api/calendar/sources/{id}/test  download now and say how many events it has
"""
import re
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone

from fastapi import Request
from fastapi.responses import JSONResponse

from net import HTTP, JSONB

try:
    from zoneinfo import ZoneInfo
except ImportError:                     # pragma: no cover
    ZoneInfo = None

WINDOWS_TZ = {                          # Outlook writes Windows zone names into ICS files
    "India Standard Time": "Asia/Kolkata", "UTC": "UTC", "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin", "Romance Standard Time": "Europe/Paris",
    "Eastern Standard Time": "America/New_York", "Central Standard Time": "America/Chicago",
    "Mountain Standard Time": "America/Denver", "Pacific Standard Time": "America/Los_Angeles",
    "AUS Eastern Standard Time": "Australia/Sydney", "Singapore Standard Time": "Asia/Singapore",
    "Tokyo Standard Time": "Asia/Tokyo", "China Standard Time": "Asia/Shanghai", "Arabian Standard Time": "Asia/Dubai",
}
DAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def _zone(name):
    if not name or ZoneInfo is None:
        return None
    name = WINDOWS_TZ.get(name.strip('"'), name.strip('"'))
    try:
        return ZoneInfo(name)
    except Exception:
        return None


def _when(value, params):
    """ICS date/time -> (datetime aware, all_day)."""
    v = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", v):
        d = datetime.strptime(v[:8], "%Y%m%d")
        return d.replace(tzinfo=timezone.utc), True
    utc = v.endswith("Z")
    d = datetime.strptime(v.rstrip("Z")[:15], "%Y%m%dT%H%M%S")
    if utc:
        return d.replace(tzinfo=timezone.utc), False
    z = _zone(params.get("TZID"))
    return d.replace(tzinfo=z or timezone.utc), False


def _unescape(t):
    return t.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def parse_ics(text):
    """-> list of events {summary, location, start, end, all_day, rrule, exdates, recurrence_id, uid}."""
    lines = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]                       # folded line
        else:
            lines.append(raw)
    events, cur = [], None
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {"exdates": [], "summary": "(no title)", "location": ""}
            continue
        if ln == "END:VEVENT":
            if cur and "start" in cur:
                if "end" not in cur:
                    cur["end"] = cur["start"] + (timedelta(days=1) if cur["all_day"] else timedelta(hours=1))
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in ln:
            continue
        head, value = ln.split(":", 1)
        name, *ps = head.split(";")
        params = dict(p.split("=", 1) for p in ps if "=" in p)
        name = name.upper()
        try:
            if name == "DTSTART":
                cur["start"], cur["all_day"] = _when(value, params)
            elif name == "DTEND":
                cur["end"], _ = _when(value, params)
            elif name == "SUMMARY":
                cur["summary"] = _unescape(value)[:200]
            elif name == "LOCATION":
                cur["location"] = _unescape(value)[:200]
            elif name == "RRULE":
                cur["rrule"] = dict(p.split("=", 1) for p in value.split(";") if "=" in p)
            elif name == "EXDATE":
                for v in value.split(","):
                    cur["exdates"].append(_when(v, params)[0])
            elif name == "RECURRENCE-ID":
                cur["recurrence_id"] = _when(value, params)[0]
            elif name == "UID":
                cur["uid"] = value
            elif name == "STATUS" and value.upper() == "CANCELLED":
                cur["cancelled"] = True
        except ValueError:
            continue
    return events


def _occurrences(ev, lo, hi):
    """Start times of one event that fall in [lo, hi). Repeating rules are expanded (max 1500 steps)."""
    start, rr = ev["start"], ev.get("rrule")
    if not rr:
        return [start] if lo <= start < hi or (start < lo < ev["end"]) else []
    freq, interval = rr.get("FREQ", "DAILY"), max(1, int(rr.get("INTERVAL", "1") or 1))
    count = int(rr["COUNT"]) if rr.get("COUNT", "").isdigit() else None
    until = None
    if rr.get("UNTIL"):
        try:
            until = _when(rr["UNTIL"], {})[0]
        except ValueError:
            until = None
    byday = [DAYS[d[-2:]] for d in rr.get("BYDAY", "").split(",") if d[-2:] in DAYS]
    ex = {x.replace(microsecond=0) for x in ev.get("exdates", [])}
    out, n, k = [], 0, 0
    tz = start.tzinfo
    local0 = start.astimezone(tz)
    while k < 1500:
        if freq == "DAILY":
            cands = [local0 + timedelta(days=k * interval)]
        elif freq == "WEEKLY":
            week0 = local0 - timedelta(days=local0.weekday())
            wk = week0 + timedelta(weeks=k * interval)
            cands = [wk + timedelta(days=d) for d in sorted(byday or [local0.weekday()])]
        elif freq == "MONTHLY":
            m = local0.month - 1 + k * interval
            y, mo = local0.year + m // 12, m % 12 + 1
            try:
                cands = [local0.replace(year=y, month=mo)]
            except ValueError:                         # 31st in a short month
                cands = []
        elif freq == "YEARLY":
            try:
                cands = [local0.replace(year=local0.year + k * interval)]
            except ValueError:
                cands = []
        else:
            return out
        k += 1
        for c in cands:
            c = c.replace(tzinfo=tz)                    # wall-clock time stays the same across DST changes
            if c < start:
                continue
            if until and c > until:
                return out
            n += 1
            if count and n > count:
                return out
            if c >= hi:
                return out
            if c >= lo and c.replace(microsecond=0) not in ex:
                out.append(c)
    return out


class Calendars:
    def __init__(self, kv):
        self.kv, self.cache, self.lock = kv, {}, threading.Lock()

    def sources(self):
        return self.kv.kv_list("calsrc")

    def fetch(self, src, force=False):
        with self.lock:
            hit = self.cache.get(src["id"])
        if hit and not force and time.time() - hit[0] < 600:
            return hit[1]
        url = src["url"].strip()
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        r = HTTP.get(url, timeout=20, headers={"User-Agent": "VR_RAG calendar"})
        r.raise_for_status()
        r.encoding = r.encoding or "utf-8"
        evs = parse_ics(r.text)
        with self.lock:
            self.cache[src["id"]] = (time.time(), evs)
        return evs

    def events_on(self, day: date, tzinfo):
        """All events of one local day from every source, as calendar rows."""
        lo = datetime.combine(day, datetime.min.time(), tzinfo)
        hi = lo + timedelta(days=1)
        rows, errors = [], []
        for src in self.sources():
            try:
                evs = self.fetch(src)
            except Exception as e:
                errors.append(f"{src.get('name', 'calendar')}: {str(e)[:120]}")
                continue
            moved = {(e.get("uid"), e["recurrence_id"].replace(microsecond=0)) for e in evs if e.get("recurrence_id")}
            for e in evs:
                if e.get("cancelled"):
                    continue
                for s in _occurrences(e, lo, hi):
                    if not e.get("recurrence_id") and (e.get("uid"), s.replace(microsecond=0)) in moved:
                        continue                               # this repeat was edited: the edited copy is shown
                    loc = s.astimezone(tzinfo)
                    rows.append({"id": f"cal:{src['id']}:{int(s.timestamp())}", "text": e["summary"],
                                 "location": e["location"], "due": loc.isoformat(timespec="minutes"),
                                 "local": "all day" if e["all_day"] else loc.strftime("%H:%M"),
                                 "end": (e["end"] - e["start"] + s).astimezone(tzinfo).strftime("%H:%M") if not e["all_day"] else "",
                                 "all_day": e["all_day"], "source": src.get("name", "Calendar"), "calendar": True, "done": False})
        return rows, errors


def _mask(src):
    u = src.get("url", "")
    return dict(src, url=(u[:28] + "…" + u[-6:]) if len(u) > 40 else u)


def register(app, kv, auth):
    cals = Calendars(kv)

    def deny(req):
        return None if (not auth.admin_required(req) or auth.is_admin(req)) else JSONResponse({"error": "admin token required"}, 403)

    @app.get("/api/calendar/sources")
    def list_sources():
        return {"sources": [_mask(s) for s in cals.sources()]}

    @app.post("/api/calendar/sources")
    def add_source(req: Request, _body=JSONB):
        d = deny(req)
        if d:
            return d
        b = _body or {}
        url = str(b.get("url", "")).strip()
        if not re.match(r"^(https?|webcal)://", url) or len(url) > 2000:
            return JSONResponse({"error": "paste the calendar's ICS / iCal link (https://… or webcal://…)"}, 400)
        src = {"id": secrets.token_hex(4), "name": str(b.get("name") or "Calendar")[:60], "url": url,
               "color": str(b.get("color") or "#4fb3ff")[:9], "added": time.time()}
        try:
            n = len(cals.fetch(src, force=True))
        except Exception as e:
            return JSONResponse({"error": f"could not read that link: {str(e)[:200]}"}, 400)
        kv.kv_put("calsrc", src["id"], src)
        return dict(_mask(src), events=n)

    @app.delete("/api/calendar/sources/{sid}")
    def del_source(sid: str, req: Request):
        d = deny(req)
        if d:
            return d
        kv.kv_delete("calsrc", sid)
        cals.cache.pop(sid, None)
        return {"deleted": sid}

    @app.post("/api/calendar/sources/{sid}/test")
    def test_source(sid: str):
        src = kv.kv_get("calsrc", sid)
        if not src:
            return JSONResponse({"error": "not found"}, 404)
        try:
            return {"ok": True, "events": len(cals.fetch(src, force=True))}
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:300]}, 502)

    return cals
