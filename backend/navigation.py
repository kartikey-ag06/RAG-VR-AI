"""
navigation.py - turn-by-turn directions for the glasses (walking, cycling, driving), free and with no API key.

  GET /api/directions?to=India Gate&from=28.61,77.20&mode=walk|bike|drive
      from = "lat,lon" (the glasses' GPS) or a place name; to = a place name or "lat,lon".
      -> {from, to, mode, distance_m, duration_s, steps:[{n, text, short, arrow, distance_m, lat, lon, street}]}
  GET /api/places?q=coffee near Connaught Place   (place search, for "where is ...")

Places come from OpenStreetMap Nominatim (max 1 request a second, results cached); routes come from the OSRM
servers of openstreetmap.de (foot / bike / car profiles). Each step is turned into a short HUD line
("Turn left onto Rajpath · 120 m") and an arrow (↑ ↰ ↱ ↶ ⟲ ⚑).
"""
import math
import re
import threading
import time
from collections import OrderedDict

from fastapi.responses import JSONResponse

from net import HTTP

NOMINATIM = "https://nominatim.openstreetmap.org/search"
OSRM = {"walk": "https://routing.openstreetmap.de/routed-foot/route/v1/foot",
        "bike": "https://routing.openstreetmap.de/routed-bike/route/v1/bike",
        "drive": "https://routing.openstreetmap.de/routed-car/route/v1/driving"}
UA = {"User-Agent": "VR_RAG-glasses/1.0 (directions demo)"}
LATLON = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")

_geo_cache = OrderedDict()
_geo_lock = threading.Lock()
_last_call = [0.0]


def geocode(q, near=None):
    """Place name -> {name, lat, lon}. Uses a small cache and keeps to Nominatim's 1 request / second."""
    m = LATLON.match(q or "")
    if m:
        return {"name": f"{float(m[1]):.5f}, {float(m[2]):.5f}", "lat": float(m[1]), "lon": float(m[2])}
    key = (q.strip().lower(), round(near[0], 1) if near else None, round(near[1], 1) if near else None)
    with _geo_lock:
        if key in _geo_cache:
            return _geo_cache[key]
        wait = 1.05 - (time.time() - _last_call[0])
        if wait > 0:
            time.sleep(wait)
        params = {"q": q, "format": "jsonv2", "limit": 1}
        if near:                                        # prefer places around the user
            params.update(viewbox=f"{near[1]-0.5},{near[0]+0.5},{near[1]+0.5},{near[0]-0.5}", bounded=0)
        r = HTTP.get(NOMINATIM, params=params, headers=UA, timeout=15)
        _last_call[0] = time.time()
    r.raise_for_status()
    js = r.json()
    if not js:
        return None
    hit = {"name": js[0].get("display_name", q).split(",")[0], "full": js[0].get("display_name", q),
           "lat": float(js[0]["lat"]), "lon": float(js[0]["lon"])}
    with _geo_lock:
        _geo_cache[key] = hit
        while len(_geo_cache) > 300:
            _geo_cache.popitem(last=False)
    return hit


def _fmt_dist(m):
    return f"{int(round(m / 10.0) * 10)} m" if m < 950 else f"{m / 1000:.1f} km"


ARROWS = {"left": "↰", "slight left": "↖", "sharp left": "↰", "right": "↱", "slight right": "↗", "sharp right": "↱",
          "straight": "↑", "uturn": "↶"}


def step_text(st):
    """One OSRM step -> (long text, short HUD text, arrow)."""
    man = st.get("maneuver", {})
    typ, mod = man.get("type", ""), man.get("modifier", "straight")
    road = st.get("name") or st.get("ref") or ""
    onto = f" onto {road}" if road else ""
    arrow = ARROWS.get(mod, "↑")
    if typ == "depart":
        t = f"Head {['north', 'north-east', 'east', 'south-east', 'south', 'south-west', 'west', 'north-west'][int(((man.get('bearing_after', 0) + 22.5) % 360) // 45)]}{(' on ' + road) if road else ''}"
        arrow = "↑"
    elif typ == "arrive":
        t, arrow = "You have arrived" + (f" at {road}" if road else ""), "⚑"
    elif typ in ("roundabout", "rotary"):
        ex = man.get("exit")
        t, arrow = f"At the roundabout take exit {ex}{onto}" if ex else f"Go round the roundabout{onto}", "⟲"
    elif typ in ("merge", "on ramp", "off ramp", "fork"):
        t = f"{'Keep' if typ == 'fork' else 'Take the ramp'} {mod}{onto}".replace("straight", "ahead")
    elif typ == "new name" or (typ == "continue" and mod == "straight"):
        t = f"Continue{onto or ' straight'}"
    elif typ == "end of road":
        t = f"At the end of the road turn {mod}{onto}"
    else:
        t = ("Make a U-turn" + onto) if mod == "uturn" else (f"Turn {mod}{onto}" if mod != "straight" else f"Continue{onto or ' straight'}")
    dist = st.get("distance", 0)
    return t, (f"{t} · {_fmt_dist(dist)}" if typ != "arrive" else t), arrow


