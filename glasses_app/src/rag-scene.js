// rag-scene.js - COORDINATOR for VR_RAG on Meta VR Glasses (IWSDK).
// Question -> RAG search -> answer panel + real PDF page panel floating in front of you.
//
//   XRPanelManager (xr-panels.js)  the two 3D panels, page cache, drawing
//   UIOverlay      (ui-overlay.js) DOM control panel + apps drawer
//   VRVoicePlayer  (tts-player.js) spoken answers (sentences are spoken live while the answer streams in)
//   rag.js / voice.js / camera.js / vrscan.js / apps.js  engine, mic, webcam, VR scan, mirror windows
//
// Glasses input (emulated by IWER as "Gaze + Hands"):
//   pinch (select) = next PDF page, squeeze / grip = next source
// Keyboard: V = talk, X = scan, Tab = next source, [ / ] = prev / next page.
import { RagIndex, loadLlmConfig, answerSmart, serverSearch, serverIndexVersion, logProcOps, answerVision, describePhoto, visionStatus, VR_VIEW_HINT } from './rag.js';
import { captureVrView, addAimRing, hideStudioInPassthrough } from './vrscan.js';
import { setupVoice } from './voice.js';
import { VRVoicePlayer, LiveSpeaker } from './tts-player.js';
import { setupCamera } from './camera.js';
import { setupApps, APPS } from './apps.js';
import { XRPanelManager } from './xr-panels.js';
import { UIOverlay } from './ui-overlay.js';

const PHOTO_Q = 'What is this, and what do my documents say about it?';
const VIEW_Q = 'What is the object in the middle of my view? Give key facts about it.';

const EXAMPLES = [
  'What penalty was imposed on Reliance Industries?',
  'Why was Reliance penalised for diluted earnings per share disclosure?',
  'What is insider trading in simple words?',
];

