// rag.js - Retrieval (TF-IDF search) + Generation (Ollama / Claude / OpenAI / offline)
// Must stay in sync with tokenize() in rag_pipeline/build_index.py

const STOP = new Set(`a an the and or of to in on for by with at from as is are was were be been being this that these those it its
which who whom whose what when where why how not no nor but if then than so such into upon under over above below
shall will would should may might can could must do does did done have has had having he she they them his her their
i we you me us our your said also any all each other same per vide hereinafter thereof therein herein whereas
page sebi noticee noticees order`.split(/\s+/).filter(Boolean));

export const API_BASE = (import.meta.env.VITE_API_URL || "").replace(/\/+$/, "");

export function apiUrl(path) {
  return API_BASE ? `${API_BASE}${path}` : path;
}

/** Headers for every backend call: the user's access token (documents with groups need it). */
export function authHeaders(extra = {}) {
  let t = "";
  try { t = localStorage.getItem("vr-user-token") || ""; } catch { /* private mode */ }
  return t ? { ...extra, "X-VR-Token": t } : extra;
}

/** JSON helper for the newer modules: api("/api/notes"), api("/api/notes", {text}) -> POST */
// ---- server load protection: after a 429 / 503 the app waits (Retry-After) instead of hammering the server
let aiBlockedUntil = 0;
function noteLimit(r) {
  if (r.status === 429 || r.status === 503) {
    const s = Number(r.headers.get("Retry-After")) || 10;
    aiBlockedUntil = Date.now() + Math.min(s, 3600) * 1000;
  }
}
function cooldownMsg() {
  return `Too many requests - please wait ${Math.max(1, Math.ceil((aiBlockedUntil - Date.now()) / 1000))} s`;
}
const AI_PATHS = ["/api/v1/ask", "/api/grok", "/api/translate", "/api/assistant/intent", "/api/vision/", "/api/workflows/generate", "/api/teleprompter/script"];

