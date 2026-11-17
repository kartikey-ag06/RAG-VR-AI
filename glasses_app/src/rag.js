// rag.js - Retrieval (TF-IDF search) + Generation (Ollama / Claude / OpenAI / offline)
// Must stay in sync with tokenize() in rag_pipeline/build_index.py

const STOP = new Set(`a an the and or of to in on for by with at from as is are was were be been being this that these those it its
which who whom whose what when where why how not no nor but if then than so such into upon under over above below
shall will would should may might can could must do does did done have has had having he she they them his her their
i we you me us our your said also any all each other same per vide hereinafter thereof therein herein whereas
page sebi noticee noticees order`.split(/\s+/).filter(Boolean));

export function tokenize(text) {
  return (text.toLowerCase().match(/[a-z0-9]+/g) || []).filter(t => t.length > 1 && !STOP.has(t));
}

export class RagIndex {
  static async load(url = import.meta.env.BASE_URL + "rag/index.json") {
    const data = await (await fetch(url, { cache: "no-store" })).json();
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
    for (const ch of this.data.chunks) {
      let s = 0;
      for (let i = 0; i < ch.terms.length; i++) {
        const qw = q.get(ch.terms[i]);
        if (qw) s += qw * ch.weights[i];
      }
      if (s > 0) hits.push({ chunk: ch, doc: this.data.docs[ch.doc], score: s / norm });
    }
    hits.sort((a, b) => b.score - a.score);
    return hits.slice(0, topK);
  }
}

// The index version changes whenever documents are uploaded or deleted; adding it to page URLs
// stops the browser from showing a cached image of a page that now belongs to another document.
let indexVersion = 0;
export function pageImageUrl(docId, page) {
  return import.meta.env.BASE_URL + `rag/pages/d${String(docId).padStart(2, "0")}_p${String(page).padStart(3, "0")}.jpg?v=${indexVersion}`;
}

/** Current index version on the server (null when the server has no upload support). */
export async function serverIndexVersion() {
  try {
    const r = await fetch("/api/index/version", { cache: "no-store", signal: AbortSignal.timeout(3000) });
    return r.ok ? (await r.json()).version : null;
  } catch { return null; }
}

// ---------------- Generation ----------------
const SYSTEM = "You are a legal research assistant answering questions about SEBI (Securities and Exchange Board of India) " +
  "enforcement orders involving Reliance Industries. Answer ONLY from the provided excerpts. " +
  "Be concise (max 120 words). Cite sources as [n]. If the excerpts do not contain the answer, say so.";