export function setupRag(world) {
  const voice = new VRVoicePlayer();
  const live = new LiveSpeaker(voice);
  const state = {
    photoHint: '', photo: null, photoImg: null, photoInfo: null, usedPhoto: false, vision: null, index: null, llm: null,
    hits: [], hitIdx: 0, doc: -1, page: 1, busy: false, speakAnswers: true, query: '', answer: '', mode: '',
    level: '', confidence: null, status: 'Loading...',
  };
  const ui = new UIOverlay();
  const panels = new XRPanelManager(world, state);
  const $ = ui.$;

  const syncDom = () => { ui.render(state); panels.drawAnswer(); };
  const showPage = (docId, page) => panels.showPage(docId, page);
  const drawPdf = (img, cap) => panels.drawPdf(img, cap);

  /** shows what the retriever decided: confidence level + score (nothing when the browser-only fallback searched) */
  function setConfidence(srv) {
    state.level = srv && srv.level ? srv.level : '';
    state.confidence = srv && srv.confidence != null ? srv.confidence : null;
  }

  /** common tail of every answer: panel text, cited hits, PDF page */
  function showResult(res, found, noPdfNote) {
    state.answer = res.text; state.mode = res.mode;
    state.hits = res.cited.map((i) => found[i]);       // only the chunks the AI actually used
    if (state.hits.length) showPage(state.hits[0].doc.id, state.hits[0].chunk.page);
    else { state.doc = -1; drawPdf(null, res.mode === 'general' ? noPdfNote : 'No matching PDF page'); }
  }

  async function ask(q) {
    q = (q || '').trim();
    if (!q && state.photo) q = PHOTO_Q;               // photo attached + empty box = "what is this?"
    if (!q || state.busy || !state.index) return;
    const t0 = performance.now();
    state.busy = true; state.query = q;
    state.mode = ''; state.hits = []; state.hitIdx = 0; state.usedPhoto = false; state.level = ''; state.confidence = null;
    if (state.photo) return askWithPhoto(q, t0);
    // R: retrieve up to 8 matching PDF chunks - hybrid (embeddings + TF-IDF + rerank) on the Python backend,
    //    or plain TF-IDF in the browser when the backend is not running
    await syncIndex();                                  // pick up uploads / deletions made on the /upload page
    let srv = await serverSearch(state.index, q, 8);
    if (srv && srv.version && srv.version !== state.index.data.version) {   // documents changed mid-question
      await syncIndex();
      srv = await serverSearch(state.index, q, 8);
    }
    const found = srv ? srv.hits : state.index.search(q, 8);   // "unconfident" = no hits: the AI answers from general knowledge
    setConfidence(srv);
    state.searchMode = srv ? srv.mode : 'keyword (browser)';
    state.answer = srv && srv.mode === 'unconfident'
      ? 'Low confidence: no match in your documents. Asking the AI from general knowledge...'
      : state.llm.provider === 'none' ? 'Searching the PDFs...' : `Asking ${state.llm.provider}...`;
    syncDom();
    voice.stop();                                       // barge-in: a new question cuts off the previous spoken answer
    live.reset();
    let lastDraw = 0;
    const res = await answerSmart(q, found, state.llm, (partial) => {   // tokens stream into the panel as they arrive
      state.answer = partial || state.answer;
      if (state.speakAnswers && partial) live.feed(partial);            // ...and complete sentences are spoken at once
      const now = performance.now();
      if (now - lastDraw > 120) { lastDraw = now; syncDom(); }
    });                                                 // G: AI answers from PDFs or general knowledge
    if (state.speakAnswers) live.finish(res.text);      // whatever was not spoken live (no streaming / last sentence)
    showResult(res, found, 'No PDF page - this answer is general knowledge');
    state.busy = false; syncDom();
    // ProcOps: log the question against the page it was answered from (page = procedure step)
    const top = state.hits[0];
    logProcOps(top ? { q, uid: top.doc.uid, page: top.chunk.page, mode: res.mode, ms: Math.round(performance.now() - t0) }
      : { q, mode: res.mode, ms: Math.round(performance.now() - t0) });
    ui.setStatus(state.status + ` · last search: ${state.searchMode}`);
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
        ui.setPhotoText('Photo: ' + (state.photoInfo.object || 'attached').slice(0, 60));
      }
      const info = state.photoInfo;
      const searchQ = [q, info.object, info.text_seen, info.keywords].filter(Boolean).join(' ');
      await syncIndex();
      const srv = await serverSearch(state.index, searchQ, 8);
      const found = srv ? srv.hits : state.index.search(searchQ, 8);
      setConfidence(srv);
      state.searchMode = (srv ? srv.mode : 'keyword (browser)') + ' + photo';
      state.answer = `I see: ${info.object || 'an object'}. Checking your documents...`; syncDom();
      voice.stop(); live.reset();
      const res = await answerVision(q, info, found, photo, state.photoHint, state.llm);
      if (state.speakAnswers) live.finish(res.text);
      showResult(res, found, 'No PDF page - answered from the photo + general knowledge');
      const top = state.hits[0], tag = `[${state.photoHint ? 'vr scan' : 'photo'}: ${(info.object || 'object').slice(0, 60)}] ${q}`;
      logProcOps(top ? { q: tag, uid: top.doc.uid, page: top.chunk.page, mode: res.mode, ms: Math.round(performance.now() - t0) }
        : { q: tag, mode: res.mode, ms: Math.round(performance.now() - t0) });
    } finally {
      state.busy = false; syncDom();
      ui.setStatus(state.status + ` · last search: ${state.searchMode || ''}`);
    }
  }

  function setPhoto(dataUrl, hint = '') {
    state.photoHint = hint;
    state.photo = dataUrl; state.photoInfo = null; state.photoImg = null;
    if (!dataUrl) { ui.showPhoto(null); panels.drawAnswer(); return; }
    const img = new Image();
    img.onload = () => { state.photoImg = img; panels.drawAnswer(); };
    img.src = dataUrl;
    ui.showPhoto(dataUrl, 'Photo attached - type or speak a question, or press Enter');
  }

  // 🔍 Scan VR view: grab the frame the user sees, then ask straight away (info shows on the VR answer panel)
  let scanning = false;
  async function scanView() {
    if (scanning || state.busy || !state.index) return;
    if (!state.vision || !state.vision.available) { state.answer = 'Scan needs a vision model: ' + ((state.vision && state.vision.reason) || 'not ready yet'); syncDom(); return; }
    let shot;
    scanning = true;
    try {
      const cap = await captureVrView(world, [...panels.meshes, ...apps.meshes()], [...ui.hideOnScan, aimRing]);
      shot = cap.image; state.scanHow = cap.how;
    } catch (e) {
      state.answer = (e.name === 'NotAllowedError' ? 'Tab sharing was not allowed, so the room could not be captured. ' : 'Could not capture the VR view: ' + e.message + '. ') +
        'Tip: turn off the Immersive Web Emulator extension, or use 📷 to upload a photo.';
      state.mode = 'offline'; syncDom(); return;
    }
    finally { scanning = false; }
    setPhoto(shot, VR_VIEW_HINT);
    ui.setPhotoText('VR view scanned');
    panels.bringToView();
    ask(VIEW_Q);
  }
  $('rag-scan').onclick = scanView;
  $('rag-speak').onclick = () => {
    state.speakAnswers = !state.speakAnswers;
    ui.setSpeakLabel(state.speakAnswers);
    if (!state.speakAnswers) voice.stop();
  };
  window.addEventListener('keydown', (e) => {
    if ((e.key === 'x' || e.key === 'X') && !e.repeat && !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) scanView();
  });
  const aimRing = addAimRing();
  hideStudioInPassthrough(world);
  $('rag-photo-x').onclick = () => setPhoto(null);

  // ---------- ☰ Apps: mirror real Chrome tabs (WhatsApp, Instagram, Email...) as VR windows ----------
  const apps = setupApps({ world, onChange: (list) => ui.renderWins(list, apps) });
  ui.buildAppButtons(APPS, apps);

  // Reloads index.json when the server's document set changed (upload page), so new files are searchable in VR.
  async function syncIndex() {
    const v = await serverIndexVersion();
    if (v === null || v === state.index.data.version) return;
    state.index = await RagIndex.load();
    panels.clearCache();
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
        ui.setVoiceHint(msg);
        if (kind === 'listening') voice.stop();   // barge-in: talking cuts off the spoken answer
        if (kind === 'listening' || kind === 'transcribing') { state.query = '[mic] ' + msg; panels.drawAnswer(); }
        if (kind === 'error') { state.query = ''; panels.drawAnswer(); }
        if (kind === 'done' && !msg) ui.setVoiceHint('');
      },
      onText: (text) => { ui.setVoiceHint('Heard: "' + text + '"'); ask(text); },
      label: '🎤 Tap to ask',
    });
    state.status += ` · voice: ${v.mode === 'groq' ? 'Whisper' : v.mode === 'browser' ? 'Chrome' : 'off'}`;
    if (v.mode === 'none') { $('rag-mic').disabled = true; ui.setVoiceHint('Voice needs Chrome with a microphone'); }
    syncDom();

    // ---------- camera (scan, then ask) ----------
    setupCamera({ button: $('rag-cam'), onPhoto: (p) => { setPhoto(p); ask(PHOTO_Q); } });
    state.vision = await visionStatus();
    if (!state.vision.available) for (const id of ['rag-cam', 'rag-scan']) { $(id).disabled = true; $(id).title = 'Photo questions unavailable: ' + state.vision.reason; }
    state.status += ` · photo: ${state.vision.available ? 'on' : 'off'}`;
    syncDom();
  })();

  window.__vrRag = { state, ask, setPhoto, world, apps, voice, live }; // handy for debugging in DevTools
}