export async function api(path, body, method) {
  if (Date.now() < aiBlockedUntil && AI_PATHS.some(p => path.startsWith(p))) throw new Error(cooldownMsg());
  const opts = { method: method || (body === undefined ? "GET" : "POST"), headers: authHeaders(body === undefined ? {} : { "Content-Type": "application/json" }) };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const r = await fetch(apiUrl(path), opts);
  if (AI_PATHS.some(p => path.startsWith(p))) noteLimit(r);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

// ---------------- answer style: language + length (full / glance card / spoken) ----------------
const style = { lang: "English", mode: "full" };
export function setAnswerStyle(lang, mode) { if (lang) style.lang = lang; if (mode) style.mode = mode; }
function styleRules() {
  const len = { full: "max 120 words", glance: "at most 3 short numbered points, each under 12 words (small lens display)",
    spoken: "at most 25 words in 1-2 short sentences, no lists (it will be read aloud)" }[style.mode] || "max 120 words";
  return `\nLength: ${len}.` + (style.lang && style.lang !== "English"
    ? `\nWrite the answer in ${style.lang}. Keep the SOURCE line exactly in English as described.` : "");
}

export function tokenize(text) {
  return (text.toLowerCase().match(/[a-z0-9]+/g) || []).filter(t => t.length > 1 && !STOP.has(t));
}

export class RagIndex {
  static async load(url = (API_BASE ? `${API_BASE}/` : import.meta.env.BASE_URL) + "rag/index.json") {
    const data = await (await fetch(url, { cache: "no-cache", headers: authHeaders() })).json();   // ETag: unchanged index = 304, no download
    indexVersion = data.version || 0;
    const ix = new RagIndex();
    ix.data = data;
    ix.vocab = new Map(data.vocab.map((t, i) => [t, i]));
    return ix;
  }

  /** cosine similarity between question TF-IDF vector and each chunk vector */
  search(query, topK = 5) {
    const counts = new Map();
    for (const t of tokenize(query)) {
      const id = this.vocab.get(t);
      if (id !== undefined) counts.set(id, (counts.get(id) || 0) + 1);
    }
    if (!counts.size) return [];
    const q = new Map();
    let norm = 0;
    for (const [id, c] of counts) {
      const w = (1 + Math.log(c)) * this.data.idf[id];
      q.set(id, w); norm += w * w;
    }
    norm = Math.sqrt(norm);
    const hits = [];
    this.data.chunks.forEach((ch, n) => {
      let s = 0;
      for (let i = 0; i < ch.terms.length; i++) {
        const qw = q.get(ch.terms[i]);
        if (qw) s += qw * ch.weights[i];
      }
      if (s > 0) hits.push({ chunk: ch, doc: this.data.docs[ch.doc], score: s / norm, i: n });   // i: chunk position
    });
    hits.sort((a, b) => b.score - a.score);
    return hits.slice(0, topK);
  }
}

// The index version changes whenever documents are uploaded or deleted; adding it to page URLs
// stops the browser from showing a cached image of a page that now belongs to another document.
let indexVersion = 0;
export function pageImageUrl(docId, page) {
  const base = API_BASE ? `${API_BASE}/` : import.meta.env.BASE_URL;
  return base + `rag/pages/d${String(docId).padStart(2, "0")}_p${String(page).padStart(3, "0")}.jpg?v=${indexVersion}` + tokenParam();
}
/** Cut-out diagram / table of a page (see indexer.py _page_figures). */
export function figureUrl(docId, file) {
  const base = API_BASE ? `${API_BASE}/` : import.meta.env.BASE_URL;
  return base + `rag/figures/${docId}/${file}?v=${indexVersion}` + tokenParam();
}
function tokenParam() {          // <img> cannot send headers: protected pages get the token in the URL
  let t = ""; try { t = localStorage.getItem("vr-user-token") || ""; } catch { /* ignore */ }
  return t ? "&token=" + encodeURIComponent(t) : "";
}

/** Current index version on the server (null when the server has no upload support). */
export async function serverIndexVersion() {
  try {
    const r = await fetch(apiUrl("/api/index/version"), { cache: "no-store", signal: AbortSignal.timeout(3000) });
    return r.ok ? (await r.json()).version : null;
  } catch { return null; }
}

// ---------------- Generation ----------------
// Answers come from the server: /api/v1/ask builds the prompt from its own index (ADR 0004).
export async function loadLlmConfig() {
  let cfg = { provider: "auto" };
  try { cfg = await (await fetch(import.meta.env.BASE_URL + "rag/llm_config.json")).json(); } catch { /* defaults */ }
  cfg.provider = (cfg.provider || "auto").toLowerCase();
  if (cfg.provider === "auto" || cfg.provider === "grok") {
    // Grok via our local server (server.ps1 holds the API key, the browser never sees it)
    try {
      const r = await fetch(apiUrl("/api/grok/status"), { signal: AbortSignal.timeout(1500) });
      const st = await r.json();
      cfg.search = st.search || ""; cfg.backend = st.backend || "powershell";
      if (st.configured) { cfg.provider = st.provider || "grok"; cfg.model = st.model; cfg.stt = !!st.stt; return cfg; }
    } catch { /* server without Grok support */ }
    if (cfg.provider === "grok") { cfg.provider = "none"; cfg.note = "Grok key not found (grok_api_key.txt)"; return cfg; }
  }
  if (cfg.provider === "auto") {
    // Is a local Ollama server running? (this PC has Ollama installed)
    try {
      const r = await fetch((cfg.endpoint || "http://localhost:11434").replace(/\/api\/chat$/, "") + "/api/tags",
        { signal: AbortSignal.timeout(1500) });
      const models = (await r.json()).models || [];
      if (models.length) { cfg.provider = "ollama"; cfg.model = cfg.model || models[0].name; }
      else cfg.provider = "none";
    } catch { cfg.provider = "none"; }
  }
  return cfg;
}

function buildPrompt(question, hits) {
  let s = "Excerpts:\n";
  hits.forEach((h, i) => { s += `[${i + 1}] (${h.doc.title}, page ${h.chunk.page})\n${h.chunk.text}\n\n`; });
  return s + "Question: " + question;
}

export function extractive(question, hits) {
  const q = new Set(tokenize(question));
  let out = "", n = 0;
  const used = new Set();
  hits.forEach((h, i) => {
    if (n >= 3) return;
    const sents = h.chunk.text.split(/(?<=[.;])\s+/);
    let best = sents[0], bestScore = -1;
    for (const s of sents) {
      const sc = tokenize(s).filter(t => q.has(t)).length;
      if (sc > bestScore) { best = s; bestScore = sc; }
    }
    best = best.trim();
    if (used.has(best)) return;
    used.add(best); n++;
    if (best.length > 260) best = best.slice(0, 260) + "...";
    out += `[${i + 1}] ${best}\n\n`;
  });
  return out + "(Offline mode: put an xAI or Groq key in VR_RAG\\grok_api_key.txt and restart START_VR_RAG.bat for Grok answers)";
}

// ---------------- Smart mode: answer ANY question ----------------
// Groq decides: if the PDF excerpts answer it -> cite them (and the app shows that PDF page);
// otherwise -> a general-knowledge answer, clearly labelled, with no PDF page.
const SMART_SYSTEM =
  "You are the assistant inside a VR app. You receive numbered excerpts from the user's uploaded documents, " +
  "plus a user question that may or may not be about them.\n" +
  "Rules:\n" +
  "1. If the excerpts contain the answer (fully or partly), answer from them and cite like [1], [2]. " +
  "Your FIRST line must be exactly: SOURCE: PDF [n, m] (the excerpt numbers you used, most important first).\n" +
  "2. If the excerpts do not contain the answer, answer from your general knowledge. Your FIRST line must be exactly: SOURCE: GENERAL\n" +
  "3. Then a blank line, then the answer in plain text (see the length rule).\n" +
  "4. Never invent citations, section numbers, case numbers or penalty amounts. If unsure, say so.";

/** returns { text, mode: "pdf" | "general" | "offline", cited: [hit indexes] } */
/** The answer text as shown on the lens: no "SOURCE:" line, no markdown. Works on a half-written answer too. */
export function cleanBody(raw) {
  const m = String(raw || "").match(/^\s*SOURCE:\s*(PDF|GENERAL)?\s*(\[[^\]]*\]?)?\s*/i);
  let body = m ? raw.slice(m[0].length) : String(raw || "");
  if (/^\s*S(O(U(R(C(E:?)?)?)?)?)?\s*$/i.test(body)) body = "";          // header still arriving
  return body.trim().replace(/\*\*(.+?)\*\*/g, "$1").replace(/__(.+?)__/g, "$1").replace(/\*\*/g, "")
    .replace(/^#+\s*/gm, "").replace(/^\s*[-*]\s+/gm, "• ");
}

export async function answerSmart(question, hits, cfg, onDelta) {
  if (cfg.provider === "none") {
    if (!hits.length) return { text: "No matching text in your documents, and no AI is connected for general questions.", mode: "offline", cited: [] };
    return { text: extractive(question, hits), mode: "offline", cited: hits.map((_, i) => i) };
  }
  if (usesServer(cfg)) return askServerSafe(question, hits, null, onDelta,
    (e) => ({ text: `AI error (${cfg.provider}): ${e.message}\n\n` + (hits.length ? extractive(question, hits) : ""), mode: "offline", cited: hits.map((_, i) => i) }));
  const user = (hits.length ? buildPrompt(question, hits) : "Excerpts: (none matched)\n\nQuestion: " + question);
  let raw;
  try { raw = await callLlm(SMART_SYSTEM + styleRules(), user, cfg, onDelta); }
  catch (e) {
    if (isAbort(e)) return { text: "Stopped.", mode: "offline", cited: [], stopped: true };
    return { text: `AI error (${cfg.provider}): ${e.message}\n\n` + (hits.length ? extractive(question, hits) : ""), mode: "offline", cited: hits.map((_, i) => i) };
  }
  return parseSmart(raw, hits);
}

/** Reads the "SOURCE: PDF [n]" / "SOURCE: GENERAL" first line and the cited excerpt numbers. */
function parseSmart(raw, hits) {
  const m = raw.match(/^\s*SOURCE:\s*(PDF|GENERAL)\s*(\[[^\]]*\])?\s*/i);
  let body = cleanBody(raw);                                                     // plain text for the VR panel
  let cited = [];
  const nums = (m && m[2] ? m[2] : "") + " " + body;
  for (const x of nums.matchAll(/\[(\d+(?:\s*,\s*\d+)*)\]/g))
    for (const n of x[1].split(",")) { const i = parseInt(n, 10) - 1; if (i >= 0 && i < hits.length && !cited.includes(i)) cited.push(i); }
  const mode = (m && m[1].toUpperCase() === "GENERAL") || !cited.length ? "general" : "pdf";
  if (mode === "general") cited = [];
  return { text: body, mode, cited, stopped: raw.endsWith(STOPPED) };
}

