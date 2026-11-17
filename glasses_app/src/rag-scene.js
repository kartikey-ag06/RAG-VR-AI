// rag-scene.js - VR_RAG on Meta VR Glasses (IWSDK).
// Question -> RAG search -> answer panel + real PDF page panel floating in front of you.
//
// Glasses input (emulated by IWER as "Gaze + Hands"):
//   pinch (select)   = next PDF page
//   squeeze / grip   = next source
// Keyboard: type in the box (top-left) + Enter. Tab = next source, [ / ] = prev / next page.
import { Mesh, PlaneGeometry, MeshBasicMaterial, CanvasTexture, SRGBColorSpace, DoubleSide } from '@iwsdk/core';
import { RagIndex, pageImageUrl, loadLlmConfig, answerSmart, serverSearch, serverIndexVersion } from './rag.js';
import { setupVoice } from './voice.js';

const SAMPLES = [
  'What penalty was imposed on Reliance Industries?',
  'Why was Reliance penalised for diluted earnings per share disclosure?',
  'What did Reliance Petroleum do in the futures and cash market in 2007?',
  'What did the Bombay High Court order say?',
  'What is insider trading in simple words?',
];

export function setupRag(world) {
  const state = { index: null, llm: null, hits: [], hitIdx: 0, doc: -1, page: 1, busy: false, query: '', answer: '', mode: '', status: 'Loading...' };

  // ---------- DOM overlay (desktop typing box) ----------
  const ui = document.createElement('div');
  ui.innerHTML = `
    <style>
      #rag-ui{position:fixed;top:10px;left:10px;width:380px;max-height:calc(100% - 20px);overflow:auto;z-index:2147483000;
        background:rgba(15,20,32,.93);border:1px solid #2a3550;border-radius:10px;padding:12px;color:#e8e8e8;font:13px Segoe UI,Arial,sans-serif}
      #rag-ui h1{font-size:15px;margin:0 0 4px;color:#8cc8ff} #rag-upl{font-size:11px;color:#8cc8ff;margin-left:6px;text-decoration:none;border:1px solid #3a4766;border-radius:6px;padding:2px 7px} #rag-upl:hover{background:#26345a} #rag-status{font-size:11px;color:#8a93a6;margin-bottom:8px}
      #rag-q{width:100%;box-sizing:border-box;padding:8px;border-radius:6px;border:1px solid #3a4766;background:#0d1220;color:#fff;font-size:13px}
      #rag-ui button{margin:5px 5px 0 0;padding:5px 9px;border-radius:6px;border:1px solid #3a4766;background:#1b2540;color:#dfe8ff;cursor:pointer;font-size:11px}
      #rag-ans{white-space:pre-wrap;margin-top:10px;line-height:1.4} #rag-src{white-space:pre-wrap;margin-top:8px;color:#ffd98a;font-size:11px}
      #rag-ui .k{font-size:10px;color:#7d879c;margin-top:8px} #rag-ui.min #rag-body{display:none}
      #rag-row{display:flex;gap:6px} #rag-row #rag-q{flex:1} #rag-mic{margin:0 !important;white-space:nowrap;font-size:12px !important}
      #rag-mic.rec{background:#7a1f2b !important;border-color:#c0394f !important;animation:ragpulse 1s infinite}
      @keyframes ragpulse{50%{opacity:.6}} #rag-voice{font-size:11px;color:#ffb3c0;min-height:14px;margin-top:4px}
    </style>
    <div id="rag-ui"><h1>VR_RAG · Meta VR Glasses <button id="rag-min" title="hide/show">_</button> <a id="rag-upl" href="/upload" target="_blank" title="Upload PDF / DOCX / TXT">＋ Upload docs</a></h1>
      <div id="rag-body"><div id="rag-status">Loading...</div>
      <div id="rag-row"><input id="rag-q" placeholder="Ask about your documents, or anything... (Enter)" autocomplete="off"><button id="rag-mic" title="Click to talk, or hold V">🎤 Speak</button></div>
      <div id="rag-voice"></div>
      <div id="rag-samples"></div><div id="rag-ans"></div><div id="rag-src"></div>
      <div class="k">Enter = ask · hold V or 🎤 = speak · Tab = next source · [ ] = PDF page · In glasses: pinch = next page, squeeze = next source</div></div></div>`;
  document.body.appendChild(ui);
  const $ = (id) => document.getElementById(id);
  $('rag-min').onclick = () => $('rag-ui').classList.toggle('min');
  SAMPLES.forEach((s, i) => {
    const b = document.createElement('button');
    b.textContent = `Sample ${i + 1}`; b.title = s; b.onclick = () => ask(s);
    $('rag-samples').appendChild(b);
  });

  // ---------- 3D panels ----------
  function makePanel(wPx, hPx, wM, pos, yaw) {
    const canvas = document.createElement('canvas');
    canvas.width = wPx; canvas.height = hPx;
    const tex = new CanvasTexture(canvas);
    tex.colorSpace = SRGBColorSpace; tex.anisotropy = 8;
    const mesh = new Mesh(new PlaneGeometry(wM, (wM * hPx) / wPx),
      new MeshBasicMaterial({ map: tex, transparent: true, side: DoubleSide }));
    mesh.position.set(...pos); mesh.rotation.y = yaw;
    world.createTransformEntity(mesh);
    return { canvas, ctx: canvas.getContext('2d'), tex };
  }
  // Meta VR Glasses have a narrower field of view than Quest, so panels sit close to the centre of view.
  const ansP = makePanel(1024, 1280, 0.72, [-0.39, 1.5, -1.25], 0.28);
  const pdfP = makePanel(1024, 1400, 0.66, [0.37, 1.5, -1.25], -0.28);

  function wrap(ctx, text, x, y, maxW, lh, maxY) {
    for (const para of String(text).split('\n')) {
      let line = '';
      for (const word of para.split(' ')) {
        const t = line ? line + ' ' + word : word;
        if (ctx.measureText(t).width > maxW && line) {
          ctx.fillText(line, x, y); y += lh; line = word;
          if (y > maxY) { ctx.fillText('...', x, y); return y; }
        } else line = t;
      }
      ctx.fillText(line, x, y); y += lh;
      if (y > maxY) return y;
    }
    return y;
  }

  function drawAnswer() {
    const { ctx, canvas, tex } = ansP;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = 'rgba(16,22,36,0.96)'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.strokeStyle = '#2f4470'; ctx.lineWidth = 6; ctx.strokeRect(3, 3, canvas.width - 6, canvas.height - 6);
    ctx.textBaseline = 'top';
    ctx.fillStyle = '#8cc8ff'; ctx.font = 'bold 44px Segoe UI, Arial'; ctx.fillText('VR_RAG  |  Your documents', 40, 30);
    ctx.fillStyle = '#fff'; ctx.font = 'bold 34px Segoe UI, Arial';
    let y = wrap(ctx, 'Q: ' + state.query + (state.busy ? '' : '_'), 40, 100, 944, 42, 240);
    let ay = Math.max(y + 20, 200);
    const badge = { pdf: ['FROM THE SEBI PDFs', '#2e7d4f'], general: ['GENERAL AI ANSWER - not from the PDFs', '#8a5a14'], offline: ['OFFLINE - best matching PDF sentences', '#4a5568'] }[state.mode];
    if (badge) {
      ctx.font = 'bold 24px Segoe UI, Arial'; const w = ctx.measureText(badge[0]).width + 30;
      ctx.fillStyle = badge[1]; ctx.fillRect(40, ay, w, 38); ctx.fillStyle = '#fff'; ctx.fillText(badge[0], 55, ay + 7); ay += 55;
    }
    ctx.fillStyle = '#e6e6e6'; ctx.font = '29px Segoe UI, Arial';
    wrap(ctx, state.answer, 40, ay, 944, 37, 980);
    ctx.fillStyle = '#ffd98a'; ctx.font = '24px Segoe UI, Arial';
    let sy = 1010;
    if (state.hits.length) {
      ctx.fillText('PDF sources used (squeeze / Tab = next):', 40, sy); sy += 32;
      state.hits.forEach((h, i) => {
        const t = `${i === state.hitIdx ? '>' : '  '} [${i + 1}] ${h.doc.title} - p.${h.chunk.page}`;
        ctx.fillText(t.length > 68 ? t.slice(0, 68) + '...' : t, 40, sy); sy += 30;
      });
    }
    ctx.fillStyle = '#6f7a90'; ctx.font = 'italic 20px Segoe UI, Arial'; ctx.fillText(state.status.slice(0, 90), 40, 1242);
    tex.needsUpdate = true;
  }

  function drawPdf(img, caption) {
    const { ctx, canvas, tex } = pdfP;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = 'rgba(16,22,36,0.96)'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (img) {
      const s = Math.min(984 / img.width, 1300 / img.height), w = img.width * s, h = img.height * s;
      ctx.drawImage(img, (canvas.width - w) / 2, 20, w, h);
    }
    ctx.fillStyle = '#cfe3ff'; ctx.font = '24px Segoe UI, Arial'; ctx.textBaseline = 'top';
    ctx.fillText(caption.slice(0, 80), 30, 1345);
    tex.needsUpdate = true;
  }

  const cache = new Map();
  function showPage(docId, page) {
    if (!state.index || docId < 0) return;
    const doc = state.index.data.docs[docId];
    page = Math.min(Math.max(page, 1), doc.pages);
    state.doc = docId; state.page = page;
    const url = pageImageUrl(docId, page);
    const cap = `${doc.title}  |  page ${page}/${doc.pages}  (pinch = next)`;
    const done = (img) => { if (state.doc === docId && state.page === page) drawPdf(img, cap); };
    if (cache.has(url)) return done(cache.get(url));
    const img = new Image();
    img.onload = () => { cache.set(url, img); done(img); };
    img.onerror = () => drawPdf(null, 'Missing image ' + url);
    img.src = url;
  }

  function syncDom() {
    $('rag-status').textContent = state.status;
    const label = { pdf: '📄 From the SEBI PDFs', general: '🌐 General AI answer (not from the PDFs)', offline: '⚙ Offline (best matching PDF sentences)' }[state.mode] || '';
    $('rag-ans').textContent = (label ? label + '\n\n' : '') + state.answer;
    $('rag-src').textContent = state.hits.length
      ? 'PDF sources used:\n' + state.hits.map((h, i) => `${i === state.hitIdx ? '▶' : '  '} [${i + 1}] ${h.doc.title} - p.${h.chunk.page}`).join('\n') : '';
    drawAnswer();
  }

  async function ask(q) {
    q = (q || '').trim();
    if (!q || state.busy || !state.index) return;
    state.busy = true; state.query = q; $('rag-q').value = q;
    state.mode = ''; state.hits = []; state.hitIdx = 0;
    // R: retrieve up to 8 matching PDF chunks - hybrid (embeddings + TF-IDF) on the Python backend,
    //    or plain TF-IDF in the browser when the backend is not running
    await syncIndex();                                  // pick up uploads / deletions made on the /upload page
    const srv = await serverSearch(state.index, q, 8);
    let found = srv ? srv.hits : state.index.search(q, 8);
    if (srv && srv.version && srv.version !== state.index.data.version) {   // documents changed mid-question
      await syncIndex();
      const again = await serverSearch(state.index, q, 8);
      found = again ? again.hits : state.index.search(q, 8);
    }
    state.searchMode = srv ? srv.mode : 'keyword (browser)';
    state.answer = state.llm.provider === 'none' ? 'Searching the PDFs...' : `Asking ${state.llm.provider}...`;
    syncDom();
    const res = await answerSmart(q, found, state.llm); // G: AI answers from PDFs or general knowledge
    state.answer = res.text; state.mode = res.mode;
    state.hits = res.cited.map((i) => found[i]);       // only the chunks the AI actually used
    if (state.hits.length) showPage(state.hits[0].doc.id, state.hits[0].chunk.page);
    else { state.doc = -1; drawPdf(null, res.mode === 'general' ? 'No PDF page - this answer is general knowledge' : 'No matching PDF page'); }
    state.busy = false; syncDom();
    $('rag-status').textContent = state.status + ` · last search: ${state.searchMode}`;
  }

  // Reloads index.json when the server's document set changed (upload page), so new files are searchable in VR.
  async function syncIndex() {
    const v = await serverIndexVersion();
    if (v === null || v === state.index.data.version) return;
    state.index = await RagIndex.load();
    cache.clear();
    const d = state.index.data;
    state.status = state.status.replace(/^\d+ docs? · \d+ chunks|^\d+ SEBI orders · \d+ chunks/, `${d.docs.length} docs · ${d.chunks.length} chunks`);
  }

  function nextHit() {
    if (!state.hits.length) return;
    state.hitIdx = (state.hitIdx + 1) % state.hits.length;
    const h = state.hits[state.hitIdx];
    showPage(h.doc.id, h.chunk.page); syncDom();
  }

  // ---------- input ----------
  $('rag-q').addEventListener('input', (e) => { state.query = e.target.value; drawAnswer(); });
  $('rag-q').addEventListener('keydown', (e) => {
    e.stopPropagation(); // keep typed letters away from the emulator's movement keys
    if (e.key === 'Enter') { e.preventDefault(); ask(e.target.value); }
    if (e.key === 'Tab') { e.preventDefault(); nextHit(); }
  }, true);
  window.addEventListener('keydown', (e) => {
    if (document.activeElement === $('rag-q')) return;
    if (e.key === ']') showPage(state.doc, state.page + 1);
    else if (e.key === '[') showPage(state.doc, state.page - 1);
    else if (e.key === 'Tab') { e.preventDefault(); nextHit(); }
  });

  // gaze + pinch / controller input inside the XR session
  world.renderer.xr.addEventListener('sessionstart', () => {
    const s = world.renderer.xr.getSession();
    s.addEventListener('select', () => showPage(state.doc, state.page + 1));
    s.addEventListener('squeeze', nextHit);
  });

  // ---------- boot ----------
  (async () => {
    try {
      state.index = await RagIndex.load();
      state.llm = await loadLlmConfig();
      const d = state.index.data;
      state.status = `${d.docs.length} docs · ${d.chunks.length} chunks · LLM: ${state.llm.provider}${state.llm.model ? ' (' + state.llm.model + ')' : ''} · search: ${state.llm.search || 'keyword (browser)'}`;
      state.answer = 'Ask ANY question in the box (top-left) and press Enter.\nIf the answer is in the SEBI PDFs you will see the PDF page; otherwise you get a general AI answer.\n\nExamples:\n' + SAMPLES.map((s, i) => `${i + 1}. ${s}`).join('\n');
    } catch (e) {
      state.status = 'ERROR: ' + e.message; state.answer = state.status;
    }
    syncDom(); drawPdf(null, 'PDF page appears here');

    // ---------- voice ----------
    const v = setupVoice({
      button: $('rag-mic'),
      useServer: !!(state.llm && state.llm.stt),
      onState: (kind, msg) => {
        $('rag-voice').textContent = msg;
        if (kind === 'listening' || kind === 'transcribing') { state.query = '[mic] ' + msg; drawAnswer(); }
        if (kind === 'error') { state.query = $('rag-q').value; drawAnswer(); }
      },
      onText: (text) => { $('rag-voice').textContent = 'Heard: "' + text + '"'; ask(text); },
    });
    state.status += ` · voice: ${v.mode === 'groq' ? 'Groq Whisper' : v.mode === 'browser' ? 'Chrome speech' : 'off'}`;
    syncDom();
  })();

  window.__vrRag = { state, ask }; // handy for debugging in DevTools
}