def route(frm, to, mode="walk"):
    base = OSRM.get(mode, OSRM["walk"])
    url = f"{base}/{frm['lon']},{frm['lat']};{to['lon']},{to['lat']}"
    r = HTTP.get(url, params={"steps": "true", "overview": "false", "alternatives": "false"}, headers=UA, timeout=20)
    r.raise_for_status()
    js = r.json()
    if js.get("code") != "Ok" or not js.get("routes"):
        raise ValueError(js.get("message") or "no route found")
    rt = js["routes"][0]
    steps = []
    for leg in rt["legs"]:
        for st in leg["steps"]:
            text, short, arrow = step_text(st)
            loc = st.get("maneuver", {}).get("location", [None, None])
            steps.append({"n": len(steps) + 1, "text": text, "short": short, "arrow": arrow,
                          "distance_m": round(st.get("distance", 0)), "duration_s": round(st.get("duration", 0)),
                          "lon": loc[0], "lat": loc[1], "street": st.get("name", "")})
    # footpaths have many tiny unnamed wiggles: fold a step shorter than 30 m with no street name (and any
    # short "continue") into the step before, so the lens shows only turns that matter
    merged = []
    for s in steps:
        tiny = s["distance_m"] < 30 and (not s["street"] or s["text"].startswith("Continue"))
        if merged and tiny and s["arrow"] not in ("⚑", "⟲") and merged[-1]["arrow"] != "⚑":
            merged[-1]["distance_m"] += s["distance_m"]
            merged[-1]["duration_s"] += s["duration_s"]
            continue
        merged.append(s)
    for s in merged:                                   # distances changed: rebuild the short HUD line
        if s["arrow"] != "⚑":
            s["short"] = f"{s['text']} · {_fmt_dist(s['distance_m'])}"
    for i, s in enumerate(merged):
        s["n"] = i + 1
    return {"distance_m": round(rt["distance"]), "duration_s": round(rt["duration"]), "steps": merged}


def haversine(a, b):
    R = 6371000.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


MODES = {"walk": "walk", "walking": "walk", "foot": "walk", "bike": "bike", "cycle": "bike", "cycling": "bike",
         "bicycle": "bike", "drive": "drive", "driving": "drive", "car": "drive"}


def register(app):
    @app.get("/api/directions")
    def directions(to: str, mode: str = "walk", frm: str = "", lat: float = None, lon: float = None):
        mode = MODES.get(mode.lower(), "walk")
        near = (lat, lon) if lat is not None and lon is not None else None
        try:
            start = geocode(frm, near) if frm else ({"name": "your location", "lat": lat, "lon": lon} if near else None)
            if not start:
                return JSONResponse({"error": "I need your location: allow location access, or say where you start from"}, 400)
            dest = geocode(to, (start["lat"], start["lon"]))
            if not dest:
                return JSONResponse({"error": f"I could not find “{to}” on the map"}, 404)
            rt = route(start, dest, mode)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, 404)
        except Exception as e:
            return JSONResponse({"error": f"map service unavailable: {str(e)[:160]}"}, 502)
        return dict(rt, mode=mode, **{"from": start, "to": dest})

    @app.get("/api/places")
    def places(q: str, lat: float = None, lon: float = None):
        try:
            hit = geocode(q, (lat, lon) if lat is not None and lon is not None else None)
        except Exception as e:
            return JSONResponse({"error": f"map service unavailable: {str(e)[:160]}"}, 502)
        if not hit:
            return JSONResponse({"error": "not found"}, 404)
        if lat is not None and lon is not None:
            hit = dict(hit, distance_m=round(haversine((lat, lon), (hit["lat"], hit["lon"]))))
        return hit
