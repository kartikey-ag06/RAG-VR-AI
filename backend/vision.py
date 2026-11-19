"""
vision.py - "Scan, then ask": the user shows a photo (webcam or upload) and asks a question about it.

  /api/vision/status    is a vision model available? which one?
  /api/vision/describe  photo -> what object it is + words seen on it (used to search the documents)
  /api/vision/chat      photo + prompt -> answer (the app sends the document excerpts in the prompt)

The photo is sent to Groq and NOT stored anywhere on disk.
Model: picked automatically from the Groq model list (override with vision_model.txt in the project folder).
"""
import json
import re
import time

import requests
from fastapi import Request
from fastapi.responses import JSONResponse

# newest first; only models that the account's /models list actually returns are used
PREFERRED = ["meta-llama/llama-4-scout-17b-16e-instruct", "meta-llama/llama-4-maverick-17b-128e-instruct",
             "qwen/qwen3.8-27b", "qwen/qwen3.6-27b", "meta-models/muse-spark-open"]
LOOKS_VISION = re.compile(r"vision|llama-4|scout|maverick|qwen3\.\d+-27b|muse|llava", re.I)
DATA_URL = re.compile(r"^data:image/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$")
MAX_B64 = 4 * 1024 * 1024          # Groq's limit for base64 images

DESCRIBE_PROMPT = (
    "Look at this photo taken by a user wearing smart glasses. Reply with JSON only, no other text: "
    '{"object": "what the main object is, in a few words", '
    '"details": "2-3 sentences: type, brand or model if visible, colour, material, condition and notable features", '
    '"text_seen": "any words, numbers, labels, model numbers or logos you can read, else empty", '
    '"keywords": "6-10 search keywords for finding this object in documents"}'
)


def register(app, provider, get_key, list_models, root):
    cache = {"model": None, "t": 0}
    override = root / "vision_model.txt"

    def vision_model():
        if override.exists() and override.read_text().strip():
            return override.read_text().strip().splitlines()[0]
        if cache["model"] and time.time() - cache["t"] < 3600:
            return cache["model"]
        ids = list_models()
        m = next((x for x in PREFERRED if x in ids), None) or next((x for x in ids if LOOKS_VISION.search(x)), None)
        cache.update(model=m, t=time.time())
        return m

    def call(image, system, user, max_tokens=700):
        p = provider()
        if not p or p["name"] != "groq":
            raise RuntimeError("Photo questions need a Groq key (gsk_...) in grok_api_key.txt")
        model = vision_model()
        if not model:
            raise RuntimeError("No vision model is available on this Groq account")
        msgs = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": [{"type": "text", "text": user},
                                         {"type": "image_url", "image_url": {"url": image}}]}]
        body = {"model": model, "messages": msgs, "temperature": 0.2, "max_tokens": max_tokens}
        for attempt in (1, 2):
            r = requests.post(f"{p['base']}/chat/completions", headers={"Authorization": f"Bearer {get_key()}"},
                              json=body, timeout=90)
            if r.status_code != 429 or attempt == 2:
                break
            # free-tier limit per minute: Groq says how long to wait ("try again in 1.13s") - wait once, then retry
            m = re.search(r"try again in ([\d.]+)s", r.text)
            wait = float(m.group(1)) + 0.5 if m else 3.0
            if wait > 15:
                break
            time.sleep(wait)
        if r.status_code == 429:
            raise RuntimeError("Groq free-tier limit reached (tokens per minute). Wait a few seconds and scan again.")
        if r.status_code != 200:
            raise RuntimeError(f"groq vision ({model}): HTTP {r.status_code} - {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"] or "", model

    def check(image):
        if not isinstance(image, str) or not DATA_URL.fullmatch(image):
            return "photo must be a JPEG, PNG or WebP data URL"
        if len(image) > MAX_B64:
            return "photo too large (max 4 MB) - take it again at a lower resolution"
        return None

    @app.get("/api/vision/status")
    def vision_status():
        p = provider()
        if not p or p["name"] != "groq":
            return {"available": False, "reason": "needs a Groq key"}
        try:
            m = vision_model()
        except Exception as e:
            return {"available": False, "reason": str(e)[:200]}
        return {"available": bool(m), "model": m or "", "reason": "" if m else "no vision model on this account"}

    @app.post("/api/vision/describe")
    async def vision_describe(req: Request):
        b = await req.json()
        bad = check(b.get("image"))
        if bad:
            return JSONResponse({"error": bad}, 400)
        try:
            hint = str(b.get("hint", ""))[:300]
            out, model = call(b["image"], "", (hint + "\n" if hint else "") + DESCRIBE_PROMPT, 300)
        except Exception as e:
            return JSONResponse({"error": str(e)}, 502)
        txt = re.sub(r"^```(?:json)?|```$", "", out.strip(), flags=re.M).strip()
        m = re.search(r"\{.*\}", txt, re.S)
        d = {}
        if m:
            try:
                d = json.loads(m.group(0))
            except ValueError:
                d = {}
        if not d:                                    # model ignored the JSON instruction: use its text
            d = {"object": txt[:120], "text_seen": "", "keywords": txt[:200]}
        return {"object": str(d.get("object", ""))[:200], "details": str(d.get("details", ""))[:600], "text_seen": str(d.get("text_seen", ""))[:400],
                "keywords": str(d.get("keywords", ""))[:300], "model": model}

    @app.post("/api/vision/chat")
    async def vision_chat(req: Request):
        b = await req.json()
        bad = check(b.get("image"))
        if bad:
            return JSONResponse({"error": bad}, 400)
        try:
            out, model = call(b["image"], str(b.get("system", ""))[:4000], str(b.get("user", ""))[:16000])
        except Exception as e:
            return JSONResponse({"error": str(e)}, 502)
        return {"text": out, "model": model}