let streamOk = true;
// The answer being written right now can be cancelled (Stop button, Esc, "stop" by voice).
let current = null;
/** Cancels the AI answer being written now. Returns true if there was one. */
export function stopAnswer() { if (!current) return false; current.abort(); current = null; return true; }
const STOPPED = "(stopped)";
const isAbort = (e) => e && (e.name === "AbortError" || e.message === STOPPED);

// ---------------- Server answers: /api/v1/ask (+ /stream) ----------------
// The glasses send the QUESTION, the answer style and REFERENCES to the passages they found (chunk i, doc, page).
// The server reads the passage text from its own index, checks permissions, builds the prompt and calls the AI.
// `cited` in the reply = positions in OUR hits list, so the page shown is the one the answer used.
const usesServer = (cfg) => cfg.provider === "grok" || cfg.provider === "groq";

function askBody(question, hits, photoInfo) {
  const b = { question, style: style.mode, lang: style.lang,
    passages: hits.map((h) => ({ i: h.i ?? -1, doc_uid: h.doc.uid, page: h.chunk.page })) };
  if (photoInfo) b.photo = { object: photoInfo.object || "", details: photoInfo.details || "",
    text_seen: photoInfo.text_seen || "", keywords: photoInfo.keywords || "" };
  return b;
}

