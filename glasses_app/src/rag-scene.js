// rag-scene.js - VR_RAG on Meta VR Glasses (IWSDK).
// Question -> RAG search -> answer panel + real PDF page panel floating in front of you.
//
// Glasses input (emulated by IWER as "Gaze + Hands"):
//   pinch (select)   = next PDF page
//   squeeze / grip   = next source
// Keyboard: type in the box (top-left) + Enter. Tab = next source, [ / ] = prev / next page.
import { Mesh, PlaneGeometry, MeshBasicMaterial, CanvasTexture, SRGBColorSpace, DoubleSide, Vector3 } from '@iwsdk/core';
import { RagIndex, pageImageUrl, loadLlmConfig, answerSmart, serverSearch, serverIndexVersion, logProcOps, answerVision, describePhoto, visionStatus, VR_VIEW_HINT } from './rag.js';
import { captureVrView, addAimRing, hideStudioInPassthrough } from './vrscan.js';
import { setupVoice } from './voice.js';
import { setupCamera } from './camera.js';

const PHOTO_Q = 'What is this, and what do my documents say about it?';
const VIEW_Q = 'What is the object in the middle of my view? Give key facts about it.';

const SAMPLES = [
  'What penalty was imposed on Reliance Industries?',
  'Why was Reliance penalised for diluted earnings per share disclosure?',
  'What did Reliance Petroleum do in the futures and cash market in 2007?',
  'What did the Bombay High Court order say?',
  'What is insider trading in simple words?',
];

