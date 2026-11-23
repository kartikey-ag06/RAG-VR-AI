"""
assistant.py - the "daily assistant" part of the glasses (like Meta's glasses: reminders, notes, calendar,
weather, translation) plus the voice-command router.

  /api/translate           text -> another language (chat model)
  /api/reminders           list / add / done / delete   (a reminder with a time is also a calendar entry)
  /api/notes               list / add / delete
  /api/calendar?day=today  reminders due on that day
  /api/weather             Open-Meteo (free, no key): by lat/lon or city name
  /api/assistant/intent    a spoken sentence -> {intent, args} (e.g. "remind me at 5 to call Ravi")

Records are stored in Postgres when configured (app_data table), else in backend/data/app_data/*.json.
"""
from net import HTTP, JSONB
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import requests
from fastapi import Request
from fastapi.responses import JSONResponse

WMO = {0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "freezing fog",
       51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
       71: "light snow", 73: "snow", 75: "heavy snow", 80: "rain showers", 81: "heavy showers", 82: "violent showers",
       95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail"}

INTENTS = """You turn ONE spoken sentence from a smart-glasses user into a command. Reply with JSON only:
{"intent": "<one of the names below>", "args": {...}}
Intents and their args:
- ask {"question": str}                       (a normal question - the default)
- reminder_add {"text": str, "due": ISO-8601 datetime with offset or null}
     text = ONLY the thing to be reminded of, e.g. "Set a reminder to call Ravi at 5 pm" -> "Call Ravi".
     Never put "set a reminder", "remind me", the time or the time zone in text. No task given -> "Reminder".
- reminder_update {"which": "last" | words from that reminder, "text": str or null, "due": ISO datetime or null}
     ("change / move / update the last reminder to 7 pm", "push the pump reminder to tomorrow 9 am")
- reminder_delete {"which": "last" | "all" | words from that reminder}   ("cancel / delete / remove the reminder")
- timer {"seconds": int, "label": str or null}  ("set a timer for 10 minutes")
- reminder_list {}
- note_add {"text": str}
- note_list {}
- calendar {"day": "today" | "tomorrow" | "YYYY-MM-DD"}
- weather {"city": str or null}
- workflow_start {"topic": str}               ("guide me through / start procedure / how do I replace ...step by step")
- translate {"text": str or null, "target": language name in English}
- captions {"on": bool, "translate_to": language name or null}
- export {"kind": "inspection" | "incident" | "workorder", "format": "pdf" | "docx" | "json"}
- expert {}                                    (call / escalate to a remote expert)
- liveai {"on": bool}
- photo {}   - video {"on": bool}   - scan {}   - code {}   (scan a QR code / barcode)
- pin {}     - diagram {}            - language {"lang": language name}
Use the current time given by the user message to turn "at 5", "tomorrow morning", "in 10 minutes" into an exact
ISO time in the user's time zone (keep the offset of the current time). Speech-to-text often mishears:
"minder" / "remainder" = reminder, "11.05" or "11 05" = 11:05, "11 of 7" / "11.7" = 11:07 (today unless a day is
said). If unsure, use intent "ask"."""


_FILLER = re.compile(r"^\s*(?:(?:please|hey glasses|ok glasses|hey vr)[, ]+)?(?:(?:can you |could you )?(?:set|add|create|make|put)"
                     r" (?:a |an |me a )?(?:reminder|minder|remainder|alarm)s?(?: for| to| that| about)?|remind me"
                     r"(?: to| about| that| of)?|reminder(?: to| for| about)?)\s*", re.I)
_TIME_TAIL = re.compile(r"\s*(?:\b(?:at|by|on|for)\s+)?(?:today|tonight|tomorrow)?\s*(?:at\s+)?\d{1,2}(?:[:.]\d{2})?\s*"
                        r"(?:a\.?m\.?|p\.?m\.?)?(?:\s*(?:of|in|on)?\s*(?:indian|ist|india)(?:\s*(?:standard\s*)?time)?)?"
                        r"\s*(?:today|tonight|tomorrow)?\s*[.!]?\s*$", re.I)


def clean_reminder(text):
    """'Set a reminder for today 5 pm of Indian time to call Ravi' -> 'Call Ravi'."""
    t = _FILLER.sub("", str(text or "")).strip(" ,.!")
    t = re.sub(r"^(?:today|tonight|tomorrow)\b\s*", "", t, flags=re.I)
    t = re.sub(r"^(?:at\s+)?\d{1,2}(?:[:.]\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?(?:\s*(?:of|in|on)?\s*(?:indian|ist|india)"
               r"(?:\s*(?:standard\s*)?time)?)?(?:\s+(?:to|for|about)\b)?\s*", "", t, flags=re.I)
    t = _TIME_TAIL.sub("", t).strip(" ,.!")
    t = re.sub(r"^(?:to|for|about|that)\s+", "", t, flags=re.I)
    if not t or re.fullmatch(r"[\d:. ]+(?:am|pm)?", t, re.I):
        return "Reminder"
    return t[:1].upper() + t[1:]


def _id():
    return secrets.token_hex(5)


def register(app, kv, llm, cals=None, auth=None):
    tcache = OrderedDict()
    _RLOCKS = {}
    _RLOCK_MUTEX = threading.Lock()

    def _get_rlock(rid: str):
        with _RLOCK_MUTEX:
            if rid not in _RLOCKS:
                _RLOCKS[rid] = threading.Lock()
            return _RLOCKS[rid]

    def owner(req):
        if not auth:
            return ""
        u = auth.user_of(req)
        return u["id"] if u else ""

    def mine(item, req):
        if not auth or not auth.enabled:
            return True
        if auth.is_admin(req):
            return True
        u = auth.user_of(req)
        if not u:
            return False
        return item.get("owner") == u["id"] or not item.get("owner")

    # ---------- translation
    @app.post("/api/translate")
    def translate(req: Request, _body=JSONB):
        if auth and auth.admin_required(req) and not auth.require_user(req):
            return JSONResponse({"error": "unauthorized"}, 401)
        b = (_body if _body is not None else {})
        text, target = str(b.get("text", "")).strip()[:3000], str(b.get("target", "English")).strip()[:40]
        if not text:
            return {"text": ""}
        key = (text, target)
        if key in tcache:
            return {"text": tcache[key], "cached": True}
        try:
            out = llm([{"role": "system", "content": f"Translate the user's text into {target}. Keep names, numbers and "
                        "units. Reply with the translation only, no notes, no quotes."},
                       {"role": "user", "content": text}], 800).strip()
        except Exception as e:
            return JSONResponse({"error": f"translation failed: {e}"}, 502)
        tcache[key] = out
        if len(tcache) > 500:
            tcache.popitem(last=False)
        return {"text": out}

    # ---------- reminders (+ calendar view) and notes
    def items(kind):
        return kv.kv_list(kind)

    @app.get("/api/reminders")
    def reminders(req: Request, all: int = 0):
        if auth and auth.admin_required(req) and not auth.require_user(req):
            return JSONResponse({"error": "unauthorized"}, 401)
        rs = [r for r in items("reminder") if mine(r, req) and (all or not r.get("done"))]
        return {"reminders": sorted(rs, key=lambda r: (r.get("due") or "9999", r.get("created", 0)))}

    @app.post("/api/reminders")
    def reminder_add(req: Request, _body=JSONB):
        if auth and auth.admin_required(req) and not auth.require_user(req):
            return JSONResponse({"error": "unauthorized"}, 401)
        b = (_body if _body is not None else {})
        text = clean_reminder(str(b.get("text", "")).strip()[:300])
        due = b.get("due")
        if due:
            try:
                due = datetime.fromisoformat(str(due).replace("Z", "+00:00")).isoformat(timespec="minutes")
            except ValueError:
                due = None
        r = {"id": _id(), "text": text, "due": due or None, "done": False, "created": time.time(), "notified": False, "owner": owner(req)}
        kv.kv_put("reminder", r["id"], r)
        return r

    @app.patch("/api/reminders/{rid}")
    def reminder_patch(rid: str, req: Request, _body=JSONB):
        with _get_rlock(rid):
            r = kv.kv_get("reminder", rid)
            if not r or not mine(r, req):
                return JSONResponse({"error": "not found"}, 404)
            b = (_body if _body is not None else {})
            for k in ("done", "notified"):
                if k in b:
                    r[k] = bool(b[k])
            if b.get("text"):
                r["text"] = clean_reminder(str(b["text"])[:300])
            if "due" in b:
                try:
                    r["due"] = datetime.fromisoformat(str(b["due"]).replace("Z", "+00:00")).isoformat(timespec="minutes") if b["due"] else None
                    r["notified"] = False
                except ValueError:
                    pass
            kv.kv_put("reminder", rid, r)
            return r

    @app.delete("/api/reminders/{rid}")
    def reminder_delete(rid: str, req: Request):
        r = kv.kv_get("reminder", rid)
        if not r or not mine(r, req):
            return JSONResponse({"error": "not found"}, 404)
        kv.kv_delete("reminder", rid)
        return {"deleted": rid}

    @app.get("/api/calendar")
    def calendar(req: Request, day: str = "today", tz: int = 0):
        """tz = minutes east of UTC (India = 330). Entries = reminders with a time + events of linked calendars."""
        if auth and auth.admin_required(req) and not auth.require_user(req):
            return JSONResponse({"error": "unauthorized"}, 401)
        now = datetime.now(timezone(timedelta(minutes=tz)))
        if day == "tomorrow":
            d = (now + timedelta(days=1)).date()
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            d = datetime.strptime(day, "%Y-%m-%d").date()
        else:
            d = now.date()
        out = []
        for r in items("reminder"):
            if not mine(r, req):
                continue
            if r.get("due"):
                try:
                    dt = datetime.fromisoformat(r["due"]).astimezone(now.tzinfo)
                except ValueError:
                    continue
                if dt.date() == d:
                    out.append(dict(r, local=dt.strftime("%H:%M")))
        errors = []
        if cals is not None:
            evs, errors = cals.events_on(d, now.tzinfo)
            out += evs
        out.sort(key=lambda r: (not r.get("all_day"), r["due"]))
        return {"day": d.isoformat(), "events": out, "errors": errors,
                "linked": len(cals.sources()) if cals is not None else 0}

    @app.get("/api/notes")
    def notes(req: Request):
        if auth and auth.admin_required(req) and not auth.require_user(req):
            return JSONResponse({"error": "unauthorized"}, 401)
        return {"notes": [n for n in items("note") if mine(n, req)]}

    @app.post("/api/notes")
    def note_add(req: Request, _body=JSONB):
        if auth and auth.admin_required(req) and not auth.require_user(req):
            return JSONResponse({"error": "unauthorized"}, 401)
        b = (_body if _body is not None else {})
        text = str(b.get("text", "")).strip()[:2000]
        if not text:
            return JSONResponse({"error": "empty note"}, 400)
        n = {"id": _id(), "text": text, "created": time.time(), "owner": owner(req)}
        kv.kv_put("note", n["id"], n)
        return n

    @app.delete("/api/notes/{nid}")
    def note_delete(nid: str, req: Request):
        n = kv.kv_get("note", nid)
        if not n or not mine(n, req):
            return JSONResponse({"error": "not found"}, 404)
        kv.kv_delete("note", nid)
        return {"deleted": nid}

    # ---------- weather (Open-Meteo, no API key)
    @app.get("/api/weather")
    def weather(lat: float = None, lon: float = None, city: str = ""):
        place = ""
        try:
            if (lat is None or lon is None) and city:
                g = HTTP.get("https://geocoding-api.open-meteo.com/v1/search",
                                 params={"name": city, "count": 1}, timeout=10).json()
                if not g.get("results"):
                    return JSONResponse({"error": f"city not found: {city}"}, 404)
                r0 = g["results"][0]
                lat, lon, place = r0["latitude"], r0["longitude"], f"{r0['name']}, {r0.get('country', '')}"
            if lat is None or lon is None:
                return JSONResponse({"error": "give lat/lon or a city"}, 400)
            w = HTTP.get("https://api.open-meteo.com/v1/forecast", timeout=10, params={
                "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 3,
                "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"}).json()
        except Exception as e:
            return JSONResponse({"error": f"weather service: {e}"}, 502)
        c, d = w.get("current", {}), w.get("daily", {})
        days = [{"date": d["time"][i], "max": d["temperature_2m_max"][i], "min": d["temperature_2m_min"][i],
                 "rain": d["precipitation_probability_max"][i], "desc": WMO.get(d["weather_code"][i], "")}
                for i in range(len(d.get("time", [])))]
        return {"place": place or f"{lat:.2f}, {lon:.2f}", "temp": c.get("temperature_2m"), "wind": c.get("wind_speed_10m"),
                "humidity": c.get("relative_humidity_2m"), "desc": WMO.get(c.get("weather_code"), ""), "days": days}

    # ---------- voice command router
    @app.post("/api/assistant/intent")
    def intent(req: Request, _body=JSONB):
        if auth and auth.admin_required(req) and not auth.require_user(req):
            return JSONResponse({"error": "unauthorized"}, 401)
        b = (_body if _body is not None else {})
        text = str(b.get("text", "")).strip()[:500]
        now = str(b.get("now", ""))[:40] or datetime.now(timezone.utc).isoformat(timespec="minutes")
        if not text:
            return {"intent": "none", "args": {}}
        try:
            out = llm([{"role": "system", "content": INTENTS},
                       {"role": "user", "content": f"Current time: {now}\nSentence: {text}"}], 400, fast=True)
            m = re.search(r"\{.*\}", out, re.S)
            d = json.loads(m.group(0)) if m else {}
        except Exception:
            d = {}
        it = str(d.get("intent", "ask"))
        args = d.get("args") if isinstance(d.get("args"), dict) else {}
        if it == "ask" and not args.get("question"):
            args["question"] = text
        return {"intent": it, "args": args}