function fromServer(j, hits) {
  if (j.mode === "offline") return { text: j.answer, mode: "offline", cited: hits.map((_, i) => i).slice(0, 3) };
  return { text: j.answer, mode: j.mode, cited: (j.cited || []).filter((i) => i >= 0 && i < hits.length) };
}

async function askServerSafe(question, hits, photoInfo, onDelta, onError) {
  if (Date.now() < aiBlockedUntil) return onError(new Error(cooldownMsg()));
  const ac = new AbortController(); current = ac;
  try { return await askServer(question, hits, photoInfo, onDelta, ac.signal); }
  catch (e) {
    if (ac.signal.aborted || isAbort(e)) return { text: "Stopped.", mode: "offline", cited: [], stopped: true };
    return onError(e);
  } finally { if (current === ac) current = null; }
}

async function askServer(question, hits, photoInfo, onDelta, signal) {
  const body = JSON.stringify(askBody(question, hits, photoInfo));
  const headers = authHeaders({ "Content-Type": "application/json" });
  if (onDelta && streamOk && window.TextDecoder) {
    const r = await fetch(apiUrl("/api/v1/ask/stream"), { method: "POST", headers, body, signal });
    noteLimit(r);
    if (r.status === 404 || r.status === 405) streamOk = false;               // older server: plain /api/v1/ask
    else {
      if (!r.ok) { const j = await r.json().catch(() => ({})); throw new Error(j.error || "HTTP " + r.status); }
      const rd = r.body.getReader(), dec = new TextDecoder();
      let buf = "", acc = "", done = null;
      for (;;) {
        let chunk;
        try { chunk = await rd.read(); }
        catch (e) {
          if (signal.aborted && acc.trim()) return { text: cleanBody(acc) + "\n\n" + STOPPED, mode: "general", cited: [], stopped: true };
          throw e;
        }
        if (chunk.done) break;
        buf += dec.decode(chunk.value, { stream: true });
        let cut;
        while ((cut = buf.indexOf("\n\n")) >= 0) {                          // one server-sent event per blank line
          const evt = buf.slice(0, cut); buf = buf.slice(cut + 2);
          const name = (evt.match(/^event: (.*)$/m) || [])[1], data = (evt.match(/^data: (.*)$/m) || [])[1];
          if (!data) continue;
          const j = JSON.parse(data);
          if (name === "delta") { acc += j.text || ""; try { onDelta(acc); } catch { /* display only */ } }
          else if (name === "done") done = j;
        }
      }
      if (!done) throw new Error("the answer stream ended early");
      return fromServer(done, hits);
    }
  }
  const r = await fetch(apiUrl("/api/v1/ask"), { method: "POST", headers, body, signal });
  noteLimit(r);
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.error) throw new Error(j.error || "HTTP " + r.status);
  return fromServer(j, hits);
}

