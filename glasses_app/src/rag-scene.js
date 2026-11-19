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
import { setupApps, APPS } from './apps.js';

const PHOTO_Q = 'What is this, and what do my documents say about it?';
const VIEW_Q = 'What is the object in the middle of my view? Give key facts about it.';

const EXAMPLES = [
  'What penalty was imposed on Reliance Industries?',
  'Why was Reliance penalised for diluted earnings per share disclosure?',
  'What is insider trading in simple words?',
];

export function setupRag(world) {
  const state = { photoHint: '', photo: null, photoImg: null, photoInfo: null, usedPhoto: false, vision: null, index: null, llm: null, hits: [], hitIdx: 0, doc: -1, page: 1, busy: false, query: '', answer: '', mode: '', status: 'Loading...' };

  // ---------- DOM overlay: voice-only control panel (top-left) + apps drawer (right) ----------
  const ui = document.createElement('div');
  ui.innerHTML = `
    <style>
      #rag-ui{position:fixed;top:12px;left:12px;width:340px;max-height:calc(100% - 24px);overflow:auto;z-index:2147483000;box-sizing:border-box;
        background:rgba(13,18,30,.94);border:1px solid #26324d;border-radius:14px;padding:14px;color:#e6eaf2;font:13px/1.45 "Segoe UI",Arial,sans-serif;
        box-shadow:0 8px 28px rgba(0,0,0,.35)}
      #rag-ui button{font:inherit;cursor:pointer;border-radius:10px;border:1px solid #30406a;background:#18213a;color:#dfe8ff}
      #rag-ui button:hover:not(:disabled){background:#22305a} #rag-ui button:disabled{opacity:.45;cursor:not-allowed}
      .rg-hd{display:flex;align-items:center;gap:8px;margin-bottom:12px}
      .rg-hd b{font-size:15px;color:#8cc8ff} .rg-hd span{font-size:11px;color:#8a95ab;flex:1}
      #rag-min{width:26px;height:26px;padding:0;font-size:14px}
      #rag-ui.min #rag-body{display:none} #rag-ui.min .rg-hd{margin:0}
      #rag-mic{width:100%;padding:16px 10px;font-size:16px !important;font-weight:600;background:linear-gradient(180deg,#2b5cff,#2447c9) !important;
        border-color:#4d74ff !important;color:#fff !important;border-radius:14px !important}
      #rag-mic.rec{background:linear-gradient(180deg,#d6334f,#a8213a) !important;border-color:#ff6b84 !important;animation:ragpulse 1s infinite}
      @keyframes ragpulse{50%{opacity:.7}}
      #rag-voice{font-size:12px;color:#a9b6d3;min-height:17px;margin:6px 2px 0;text-align:center}
      .rg-row{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:10px}
      .rg-row button{padding:9px 4px;font-size:12px}
      #rag-scan{background:#1d3566 !important}
      #rag-photo{display:none;align-items:center;gap:8px;margin-top:10px;font-size:12px;color:#cfe3ff;background:#141d33;border:1px solid #2f4470;border-radius:10px;padding:5px}
      #rag-photo img{width:54px;height:40px;object-fit:cover;border-radius:6px} #rag-photo span{flex:1} #rag-photo button{width:26px;height:26px;padding:0}
      #rag-ans{white-space:pre-wrap;margin-top:12px;padding-top:10px;border-top:1px solid #222d47;line-height:1.5}
      #rag-src{white-space:pre-wrap;margin-top:8px;color:#ffd98a;font-size:11.5px}
      .rg-ft{margin-top:12px;padding-top:8px;border-top:1px solid #222d47;font-size:11px;color:#7d879c;display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center}
      .rg-ft a{color:#8cc8ff;text-decoration:none} .rg-ft a:hover{text-decoration:underline}
      #rag-status{width:100%}
      #rag-drawer{position:fixed;top:12px;right:12px;width:300px;max-height:calc(100% - 24px);overflow:auto;z-index:2147483000;box-sizing:border-box;
        background:rgba(13,18,30,.96);border:1px solid #26324d;border-radius:14px;padding:14px;color:#e6eaf2;font:13px/1.45 "Segoe UI",Arial,sans-serif;
        box-shadow:0 8px 28px rgba(0,0,0,.35);transform:translateX(calc(100% + 24px));transition:transform .2s ease}
      #rag-drawer.open{transform:none}
      #rag-drawer h3{margin:0 0 10px;font-size:15px;color:#8cc8ff;display:flex;justify-content:space-between;align-items:center}
      #rag-drawer button{font:inherit;cursor:pointer;border-radius:10px;border:1px solid #30406a;background:#18213a;color:#dfe8ff}
      #rag-drawer button:hover{background:#22305a}
      .rg-apps{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
      .rg-app{display:flex;flex-direction:column;align-items:center;gap:4px;padding:10px 4px !important;font-size:11.5px !important}
      .rg-app i{font-style:normal;font-size:22px}
      #rag-step{display:none;margin-top:12px;background:#141d33;border:1px solid #2f4470;border-radius:10px;padding:10px;font-size:12px}
      #rag-step ol{margin:6px 0 8px 18px;padding:0} #rag-step .go{width:100%;padding:9px;background:#2447c9 !important;border-color:#4d74ff !important;color:#fff;font-weight:600}
      .rg-wins{margin-top:12px} .rg-wins h4{margin:0 0 6px;font-size:12px;color:#8a95ab;font-weight:600}
      .rg-win{display:flex;align-items:center;gap:6px;padding:6px 8px;background:#141d33;border-radius:8px;margin-bottom:5px}
      .rg-win span{flex:1} .rg-win button{padding:3px 8px;font-size:11px}
      .rg-note{font-size:11px;color:#7d879c;margin-top:10px}
    </style>
    <div id="rag-ui">
      <div class="rg-hd"><b>VR_RAG</b><span>Meta VR Glasses · voice assistant</span><button id="rag-min" title="Hide / show">–</button></div>
      <div id="rag-body">
        <button id="rag-mic" title="Tap to talk (tap again to stop), or hold V">🎤 Tap to ask</button>
        <div id="rag-voice">Hold <b>V</b> or tap the mic, then speak</div>
        <div class="rg-row">
          <button id="rag-scan" title="Scan the object in the red ring of your view (or press X)">🔍 Scan</button>
          <button id="rag-cam" title="Use the webcam or upload a photo">📷 Photo</button>
          <button id="rag-apps" title="Open apps (Chrome, WhatsApp, Instagram, Email...) as VR windows">☰ Apps</button>
        </div>
        <div id="rag-photo"><img id="rag-photo-img" alt=""><span id="rag-photo-txt">Photo attached</span><button id="rag-photo-x" title="Remove photo">✕</button></div>
        <div id="rag-ans"></div><div id="rag-src"></div>
        <div class="rg-ft"><span id="rag-status">Loading...</span><a href="/upload" target="_blank">＋ Upload docs</a><a href="/procops" target="_blank">ProcOps</a>
          <span>V = talk · X = scan · Tab = next source · [ ] = page</span></div>
      </div>
    </div>
    <div id="rag-drawer">
      <h3>☰ Apps <button id="rag-apps-x" style="width:26px;height:26px;padding:0">✕</button></h3>
      <div class="rg-apps" id="rag-applist"></div>
      <div id="rag-step"></div>
      <div class="rg-wins"><h4>Open in VR</h4><div id="rag-wins"><div class="rg-note">No app windows yet.</div></div></div>
      <div class="rg-note">Apps are mirrored from a real Chrome tab: you see them live in VR, but click and type in the real tab.</div>
    </div>`;
  document.body.appendChild(ui);
  const $ = (id) => document.getElementById(id);
  $('rag-min').onclick = () => $('rag-ui').classList.toggle('min');

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
    state.busy = true; state.query = q;
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
      const cap = await captureVrView(world, [ansP.mesh, pdfP.mesh, ...apps.meshes()], [$('rag-ui'), $('rag-drawer'), aimRing]);
      shot = cap.image; state.scanHow = cap.how;
    } catch (e) {
      state.answer = (e.name === 'NotAllowedError' ? 'Tab sharing was not allowed, so the room could not be captured. ' : 'Could not capture the VR view: ' + e.message + '. ') +
        'Tip: turn off the Immersive Web Emulator extension, or use 📷 to upload a photo.';
      state.mode = 'offline'; syncDom(); return;
    }
    finally { scanning = false; }
    setPhoto(shot, VR_VIEW_HINT);
    $('rag-photo-txt').textContent = 'VR view scanned';
    bringPanelsToView();
    ask(VIEW_Q);
  }
  $('rag-scan').onclick = scanView;
  window.addEventListener('keydown', (e) => {
    if ((e.key === 'x' || e.key === 'X') && !e.repeat && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) scanView();
  });
  const aimRing = addAimRing();
  hideStudioInPassthrough(world);

  $('rag-photo-x').onclick = () => setPhoto(null);

  // ---------- ☰ Apps: mirror real Chrome tabs (WhatsApp, Instagram, Email...) as VR windows ----------
  const apps = setupApps({ world, onChange: renderWins });
  function renderWins(list) {
    $('rag-wins').innerHTML = list.length ? '' : '<div class="rg-note">No app windows yet.</div>';
    list.slice().reverse().forEach((w, i) => {
      const row = document.createElement('div'); row.className = 'rg-win';
      row.innerHTML = `<span>${w.icon} ${w.name}${i === 0 ? ' <small style="color:#8a95ab">(in front)</small>' : ''}</span>`;
      const fr = document.createElement('button'); fr.textContent = 'Front'; fr.title = 'Bring in front of you'; fr.onclick = () => apps.front(w.id);
      const x = document.createElement('button'); x.textContent = '✕'; x.title = 'Close window'; x.onclick = () => apps.close(w.id);
      if (i) row.appendChild(fr);
      row.appendChild(x); $('rag-wins').appendChild(row);
    });
  }
  function showStep(app) {
    const st = $('rag-step'); st.style.display = 'block';
    const opened = !!app.url;
    st.innerHTML = `<b>${app.icon} ${app.name}</b><ol>` +
      (opened ? `<li>${app.name} opened in a new Chrome tab - sign in there if needed.</li><li>Come back here and click <b>Show in VR</b>.</li><li>In Chrome's box, pick the <b>${app.name}</b> tab and click <b>Share</b>.</li>`
        : `<li>Click <b>Show in VR</b>.</li><li>Pick any Chrome tab or window and click <b>Share</b>.</li>`) +
      `</ol><button class="go" id="rag-step-go">Show ${app.name} in VR</button><div class="rg-note" id="rag-step-msg"></div>`;
    $('rag-step-go').onclick = async () => {
      try { await apps.open(app); st.style.display = 'none'; }
      catch (e) { $('rag-step-msg').textContent = e.name === 'NotAllowedError' ? 'Sharing was cancelled.' : 'Could not show it: ' + e.message; }
    };
  }
  APPS.forEach((app) => {
    const b = document.createElement('button'); b.className = 'rg-app';
    b.innerHTML = `<i>${app.icon}</i>${app.name}`; b.title = app.note || ('Open ' + app.name + ' and show it in VR');
    b.onclick = () => { if (app.url) window.open(app.url, '_blank', 'noopener'); showStep(app); };
    $('rag-applist').appendChild(b);
  });
  if (!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia)) $('rag-applist').insertAdjacentHTML('afterend', '<div class="rg-note">This browser cannot share tabs - use Chrome on a computer.</div>');
  $('rag-apps').onclick = () => $('rag-drawer').classList.toggle('open');
  $('rag-apps-x').onclick = () => $('rag-drawer').classList.remove('open');

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
  window.addEventListener('keydown', (e) => {
    if (/INPUT|TEXTAREA/.test(document.activeElement.tagName)) return;
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
      state.status = `${d.docs.length} docs · AI: ${state.llm.provider} · search: ${(state.llm.search || 'keyword').split(' ')[0]}`;
      state.answer = 'Hold V (or tap the mic) and ASK by voice.\nIf the answer is in your documents you will see the page; otherwise you get a general AI answer.\n\nTry saying:\n' + EXAMPLES.map((s) => '• ' + s).join('\n') +
        '\n\n🔍 Scan = what is the object in the red ring\n☰ Apps = Chrome, WhatsApp, Instagram, Email in VR';
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
        if (kind === 'error') { state.query = ''; drawAnswer(); }
        if (kind === 'done' && !msg) $('rag-voice').textContent = '';
      },
      onText: (text) => { $('rag-voice').textContent = 'Heard: "' + text + '"'; ask(text); },
      label: '🎤 Tap to ask',
    });
    state.status += ` · voice: ${v.mode === 'groq' ? 'Whisper' : v.mode === 'browser' ? 'Chrome' : 'off'}`;
    if (v.mode === 'none') { $('rag-mic').disabled = true; $('rag-voice').textContent = 'Voice needs Chrome with a microphone'; }
    syncDom();

    // ---------- camera (scan, then ask) ----------
    setupCamera({ button: $('rag-cam'), onPhoto: (p) => { setPhoto(p); ask(PHOTO_Q); } });
    state.vision = await visionStatus();
    if (!state.vision.available) for (const id of ['rag-cam', 'rag-scan']) { $(id).disabled = true; $(id).title = 'Photo questions unavailable: ' + state.vision.reason; }
    state.status += ` · photo: ${state.vision.available ? 'on' : 'off'}`;
    syncDom();
  })();

  window.__vrRag = { state, ask, setPhoto, world, apps }; // handy for debugging in DevTools
}