export async function loadLlmConfig() {
  let cfg = { provider: "auto" };
  try { cfg = await (await fetch(import.meta.env.BASE_URL + "rag/llm_config.json")).json(); } catch { /* defaults */ }
  cfg.provider = (cfg.provider || "auto").toLowerCase();
  if (cfg.provider === "auto" || cfg.provider === "grok") {
    // Grok via our local server (server.ps1 holds the API key, the browser never sees it)
    try {
      const r = await fetch("/api/grok/status", { signal: AbortSignal.timeout(1500) });
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
  "You are the assistant inside a VR app. You receive numbered excerpts from the user's uploaded documents " +
  "(for example SEBI enforcement orders about Reliance Industries), plus a user question that may or may not be about them.\n" +
  "Rules:\n" +
  "1. If the excerpts contain the answer (fully or partly), answer from them and cite like [1], [2]. " +
  "Your FIRST line must be exactly: SOURCE: PDF [n, m] (the excerpt numbers you used, most important first).\n" +
  "2. If the excerpts do not contain the answer, answer from your general knowledge. Your FIRST line must be exactly: SOURCE: GENERAL\n" +
  "3. Then a blank line, then the answer in plain text, max 120 words.\n" +
  "4. Never invent citations, section numbers, case numbers or penalty amounts. If unsure, say so.";

/** returns { text, mode: "pdf" | "general" | "offline", cited: [hit indexes] } */
export async function answerSmart(question, hits, cfg) {
  if (cfg.provider === "none") {
    if (!hits.length) return { text: "No matching text in the 10 SEBI orders, and no AI is connected for general questions.", mode: "offline", cited: [] };
    return { text: extractive(question, hits), mode: "offline", cited: hits.map((_, i) => i) };
  }
  const user = (hits.length ? buildPrompt(question, hits) : "Excerpts: (none matched)\n\nQuestion: " + question);
  let raw;
  try { raw = await callLlm(SMART_SYSTEM, user, cfg); }
  catch (e) {
    return { text: `AI error (${cfg.provider}): ${e.message}\n\n` + (hits.length ? extractive(question, hits) : ""), mode: "offline", cited: hits.map((_, i) => i) };
  }
  const m = raw.match(/^\s*SOURCE:\s*(PDF|GENERAL)\s*(\[[^\]]*\])?\s*/i);
  let body = (m ? raw.slice(m[0].length) : raw).trim()
    .replace(/\*\*(.+?)\*\*/g, "$1").replace(/__(.+?)__/g, "$1").replace(/^#+\s*/gm, "").replace(/^\s*[-*]\s+/gm, "• "); // plain text for the VR panel
  let cited = [];
  const nums = (m && m[2] ? m[2] : "") + " " + body;
  for (const x of nums.matchAll(/\[(\d+(?:\s*,\s*\d+)*)\]/g))
    for (const n of x[1].split(",")) { const i = parseInt(n, 10) - 1; if (i >= 0 && i < hits.length && !cited.includes(i)) cited.push(i); }
  const mode = (m && m[1].toUpperCase() === "GENERAL") || !cited.length ? "general" : "pdf";
  if (mode === "general") cited = [];
  return { text: body, mode, cited };
}

async function callLlm(system, user, cfg) {
  let r, j;
  if (cfg.provider === "grok" || cfg.provider === "groq") {
    r = await fetch("/api/grok", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: [{ role: "system", content: system }, { role: "user", content: user }] }) });
    j = await r.json();
    if (!r.ok || j.error) throw new Error(j.error?.message || j.error || r.status);
    return j.choices[0].message.content;
  }
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

export async function generate(question, hits, cfg) {
  if (!hits.length) return "No matching text found in the 10 SEBI orders. Try other keywords (penalty, insider trading, diluted EPS...).";
  if (cfg.provider === "none") return extractive(question, hits);
  const prompt = buildPrompt(question, hits);
  try {
    let r, j;
    if (cfg.provider === "grok" || cfg.provider === "groq") {
      r = await fetch("/api/grok", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: [{ role: "system", content: SYSTEM }, { role: "user", content: prompt }] })
      });
      j = await r.json();
      if (!r.ok || j.error) throw new Error(j.error?.message || j.error || r.status);
      return j.choices[0].message.content.trim();
    }
    if (cfg.provider === "ollama") {
      r = await fetch(cfg.endpoint && cfg.endpoint.includes("/api/chat") ? cfg.endpoint : "http://localhost:11434/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ model: cfg.model || "llama3.2", stream: false,
          messages: [{ role: "system", content: SYSTEM }, { role: "user", content: prompt }] })
      });
      j = await r.json();
      if (!r.ok) throw new Error(j.error || r.status);
      return j.message.content.trim();
    }
    if (cfg.provider === "anthropic") {
      r = await fetch("https://api.anthropic.com/v1/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-api-key": cfg.apiKey, "anthropic-version": "2023-06-01",
          "anthropic-dangerous-direct-browser-access": "true" },
        body: JSON.stringify({ model: cfg.model || "claude-sonnet-5-5", max_tokens: 600, system: SYSTEM,
          messages: [{ role: "user", content: prompt }] })
      });
      j = await r.json();
      if (!r.ok) throw new Error(j.error?.message || r.status);
      return j.content.filter(c => c.type === "text").map(c => c.text).join("").trim();
    }
    if (cfg.provider === "openai") {
      r = await fetch(cfg.endpoint || "https://api.openai.com/v1/chat/completions", {
        method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer " + cfg.apiKey },
        body: JSON.stringify({ model: cfg.model || "gpt-4o-mini",
          messages: [{ role: "system", content: SYSTEM }, { role: "user", content: prompt }] })
      });
      j = await r.json();
      if (!r.ok) throw new Error(j.error?.message || r.status);
      return j.choices[0].message.content.trim();
    }
    return `Unknown provider "${cfg.provider}".\n\n` + extractive(question, hits);
  } catch (e) {
    return `LLM error (${cfg.provider}): ${e.message}\nFalling back to offline answer:\n\n` + extractive(question, hits);
  }
}

// ---------------- Voice: speech-to-text ----------------
/** Sends recorded audio to the local server, which forwards it to Groq Whisper. Returns the text. */
export async function transcribe(blob) {
  const r = await fetch("/api/stt", { method: "POST", headers: { "Content-Type": blob.type || "audio/webm" }, body: blob });
  const j = await r.json();
  if (!r.ok || j.error) throw new Error(j.error?.message || j.error || r.status);
  return (j.text || "").trim();
}

// ---------------- Hybrid search on the Python backend ----------------
/** Asks the Python backend (embeddings + TF-IDF). Returns hits in the same shape as RagIndex.search,
 *  or null when the backend is not there (PowerShell server / offline) so the caller can fall back. */
export async function serverSearch(index, query, k = 8) {
  try {
    const r = await fetch("/api/search", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query, k }), signal: AbortSignal.timeout(8000) });
    if (!r.ok) return null;
    const j = await r.json();
    const hits = (j.hits || []).map((h) => {
      const chunk = index.data.chunks[h.i];
      return { chunk, doc: index.data.docs[chunk.doc], score: h.score, sem: h.sem, kw: h.kw };
    });
    return { mode: j.mode, version: j.version, hits: j.version && j.version !== index.data.version ? [] : hits };
  } catch { return null; }
}