async function callLlm(system, user, cfg, onDelta) {
  if (Date.now() < aiBlockedUntil) throw new Error(cooldownMsg());     // don't call while the server asked us to wait
  const ac = new AbortController(); current = ac;
  try { return await callLlmInner(system, user, cfg, onDelta, ac.signal); }
  catch (e) { if (ac.signal.aborted) throw new Error(STOPPED); throw e; }
  finally { if (current === ac) current = null; }
}

async function callLlmInner(system, user, cfg, onDelta, signal) {
  let r, j;
  if (cfg.provider === "ollama") {
    r = await fetch("http://localhost:11434/api/chat", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: cfg.model || "llama3.2", stream: false,
        messages: [{ role: "system", content: system }, { role: "user", content: user }] }) });
    j = await r.json();
    if (!r.ok) throw new Error(j.error || r.status);
    return j.message.content;
  }
  throw new Error("provider " + cfg.provider + " not supported in smart mode");
}

// ---------------- Voice: speech-to-text ----------------
/** Sends recorded audio to the local server, which forwards it to Groq Whisper. Returns the text. */
export async function transcribe(blob, lang = "", hint = "") {
  const q = new URLSearchParams();
  if (lang) q.set("lang", lang);
  if (hint) q.set("prompt", hint);                // spelling hint for Whisper, e.g. "Hey Glasses, "
  const r = await fetch(apiUrl("/api/stt" + (q.toString() ? "?" + q : "")), { method: "POST",
    headers: authHeaders({ "Content-Type": blob.type || "audio/webm" }), body: blob });
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error?.message || j.error || r.status);
  return (j.text || "").trim();
}

// ---------------- Hybrid search on the Python backend ----------------
/** Asks the Python backend (embeddings + TF-IDF). Returns hits in the same shape as RagIndex.search,
 *  or null when the backend is not there (PowerShell server / offline) so the caller can fall back. */
/** /api/v1/search (the shared API every glasses client uses), returned in the old /api/search shape.
 *  null = this server has no /api/v1 (then the caller falls back to /api/search). */
async function searchV1(query, k, machine) {
  const r = await fetch(apiUrl("/api/v1/search"), { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ query, k, machine_id: machine }), signal: AbortSignal.timeout(10000) });
  if (r.status === 404 || r.status === 405) { v1Ok = false; return null; }
  if (!r.ok) throw new Error("HTTP " + r.status);
  const j = await r.json();
  return { mode: j.mode, version: j.index_version, conf: j.confidence, lowConfidence: j.low_confidence,
    machine: j.machine, machineMatched: j.machine_matched, engine: j.engine,
    hits: (j.passages || []).map((p) => ({ i: p.i, score: p.score, sem: p.semantic, kw: p.keyword })) };
}