export function setupRag(world) {
  const state = { photoHint: '', photo: null, photoImg: null, photoInfo: null, usedPhoto: false, vision: null, index: null, llm: null, hits: [], hitIdx: 0, doc: -1, page: 1, busy: false, query: '', answer: '', mode: '', status: 'Loading...' };

  // ---------- DOM overlay (desktop typing box) ----------
  const ui = document.createElement('div');
  ui.innerHTML = `
    <style>
      #rag-ui{position:fixed;top:10px;left:10px;width:380px;max-height:calc(100% - 20px);overflow:auto;z-index:2147483000;
        background:rgba(15,20,32,.93);border:1px solid #2a3550;border-radius:10px;padding:12px;color:#e8e8e8;font:13px Segoe UI,Arial,sans-serif}
      #rag-ui h1{font-size:15px;margin:0 0 4px;color:#8cc8ff} #rag-upl,#rag-po{font-size:11px;color:#8cc8ff;margin-left:6px;text-decoration:none;border:1px solid #3a4766;border-radius:6px;padding:2px 7px} #rag-upl:hover,#rag-po:hover{background:#26345a} #rag-status{font-size:11px;color:#8a93a6;margin-bottom:8px}
      #rag-q{width:100%;box-sizing:border-box;padding:8px;border-radius:6px;border:1px solid #3a4766;background:#0d1220;color:#fff;font-size:13px}
      #rag-ui button{margin:5px 5px 0 0;padding:5px 9px;border-radius:6px;border:1px solid #3a4766;background:#1b2540;color:#dfe8ff;cursor:pointer;font-size:11px}
      #rag-ans{white-space:pre-wrap;margin-top:10px;line-height:1.4} #rag-src{white-space:pre-wrap;margin-top:8px;color:#ffd98a;font-size:11px}
      #rag-ui .k{font-size:10px;color:#7d879c;margin-top:8px} #rag-ui.min #rag-body{display:none}
      #rag-row{display:flex;gap:6px} #rag-row #rag-q{flex:1} #rag-mic{margin:0 !important;white-space:nowrap;font-size:12px !important}
      #rag-mic.rec{background:#7a1f2b !important;border-color:#c0394f !important;animation:ragpulse 1s infinite}
      @keyframes ragpulse{50%{opacity:.6}} #rag-cam,#rag-scan{margin:0 !important;white-space:nowrap;font-size:12px !important} #rag-scan{background:#2a3f6e !important}
      #rag-photo{display:none;align-items:center;gap:8px;margin-top:6px;font-size:11px;color:#cfe3ff;background:#16203a;border:1px solid #2f4470;border-radius:8px;padding:4px}
      #rag-photo img{width:54px;height:40px;object-fit:cover;border-radius:5px} #rag-photo button{margin:0 0 0 auto !important} #rag-voice{font-size:11px;color:#ffb3c0;min-height:14px;margin-top:4px}
    </style>
    <div id="rag-ui"><h1>VR_RAG · Meta VR Glasses <button id="rag-min" title="hide/show">_</button> <a id="rag-upl" href="/upload" target="_blank" title="Upload PDF / DOCX / TXT">＋ Upload docs</a> <a id="rag-po" href="/procops" target="_blank" title="ProcOps: which pages confuse people, and fix them">ProcOps</a></h1>
      <div id="rag-body"><div id="rag-status">Loading...</div>
      <div id="rag-row"><input id="rag-q" placeholder="Ask about your documents, or anything... (Enter)" autocomplete="off"><button id="rag-mic" title="Click to talk, or hold V">🎤 Speak</button><button id="rag-scan" title="Scan the object in the middle of your VR view (red ring) - info appears in VR">🔍 Scan</button><button id="rag-cam" title="Use the webcam or upload a photo instead">📷</button></div>
      <div id="rag-photo"><img id="rag-photo-img" alt=""><span id="rag-photo-txt">Photo attached - ask about it</span><button id="rag-photo-x" title="Remove photo">✕</button></div>
      <div id="rag-voice"></div>
      <div id="rag-samples"></div><div id="rag-ans"></div><div id="rag-src"></div>
      <div class="k">Enter = ask · hold V or 🎤 = speak · 🔍 Scan = what is the object in the red ring (or press X) · 📷 = webcam / upload photo · Tab = next source · [ ] = PDF page · In glasses: pinch = next page, squeeze = next source</div></div></div>`;
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
    return { canvas, ctx: canvas.getContext('2d'), tex, mesh };
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
    const thumb = state.photoImg;
    if (thumb) {                                        // the scanned photo, top right
      const s = Math.min(230 / thumb.width, 172 / thumb.height), w = thumb.width * s, h = thumb.height * s;
      ctx.drawImage(thumb, 984 - w, 92, w, h); ctx.strokeStyle = '#8cc8ff'; ctx.lineWidth = 3; ctx.strokeRect(984 - w, 92, w, h);
    }
    ctx.fillStyle = '#fff'; ctx.font = 'bold 34px Segoe UI, Arial';
    let y = wrap(ctx, 'Q: ' + state.query + (state.busy ? '' : '_'), 40, 100, thumb ? 690 : 944, 42, 240);
    if (thumb) y = Math.max(y, 280);
    let ay = Math.max(y + 20, 200);
    const badge = { pdf: ['FROM THE SEBI PDFs', '#2e7d4f'], general: ['GENERAL AI ANSWER - not from the PDFs', '#8a5a14'], offline: ['OFFLINE - best matching PDF sentences', '#4a5568'] }[state.mode];
    if (badge) {
      const label = (state.usedPhoto ? 'PHOTO + ' : '') + badge[0];
      ctx.font = 'bold 24px Segoe UI, Arial'; const w = ctx.measureText(label).width + 30;
      ctx.fillStyle = badge[1]; ctx.fillRect(40, ay, w, 38); ctx.fillStyle = '#fff'; ctx.fillText(label, 55, ay + 7); ay += 55;
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
    const ver = doc.rev && doc.rev !== '1.0' ? ` v${doc.rev}` : '';
    const cap = `${doc.title}${ver}  |  page ${page}/${doc.pages}  (pinch = next)`;
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
    $('rag-ans').textContent = (label ? (state.usedPhoto ? '📷 Photo + ' : '') + label + '\n\n' : '') + state.answer;
    $('rag-src').textContent = state.hits.length
      ? 'PDF sources used:\n' + state.hits.map((h, i) => `${i === state.hitIdx ? '▶' : '  '} [${i + 1}] ${h.doc.title} - p.${h.chunk.page}`).join('\n') : '';
    drawAnswer();
  }

  async function ask(q) {
    q = (q || '').trim();
    if (!q && state.photo) q = PHOTO_Q;               // photo attached + empty box = "what is this?"
    if (!q || state.busy || !state.index) return;
    const t0 = performance.now();
    state.busy = true; state.query = q; $('rag-q').value = q;
    state.mode = ''; state.hits = []; state.hitIdx = 0; state.usedPhoto = false;
    if (state.photo) return askWithPhoto(q, t0);
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
    // ProcOps: log the question against the page it was answered from (page = procedure step)
    const top = state.hits[0];
    logProcOps(top ? { q, uid: top.doc.uid, page: top.chunk.page, mode: res.mode, ms: Math.round(performance.now() - t0) }
      : { q, mode: res.mode, ms: Math.round(performance.now() - t0) });
    $('rag-status').textContent = state.status + ` · last search: ${state.searchMode}`;
  }

  // Scan, then ask: 1) the vision model says what the object is, 2) documents are searched with the question
  // + those words, 3) the vision model answers from photo + excerpts (same SOURCE: PDF / GENERAL rule).
  async function askWithPhoto(q, t0) {
    const photo = state.photo;
    state.usedPhoto = true;
    try {
      if (!state.photoInfo) {
        state.answer = 'Looking at your photo...'; syncDom();
        try { state.photoInfo = await describePhoto(photo, state.photoHint); }
        catch (e) { state.answer = 'Could not read the photo: ' + e.message; state.mode = 'offline'; return; }
        $('rag-photo-txt').textContent = 'Photo: ' + (state.photoInfo.object || 'attached').slice(0, 60);
      }
      const info = state.photoInfo;
      const searchQ = [q, info.object, info.text_seen, info.keywords].filter(Boolean).join(' ');
      await syncIndex();
      const srv = await serverSearch(state.index, searchQ, 8);
      const found = srv ? srv.hits : state.index.search(searchQ, 8);
      state.searchMode = (srv ? srv.mode : 'keyword (browser)') + ' + photo';
      state.answer = `I see: ${info.object || 'an object'}. Checking your documents...`; syncDom();
      const res = await answerVision(q, info, found, photo, state.photoHint, state.llm);
      state.answer = res.text; state.mode = res.mode;
      state.hits = res.cited.map((i) => found[i]);
      if (state.hits.length) showPage(state.hits[0].doc.id, state.hits[0].chunk.page);
      else { state.doc = -1; drawPdf(null, res.mode === 'general' ? 'No PDF page - answered from the photo + general knowledge' : 'No matching PDF page'); }
      const top = state.hits[0], tag = `[${state.photoHint ? 'vr scan' : 'photo'}: ${(info.object || 'object').slice(0, 60)}] ${q}`;
      logProcOps(top ? { q: tag, uid: top.doc.uid, page: top.chunk.page, mode: res.mode, ms: Math.round(performance.now() - t0) }
        : { q: tag, mode: res.mode, ms: Math.round(performance.now() - t0) });
    } finally {
      state.busy = false; syncDom();
      $('rag-status').textContent = state.status + ` · last search: ${state.searchMode || ''}`;
    }
  }

  function setPhoto(dataUrl, hint = '') {
    state.photoHint = hint;
    state.photo = dataUrl; state.photoInfo = null; state.photoImg = null;
    if (!dataUrl) { $('rag-photo').style.display = 'none'; drawAnswer(); return; }
    const img = new Image();
    img.onload = () => { state.photoImg = img; drawAnswer(); };
    img.src = dataUrl;
    $('rag-photo-img').src = dataUrl; $('rag-photo-txt').textContent = 'Photo attached - type or speak a question, or press Enter';
    $('rag-photo').style.display = 'flex';
    $('rag-q').placeholder = 'Ask about the photo... (Enter = "' + PHOTO_Q + '")';
    $('rag-q').focus();
  }
  // After a scan the user may be facing anywhere: move both panels in front of their current view
  // (same layout as at start: answer left, PDF page right, slightly turned towards the user).
  function bringPanelsToView() {
    const cam = world.camera, pos = new Vector3(), f = new Vector3();
    cam.getWorldPosition(pos); cam.getWorldDirection(f);
    f.y = 0; if (f.lengthSq() < 1e-6) return; f.normalize();
    const right = new Vector3(-f.z, 0, f.x), yaw = Math.atan2(-f.x, -f.z);
    const place = (m, side, turn) => {
      m.position.copy(pos).addScaledVector(f, 1.25).addScaledVector(right, side); m.position.y = pos.y;
      m.rotation.set(0, yaw + turn, 0);
    };
    place(ansP.mesh, -0.39, 0.28); place(pdfP.mesh, 0.37, -0.28);
  }

  // 🔍 Scan VR view: grab the frame the user sees, then ask straight away (info shows on the VR answer panel)
  let scanning = false;
  async function scanView() {
    if (scanning || state.busy || !state.index) return;
    if (!state.vision || !state.vision.available) { state.answer = 'Scan needs a vision model: ' + ((state.vision && state.vision.reason) || 'not ready yet'); syncDom(); return; }
    let shot;
    scanning = true;
    try {
      const cap = await captureVrView(world, [ansP.mesh, pdfP.mesh], [$('rag-ui'), aimRing]);
      shot = cap.image; state.scanHow = cap.how;
    } catch (e) {
      state.answer = (e.name === 'NotAllowedError' ? 'Tab sharing was not allowed, so the room could not be captured. ' : 'Could not capture the VR view: ' + e.message + '. ') +
        'Tip: turn off the Immersive Web Emulator extension, or use 📷 to upload a photo.';
      state.mode = 'offline'; syncDom(); return;
    }
    finally { scanning = false; }
    const typed = $('rag-q').value.trim();
    setPhoto(shot, VR_VIEW_HINT);
    $('rag-photo-txt').textContent = 'VR view scanned';
    bringPanelsToView();
    ask(typed && typed !== state.query ? typed : VIEW_Q);
  }
  $('rag-scan').onclick = scanView;
  window.addEventListener('keydown', (e) => {
    if ((e.key === 'x' || e.key === 'X') && document.activeElement !== $('rag-q') && !e.repeat) scanView();
  });
  const aimRing = addAimRing();
  hideStudioInPassthrough(world);

  $('rag-photo-x').onclick = () => { setPhoto(null); $('rag-q').placeholder = 'Ask about your documents, or anything... (Enter)'; };

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

    // ---------- camera (scan, then ask) ----------
    setupCamera({ button: $('rag-cam'), onPhoto: setPhoto });
    state.vision = await visionStatus();
    if (!state.vision.available) for (const id of ['rag-cam', 'rag-scan']) { $(id).disabled = true; $(id).title = 'Photo questions unavailable: ' + state.vision.reason; }
    state.status += ` · photo: ${state.vision.available ? 'on' : 'off'}`;
    syncDom();
  })();

  window.__vrRag = { state, ask, setPhoto, world }; // handy for debugging in DevTools
}