let v1Ok = true;            // false after a 404: older server without /api/v1 -> keep using /api/search
export async function serverSearch(index, query, k = 8, machine = "") {
  try {
    let j = v1Ok ? await searchV1(query, k, machine) : null;
    if (!j) {
      const r = await fetch(apiUrl("/api/search"), { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ query, k, machine }), signal: AbortSignal.timeout(10000) });
      if (!r.ok) return null;
      j = await r.json();
    }
    const hits = (j.hits || []).map((h) => {
      const chunk = index.data.chunks[h.i];
      return { chunk, doc: index.data.docs[chunk.doc], score: h.score, sem: h.sem, kw: h.kw, i: h.i };
    });
    return { mode: j.mode, version: j.version, hits: j.version && j.version !== index.data.version ? [] : hits,
      conf: j.conf, lowConfidence: !!j.lowConfidence, machine: j.machine, machineMatched: j.machineMatched, engine: j.engine };
  } catch { return null; }
}

// ---------------- ProcOps: every question is a bug report for the page ----------------
/** Logs a question + the page it was answered from to the ProcOps dashboard (/procops).
 *  Fire-and-forget: never throws, never blocks the answer (the PowerShell server simply returns 404). */
export function logProcOps(event) {
  try {
    fetch(apiUrl("/api/procops/event"), { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(event), keepalive: true }).catch(() => {});
  } catch { /* ignore */ }
}

// ---------------- Scan, then ask: photo + question ----------------
/** Is a vision model available on the backend? -> { available, model, reason } */
export async function visionStatus() {
  try {
    const r = await fetch(apiUrl("/api/vision/status"), { signal: AbortSignal.timeout(8000) });
    return r.ok ? await r.json() : { available: false, reason: "this server has no photo support" };
  } catch { return { available: false, reason: "backend not reachable" }; }
}

/** photo -> { object, text_seen, keywords } (used to search the documents) */
export const VR_VIEW_HINT = "This image is a frame of what the user sees through their smart glasses / VR headset " +
  "(the real world around them, or a 3D scene). " +
  "The red ring marks the centre of their view: describe the object inside or nearest to the red ring, not the ring itself.";

export async function describePhoto(image, hint = "") {
  const r = await fetch(apiUrl("/api/vision/describe"), { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ image, hint }), signal: AbortSignal.timeout(60000) });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || j.error) throw new Error(j.error || "HTTP " + r.status);
  return j;
}

const VISION_SYSTEM = SMART_SYSTEM +
  "\n5. The user also took a PHOTO with their smart glasses. You get a description of it from a vision model " +
  "(you do not see the photo yourself). Start with what the object is, then the key facts about it. " +
  "Only use SOURCE: PDF when an excerpt really is about this object or question.";

/** Same result shape as answerSmart: { text, mode, cited }.
 *  Only ONE vision call per photo (describePhoto); the answer comes from the normal chat model using that
 *  description, which keeps the scan inside Groq's free-tier tokens-per-minute limit. */
export async function answerVision(question, photoInfo, hits, image, hint = "", cfg = { provider: "groq" }, onDelta) {
  if (cfg.provider === "none" || usesServer(cfg)) return askServerSafe(question, hits.slice(0, 6), photoInfo, onDelta,
    (e) => ({ text: `I see: ${photoInfo.object}. ${photoInfo.details || ""}\n\n(AI answer failed: ${e.message})`, mode: "general", cited: [] }));
  const seen = "PHOTO DESCRIPTION (from the vision model): " + (photoInfo.object || "unknown object") + ". " +
    (photoInfo.details ? photoInfo.details + " " : "") + "Text on it: " + (photoInfo.text_seen || "none") + ".\n\n";
  const user = seen + (hits.length ? buildPrompt(question, hits.slice(0, 6)) : "Excerpts: (none matched)\n\nQuestion: " + question);
  let raw;
  try { raw = await callLlm(VISION_SYSTEM + styleRules(), user, cfg, onDelta); }     // local Ollama only
  catch (e) {
    if (isAbort(e)) return { text: "Stopped.", mode: "offline", cited: [], stopped: true };
    return { text: `I see: ${photoInfo.object}. ${photoInfo.details || ""}\n\n(AI answer failed: ${e.message})`, mode: "general", cited: [] };
  }
  return parseSmart(raw, hits.slice(0, 6));
}
