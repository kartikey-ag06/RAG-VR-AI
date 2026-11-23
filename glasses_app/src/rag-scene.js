// rag-scene.js - VR_RAG on Meta VR Glasses (IWSDK): the glasses' "operating system".
//
// Ask by voice -> documents (RAG) or the daily assistant -> answer on the 3D panels + the in-lens HUD.
// Feature modules: assistant (reminders, weather, translate...), speech (wake word, captions, read aloud),
// hud (in-lens display), liveai, media (photos, video, filters), codes (QR/barcodes -> machine id),
// workflow (guided steps, pins, look-to-ask, exports), expert (remote expert call), apps (mirror windows).
//
// Glasses input (emulated by IWER): pinch = next page (or next step in a procedure), squeeze = next source.
// Keys: hold V = talk · X = scan view · N / B = next / previous step · Tab = next source · [ ] = page.
import { Mesh, PlaneGeometry, MeshBasicMaterial, CanvasTexture, SRGBColorSpace, DoubleSide, Vector3 } from '@iwsdk/core';
import { RagIndex, pageImageUrl, figureUrl, loadLlmConfig, answerSmart, serverSearch, serverIndexVersion, logProcOps,
  answerVision, describePhoto, visionStatus, VR_VIEW_HINT, setAnswerStyle, api, cleanBody } from './rag.js';
import { setupVoice } from './voice.js';
import { setupCamera } from './camera.js';
import { setupApps, APPS } from './apps.js';
import { captureVrView, addAimRing, hideStudioInPassthrough, viewStream } from './vrscan.js';
import { S, LANGS, langByName, langByCode, onSetting } from './settings.js';
import { setupListener, speak, speakChunk, stopSpeaking, speechSupported, nativeSpeech, diagnoseMic, explainDiagnosis, whisperStats } from './speech.js';
import { setupHud } from './hud.js';
import { setupAssistant } from './assistant.js';
import { setupLiveAI } from './liveai.js';
import { setupMedia, blurFaces, dataUrlToCanvas, FILTERS } from './media.js';
import { readCodes } from './codes.js';
import { setupSession, setupPins, setupWorkflow, setupExport } from './workflow.js';
import { setupExpert } from './expert.js';
import { privacy } from './privacy.js';
import { setupExtras } from './extras.js';

const PHOTO_Q = 'What is this, and what do my documents say about it?';
const VIEW_Q = 'What is the object in the middle of my view? Give key facts about it.';
const EXAMPLES = [
  'What penalty was imposed on Reliance Industries?',
  'Remind me at 5 pm to check the pump',
  'Guide me through starting pump P-101',
  'Turn on captions and translate to Hindi',
];

export function setupRag(world) {
  const state = { photoHint: '', photo: null, photoImg: null, photoInfo: null, usedPhoto: false, vision: null, index: null,
    llm: null, hits: [], hitIdx: 0, doc: -1, page: 1, busy: false, query: '', answer: '', mode: '', status: 'Loading...',
    conf: null, lowConf: false, figure: null, translateTo: '' };
  setAnswerStyle(S.answerLang, S.answerMode);

  // ---------- DOM overlay: control panel (Ask / Tools / Settings) + apps drawer ----------
  const ui = document.createElement('div');
  ui.innerHTML = `
    <style>
      #rag-ui{position:fixed;top:12px;left:12px;width:350px;max-height:calc(100% - 24px);overflow:auto;z-index:2147483000;box-sizing:border-box;
        background:linear-gradient(160deg,rgba(24,33,58,.86),rgba(10,14,28,.9));border:1px solid rgba(140,170,255,.22);border-radius:22px;padding:16px;
        color:#e9eefb;font:13px/1.45 "Segoe UI Variable","Segoe UI",Inter,system-ui,Arial,sans-serif;
        box-shadow:0 18px 50px rgba(0,0,0,.45),inset 0 1px 0 rgba(255,255,255,.07);backdrop-filter:blur(18px) saturate(150%);-webkit-backdrop-filter:blur(18px) saturate(150%)}
      #rag-ui::-webkit-scrollbar{width:6px} #rag-ui::-webkit-scrollbar-thumb{background:#2b3a60;border-radius:3px}
      #rag-ui button,#rag-ui select,#rag-ui input{font:inherit;border-radius:12px;border:1px solid rgba(140,170,255,.22);background:rgba(255,255,255,.05);color:#e3ebff;transition:background .15s,border-color .15s,transform .1s}
      #rag-ui button{cursor:pointer} #rag-ui button:hover:not(:disabled){background:rgba(79,179,255,.14);border-color:rgba(79,179,255,.5)} #rag-ui button:active:not(:disabled){transform:scale(.97)}
      #rag-ui button:disabled{opacity:.4;cursor:not-allowed}
      #rag-ui button.on{background:rgba(52,211,153,.16) !important;border-color:rgba(52,211,153,.6) !important;color:#bff5dd !important}
      .rg-hd{display:flex;align-items:center;gap:8px;margin-bottom:10px}
      .rg-hd b{font-size:16px;font-weight:750;background:linear-gradient(135deg,#4fb3ff,#8b6cff);-webkit-background-clip:text;background-clip:text;color:transparent}
      .rg-hd span{font-size:11px;color:#8f9bb6;flex:1}
      #rag-min{width:26px;height:26px;padding:0;font-size:14px}
      #rag-ui.min #rag-body{display:none} #rag-ui.min .rg-hd{margin:0}
      .rg-tabs{display:grid;grid-template-columns:repeat(3,1fr);gap:4px;margin-bottom:12px;padding:4px;border-radius:14px;background:rgba(0,0,0,.25)}
      .rg-tabs button{padding:7px;font-size:12px;border-color:transparent !important;background:transparent !important}
      .rg-tabs button.sel{background:linear-gradient(135deg,rgba(79,179,255,.28),rgba(139,108,255,.28)) !important;color:#fff !important;box-shadow:inset 0 0 0 1px rgba(140,170,255,.35)}
      .rg-pane{display:none} .rg-pane.sel{display:block}
      #rag-mic{width:100%;padding:17px 10px;font-size:16px !important;font-weight:650;background:linear-gradient(135deg,#3b9dff,#7a5cff) !important;
        border-color:transparent !important;color:#fff !important;border-radius:16px !important;box-shadow:0 8px 26px rgba(80,120,255,.45)}
      #rag-mic.rec{background:linear-gradient(135deg,#ff4d6d,#c9184a) !important;animation:ragpulse 1.1s infinite}
      @keyframes ragpulse{0%{box-shadow:0 0 0 0 rgba(255,77,109,.6)}100%{box-shadow:0 0 0 16px rgba(255,77,109,0)}}
      #rag-voice{font-size:12px;color:#a9b6d3;min-height:17px;margin:6px 2px 0;text-align:center}
      .rg-row{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin-top:9px}
      .rg-row button{padding:8px 3px;font-size:11.5px}
      .rg-h{font-size:11px;color:#8a95ab;margin:10px 0 0;text-transform:uppercase;letter-spacing:.05em}
      #rag-scan{background:rgba(79,179,255,.16) !important;border-color:rgba(79,179,255,.45) !important}
      .rg-chips{display:flex;flex-wrap:wrap;gap:5px;margin-top:8px}
      .rg-chip{font-size:11px;background:rgba(79,179,255,.14);color:#a9dbff;border-radius:999px;padding:2px 9px;display:none;align-items:center;gap:5px;box-shadow:inset 0 0 0 1px rgba(79,179,255,.3)}
      .rg-chip button{border:0 !important;background:none !important;padding:0 !important;color:#9fd0ff !important;font-size:11px}
      #rag-photo{display:none;align-items:center;gap:8px;margin-top:10px;font-size:12px;color:#cfe3ff;background:#141d33;border:1px solid #2f4470;border-radius:10px;padding:5px}
      #rag-photo img{width:54px;height:40px;object-fit:cover;border-radius:6px} #rag-photo span{flex:1} #rag-photo button{width:26px;height:26px;padding:0}
      #rag-ans{white-space:pre-wrap;margin-top:12px;padding:12px;border-radius:14px;background:rgba(0,0,0,.22);border:1px solid rgba(140,170,255,.12);line-height:1.55;font-size:13.5px}
      #rag-src{white-space:pre-wrap;margin-top:8px;color:#ffd98a;font-size:11.5px}
      #rag-cap{font-size:12px;color:#fff;background:#0b101c;border-radius:8px;padding:6px 8px;margin-top:8px;display:none;white-space:pre-wrap}
      .rg-set{display:grid;grid-template-columns:1fr 1.2fr;gap:6px 8px;align-items:center;font-size:12px}
      .rg-set select,.rg-set input{padding:5px 6px;font-size:12px;width:100%;box-sizing:border-box}
      .rg-set input[type=checkbox]{width:auto;justify-self:start}
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
      #rg-modal{position:fixed;inset:0;z-index:2147483003;background:rgba(4,7,14,.55);display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)}
      .rg-mc{width:min(520px,92vw);background:linear-gradient(160deg,rgba(24,33,58,.97),rgba(10,14,28,.97));border:1px solid rgba(140,170,255,.25);border-radius:20px;padding:16px 18px;color:#e9eefb;font:13px "Segoe UI",Arial,sans-serif;box-shadow:0 24px 60px rgba(0,0,0,.55)}
      .rg-mh{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px} .rg-mh b{font-size:15px}
      .rg-mc label{display:block;margin:10px 0 4px;color:#8f9bb6;font-size:12px}
      .rg-mc input,.rg-mc textarea,.rg-mc select{width:100%;box-sizing:border-box;font:inherit;border-radius:10px;border:1px solid rgba(140,170,255,.25);background:rgba(0,0,0,.3);color:#e9eefb;padding:8px 10px}
      .rg-mc textarea{resize:vertical;line-height:1.5}
      .rg-mc button{font:inherit;border-radius:10px;border:1px solid rgba(140,170,255,.3);background:rgba(255,255,255,.06);color:#e3ebff;padding:8px 12px;cursor:pointer;white-space:nowrap}
      .rg-mc button.go{background:linear-gradient(135deg,#3b9dff,#7a5cff);border-color:transparent;color:#fff;font-weight:600}
      .rg-mrow{display:flex;gap:8px;margin-top:8px;align-items:center} .rg-mrow input,.rg-mrow select{flex:1}
    </style>
    <div id="rag-ui">
      <div class="rg-hd"><b>VR_RAG</b><span>Meta VR Glasses · voice assistant</span><button id="rag-min" title="Hide / show">–</button></div>
      <div id="rag-body">
        <div class="rg-tabs"><button data-tab="ask" class="sel">🎤 Ask</button><button data-tab="tools">🛠 Tools</button><button data-tab="set">⚙ Settings</button></div>
        <div class="rg-pane sel" data-pane="ask">
          <button id="rag-mic" title="Tap to talk (tap again to stop), or hold V">🎤 Tap to ask</button>
          <div id="rag-voice">Hold <b>V</b> or tap the mic, then speak</div>
          <div class="rg-row">
            <button id="rag-scan" title="Scan the object in the red ring of your view (or press X)">🔍 Scan</button>
            <button id="rag-cam" title="Use the webcam or upload a photo">📷 Photo</button>
            <button id="rag-apps" title="Open apps (Chrome, WhatsApp, Instagram, Email...) as VR windows">☰ Apps</button>
          </div>
          <div class="rg-chips">
            <span class="rg-chip" id="chip-machine">🏷 <b id="chip-machine-t"></b><button id="chip-machine-x" title="Stop filtering by this machine">✕</button></span>
            <span class="rg-chip" id="chip-wake">👂 Hey Glasses</span><span class="rg-chip" id="chip-cap">💬 Captions</span>
            <span class="rg-chip" id="chip-live">👁 Live AI</span><span class="rg-chip" id="chip-rec" style="background:#4a1220;color:#ffb3c0">● REC</span>
            <span class="rg-chip" id="chip-priv" style="background:#4a1220;color:#ffd0d8" title="Recording light: shows whenever the camera, microphone or screen is captured">● Mic on</span>
            <span class="rg-chip" id="chip-meet">📝 Meeting notes</span><span class="rg-chip" id="chip-gest">✋ Gestures</span>
            <span class="rg-chip" id="chip-call" style="background:#2e1f4f;color:#d9c6ff">🧑‍🔧 Expert call <button id="chip-call-x" title="End call">✕</button></span>
          </div>
          <div id="rag-cap"></div>
          <div id="rag-photo"><img id="rag-photo-img" alt=""><span id="rag-photo-txt">Photo attached</span><button id="rag-photo-x" title="Remove photo">✕</button></div>
          <div id="rag-ans"></div><div id="rag-src"></div>
        </div>
        <div class="rg-pane" data-pane="tools">
          <div class="rg-h">Assistant</div>
          <div class="rg-row">
            <button id="t-cap" title="Live captions of what people say">💬 Captions</button>
            <button id="t-tr" title="Live captions translated into your answer language">🌐 Translate</button>
            <button id="t-live" title="AI looks at your view every few seconds">👁 Live AI</button>
            <button id="t-rem">⏰ Reminders</button><button id="t-cal">📅 Today</button><button id="t-wx">☀ Weather</button>
          </div>
          <div class="rg-h">Camera</div>
          <div class="rg-row">
            <button id="t-photo" title="Save a photo of your view">📸 Photo</button><button id="t-rec" title="Record your view">🎥 Record</button>
            <button id="t-code" title="Read a QR code / barcode in your view -> machine">🏷 QR / code</button>
            <button id="t-filters">🎭 Filters</button><button id="t-gallery">🖼 Gallery</button><button id="t-notes">📝 Notes</button>
          </div>
          <div class="rg-h">Work</div>
          <div class="rg-row">
            <button id="t-wf" title="Step-by-step procedure for the last question">🧭 Guide me</button><button id="t-next" title="Next step (N)">⏭ Next step</button>
            <button id="t-diag" title="Show the diagram / table of this page">📐 Diagram</button>
            <button id="t-pinp" title="Pin the page in 3D space">📌 Pin page</button><button id="t-pina" title="Pin the answer in 3D space">📌 Pin answer</button>
            <button id="t-exp" title="Inspection report / incident log / work order">📄 Report</button>
          </div>
          <div class="rg-row"><button id="t-expert" style="grid-column:span 3">🧑‍🔧 Call remote expert</button></div>
          <div class="rg-h">Talk, meet, move</div>
          <div class="rg-row">
            <button id="t-tp" title="Your script on the lens - follows your voice">🎤 Teleprompter</button>
            <button id="t-meet" title="Transcribe a meeting, then AI summary + action items">📝 Meeting notes</button>
            <button id="t-gest" title="Webcam hand gestures: pinch = talk, swipe = next/back, palm = pause, 👍 = scan">✋ Gestures</button>
            <button id="t-nav" title="Turn-by-turn walking / cycling / driving directions">🧭 Directions</button>
            <button id="t-calx" title="Show your Google / Outlook calendar on the glasses">📅 Link calendar</button>
            <button id="t-mtl" title="Meeting notes, scripts and more on the Workspace page">🗂 Workspace</button>
          </div>
        </div>
        <div class="rg-pane" data-pane="set">
          <div class="rg-set">
            <label for="s-vl">I speak</label><select id="s-vl">${LANGS.map((l) => `<option value="${l.code}">${l.name} (${l.code})</option>`).join('')}</select>
            <label for="s-al">Answers in</label><select id="s-al">${[...new Set(LANGS.map((l) => l.name.replace(' (US)', '')))].map((n) => `<option>${n}</option>`).join('')}</select>
            <label for="s-am">Answer style</label><select id="s-am"><option value="full">Full answer</option><option value="glance">Glance card (3 points + diagram)</option><option value="spoken">Spoken (1 sentence, read aloud)</option></select>
            <label for="s-sp">Read answers aloud</label><input type="checkbox" id="s-sp">
            <label for="s-wk">"Hey Glasses" wake word</label><input type="checkbox" id="s-wk">
            <label for="s-we">Wake word engine</label><select id="s-we"><option value="auto">Auto (Chrome, else Whisper)</option><option value="whisper">Whisper (same as push-to-talk)</option></select>
            <label for="s-la">Look-to-ask (2 s gaze)</label><input type="checkbox" id="s-la">
            <label for="s-bf">Blur faces before AI</label><input type="checkbox" id="s-bf">
            <label for="s-li">Live AI every (s)</label><input type="number" id="s-li" min="8" max="120">
            <label for="s-gs">Hand gestures (webcam)</label><input type="checkbox" id="s-gs">
            <label for="s-tf">Teleprompter follows voice</label><input type="checkbox" id="s-tf">
            <label for="s-tw">Teleprompter words/min</label><input type="number" id="s-tw" min="80" max="220" step="10">
            <label for="s-tk">Access token</label><input type="password" id="s-tk" placeholder="only if your admin gave you one">
          </div>
          <div class="rg-note" id="s-note">Settings are saved in this browser.</div>
          <div class="rg-note" id="s-heard">Turn on the wake word, then say “Hey Glasses, what is the penalty?”</div>
          <button id="s-test" style="width:100%;margin-top:8px;padding:8px">🎙 Test wake word (8 s)</button>
        </div>
        <div class="rg-ft"><span id="rag-status">Loading...</span><a href="/upload" target="_blank">＋ Upload</a><a href="/workspace" target="_blank">Workspace</a>
          <a href="/procops" target="_blank">ProcOps</a><a href="/expert" target="_blank">Expert</a><a href="/admin" target="_blank">Admin</a>
          <span>V talk · X scan · N/B step · Tab source · [ ] page</span></div>
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
  ui.querySelectorAll('.rg-tabs button').forEach((b) => { b.onclick = () => {
    ui.querySelectorAll('.rg-tabs button').forEach((x) => x.classList.toggle('sel', x === b));
    ui.querySelectorAll('.rg-pane').forEach((p) => p.classList.toggle('sel', p.dataset.pane === b.dataset.tab));
  }; });
  ui.querySelectorAll('#rag-ui input, #rag-ui select').forEach((el) => el.addEventListener('keydown', (e) => e.stopPropagation(), true));

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
  const hud = setupHud(world);

  function wrap(ctx, text, x, y, maxW, lh, maxY, measureOnly = false) {
    const put = measureOnly ? () => {} : (t, yy) => ctx.fillText(t, x, yy);
    for (const para of String(text).split('\n')) {
      let line = '';
      for (const word of para.split(' ')) {
        const t = line ? line + ' ' + word : word;
        if (ctx.measureText(t).width > maxW && line) {
          put(line, y); y += lh; line = word;
          if (y > maxY) { put('…', y); return y; }
        } else line = t;
      }
      put(line, y); y += lh;
      if (y > maxY) return y;
    }
    return y;
  }

  const BADGES = { pdf: ['FROM YOUR DOCUMENTS', '#34d399'], general: ['GENERAL AI · not from the documents', '#fbbf24'],
    offline: ['OFFLINE · best matching sentences', '#94a3b8'], assistant: ['ASSISTANT', '#a78bfa'], look: ['LOOK-TO-ASK', '#4fb3ff'] };
  function rr(ctx, x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }
  /** Frosted-glass window like the Quest / Vision Pro system panels: gradient glass, glowing edge, header bar. */
  function glassBg(ctx, W, H, title, accent = '#4fb3ff') {
    ctx.clearRect(0, 0, W, H);
    rr(ctx, 8, 8, W - 16, H - 16, 44);
    const g = ctx.createLinearGradient(0, 0, W, H);
    g.addColorStop(0, 'rgba(26,36,62,0.93)'); g.addColorStop(0.55, 'rgba(14,20,38,0.93)'); g.addColorStop(1, 'rgba(22,18,48,0.93)');
    ctx.fillStyle = g; ctx.fill();
    const e = ctx.createLinearGradient(0, 0, W, 0); e.addColorStop(0, accent); e.addColorStop(1, '#8b6cff');
    ctx.strokeStyle = e; ctx.globalAlpha = 0.75; ctx.lineWidth = 5; ctx.stroke(); ctx.globalAlpha = 1;
    // soft light on the top edge
    const sh = ctx.createLinearGradient(0, 8, 0, 120); sh.addColorStop(0, 'rgba(255,255,255,0.10)'); sh.addColorStop(1, 'rgba(255,255,255,0)');
    rr(ctx, 8, 8, W - 16, 112, 44); ctx.fillStyle = sh; ctx.fill();
    // header: glasses logo + title + live dot
    ctx.strokeStyle = accent; ctx.lineWidth = 6;
    rr(ctx, 44, 42, 46, 34, 13); ctx.stroke(); rr(ctx, 104, 42, 46, 34, 13); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(90, 56); ctx.lineTo(104, 56); ctx.stroke();
    ctx.textBaseline = 'top'; ctx.fillStyle = '#f2f6ff'; ctx.font = '700 40px Segoe UI, Arial'; ctx.fillText(title, 172, 36);
    ctx.fillStyle = 'rgba(140,170,255,0.18)'; ctx.fillRect(44, 104, W - 88, 2);
  }
  function chip(ctx, x, y, text, color, font = '700 22px Segoe UI, Arial') {
    ctx.font = font; const w = ctx.measureText(text).width + 34;
    rr(ctx, x, y, w, 40, 20); ctx.fillStyle = color + '26'; ctx.fill(); ctx.strokeStyle = color + '99'; ctx.lineWidth = 2; ctx.stroke();
    ctx.fillStyle = color; ctx.fillText(text, x + 17, y + 9);
    return w;
  }
  function drawAnswer() {
    const { ctx, canvas, tex } = ansP, W = canvas.width;
    glassBg(ctx, W, canvas.height, 'AI Assistant');
    const live = state.busy ? '#fbbf24' : '#34d399';
    ctx.fillStyle = live; ctx.beginPath(); ctx.arc(W - 70, 58, 11, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = '#9fb0d0'; ctx.font = '600 22px Segoe UI, Arial'; ctx.textAlign = 'right';
    ctx.fillText(state.busy ? 'thinking…' : 'ready', W - 92, 46); ctx.textAlign = 'left';
    let top = 128;
    if (S.machine) { chip(ctx, 44, top, '🏷 ' + S.machine, '#4fb3ff'); top += 54; }
    const thumb = state.photoImg;
    if (thumb) {
      const sc = Math.min(230 / thumb.width, 172 / thumb.height), w = thumb.width * sc, h = thumb.height * sc;
      ctx.save(); rr(ctx, 980 - w, top, w, h, 18); ctx.clip(); ctx.drawImage(thumb, 980 - w, top, w, h); ctx.restore();
      rr(ctx, 980 - w, top, w, h, 18); ctx.strokeStyle = '#4fb3ff'; ctx.lineWidth = 3; ctx.stroke();
    }
    // the question, in a muted "bubble"
    ctx.font = '600 30px Segoe UI, Arial';
    const qText = state.query ? state.query : 'Ask anything about your documents — or say "Hey Glasses"';
    const qW = thumb ? 660 : 900;
    const qEnd = wrap(ctx, qText, 70, top + 20, qW, 38, top + 20 + 38 * 3, true);
    rr(ctx, 44, top, qW + 52, Math.max(78, qEnd - top + 18), 24); ctx.fillStyle = 'rgba(255,255,255,0.06)'; ctx.fill();
    ctx.fillStyle = state.query ? '#c9d6f2' : '#7f8db0'; wrap(ctx, qText, 70, top + 20, qW, 38, top + 20 + 38 * 3);
    let ay = Math.max(qEnd + 34, thumb ? top + 190 : 0);
    const badge = BADGES[state.mode];
    if (badge) {
      const w = chip(ctx, 44, ay, (state.usedPhoto ? '📷 PHOTO + ' : '') + badge[0], badge[1]);
      if (state.mode === 'pdf' && state.lowConf) chip(ctx, 56 + w, ay, '⚠ LOW CONFIDENCE · say "call expert"', '#fb923c');
      ay += 62;
    }
    ctx.fillStyle = '#f3f6fc'; ctx.font = (S.answerMode === 'glance' ? '700 36px' : '31px') + ' Segoe UI, Arial';
    wrap(ctx, state.answer || '', 50, ay, 924, S.answerMode === 'glance' ? 46 : 41, 990);
    // sources as rounded rows
    let sy = 1004;
    if (state.hits.length) {
      ctx.fillStyle = '#8f9bb6'; ctx.font = '700 20px Segoe UI, Arial'; ctx.fillText('SOURCES  ·  squeeze / Tab = next', 50, sy); sy += 32;
      state.hits.slice(0, 4).forEach((h, i) => {
        const on = i === state.hitIdx;
        rr(ctx, 44, sy, W - 88, 40, 14); ctx.fillStyle = on ? 'rgba(79,179,255,0.18)' : 'rgba(255,255,255,0.04)'; ctx.fill();
        ctx.fillStyle = on ? '#cfe9ff' : '#aab6cf'; ctx.font = (on ? '600 ' : '') + '23px Segoe UI, Arial';
        const t = `[${i + 1}]  ${h.doc.title}  ·  p.${h.chunk.page}`;
        ctx.fillText(t.length > 66 ? t.slice(0, 66) + '…' : t, 62, sy + 8); sy += 46;
      });
    }
    ctx.fillStyle = '#65708a'; ctx.font = '20px Segoe UI, Arial'; ctx.fillText(state.status.slice(0, 92), 50, 1228);
    tex.needsUpdate = true;
  }

  function drawPdf(img, caption) {
    const { ctx, canvas, tex } = pdfP;
    const W = canvas.width;
    ctx.clearRect(0, 0, W, canvas.height);
    rr(ctx, 8, 8, W - 16, canvas.height - 16, 40);
    const g = ctx.createLinearGradient(0, 0, 0, canvas.height); g.addColorStop(0, 'rgba(24,32,56,0.95)'); g.addColorStop(1, 'rgba(12,16,30,0.95)');
    ctx.fillStyle = g; ctx.fill(); ctx.strokeStyle = 'rgba(120,170,255,0.55)'; ctx.lineWidth = 4; ctx.stroke();
    if (img) {
      const sc = Math.min(964 / img.width, 1270 / img.height), w = img.width * sc, h = img.height * sc, x = (W - w) / 2, y = state.figure ? (1320 - h) / 2 : 28;
      ctx.save(); ctx.shadowColor = 'rgba(0,0,0,0.6)'; ctx.shadowBlur = 24; ctx.shadowOffsetY = 8;
      rr(ctx, x, y, w, h, 14); ctx.fillStyle = '#fff'; ctx.fill(); ctx.restore();
      ctx.save(); rr(ctx, x, y, w, h, 14); ctx.clip(); ctx.drawImage(img, x, y, w, h); ctx.restore();
    }
    ctx.textBaseline = 'top';
    rr(ctx, 26, 1326, W - 52, 50, 25); ctx.fillStyle = 'rgba(79,179,255,0.14)'; ctx.fill();
    ctx.fillStyle = '#d6e8ff'; ctx.font = '600 24px Segoe UI, Arial';
    ctx.fillText('📄 ' + caption.slice(0, 74), 46, 1338);
    tex.needsUpdate = true;
  }

  const cache = new Map();
  function loadImg(url, done, fail) {
    if (cache.has(url)) return done(cache.get(url));
    const img = new Image(); img.crossOrigin = 'anonymous';
    img.onload = () => { cache.set(url, img); done(img); };
    img.onerror = fail; img.src = url;
  }
  function showPage(docId, page) {
    if (!state.index || docId < 0) return;
    const doc = state.index.data.docs[docId];
    page = Math.min(Math.max(page, 1), doc.pages);
    state.doc = docId; state.page = page; state.figure = null;
    const url = pageImageUrl(docId, page);
    const ver = doc.rev && doc.rev !== '1.0' ? ` v${doc.rev}` : '';
    const figs = (doc.figs || {})[String(page)] || [];
    const cap = `${doc.title}${ver}  |  page ${page}/${doc.pages}${figs.length ? '  · 📐 diagram' : ''}`;
    loadImg(url, (img) => { if (state.doc === docId && state.page === page && !state.figure) drawPdf(img, cap); },
      () => drawPdf(null, 'Missing image ' + url));
  }
  /** Glance mode / "show diagram": the cut-out diagram or table instead of the whole A4 page. */
  function showFigure(docId, page) {
    const doc = state.index && state.index.data.docs[docId];
    const figs = doc && ((doc.figs || {})[String(page)] || []);
    if (!figs || !figs.length) return false;
    const f = figs.slice().sort((a, b) => b.w * b.h - a.w * a.h)[0];
    state.doc = docId; state.page = page; state.figure = f;
    loadImg(figureUrl(docId, f.file), (img) => { if (state.figure === f) drawPdf(img, `📐 ${f.kind}: ${doc.title} p.${page}${f.caption ? ' · ' + f.caption.slice(0, 30) : ''}`); },
      () => showPage(docId, page));
    return true;
  }

  function syncDom() {
    $('rag-status').textContent = state.status;
    const label = { pdf: '📄 From your documents', general: '🌐 General AI answer (not from the documents)', offline: '⚙ Offline (best matching sentences)',
      assistant: '🤖 Assistant', look: '👁 Look-to-ask' }[state.mode] || '';
    $('rag-ans').textContent = (label ? (state.usedPhoto ? '📷 Photo + ' : '') + label + (state.mode === 'pdf' && state.lowConf ? '  ·  ⚠ low confidence' : '') + '\n\n' : '') + state.answer;
    $('rag-src').textContent = state.hits.length
      ? 'Sources:\n' + state.hits.map((h, i) => `${i === state.hitIdx ? '▶' : '  '} [${i + 1}] ${h.doc.title} - p.${h.chunk.page}`).join('\n') : '';
    drawAnswer();
  }
  const ttsLang = () => (langByName(S.answerLang) || langByCode(S.voiceLang)).code;
  function say(text, lang) { if (S.speak || S.answerMode === 'spoken') speak(text, lang || ttsLang()); }
  const speaking = () => S.speak || S.answerMode === 'spoken';

  /** Shows a streamed answer as it is written (redraw ~8x per second) and starts reading it aloud at the
   *  first full sentence. finish(text) reads whatever was not spoken yet. */
  function streamer(t0 = performance.now()) {
    let lastDraw = 0, spoken = 0, first = 0, lang = ttsLang();
    return {
      onDelta(acc) {
        const body = cleanBody(acc);
        if (!body) return;
        if (!first) { first = performance.now(); state.firstWordMs = Math.round(first - t0); stopSpeaking(); }
        state.answer = body + ' ▍';
        const now = performance.now();
        if (now - lastDraw > 120) { lastDraw = now; syncDom(); }
        if (speaking()) {
          const rest = body.slice(spoken), m = rest.match(/^[\s\S]*?[.!?।](\s|$)/);
          if (m && m[0].trim().length > 12) { speakChunk(m[0], lang); spoken += m[0].length; }
        }
      },
      finish(text) {
        if (!spoken) return say(text);
        if (speaking() && text.length > spoken) speakChunk(text.slice(spoken), lang);
      },
    };
  }

  // ---------- feature modules ----------
  const session = setupSession();
  const aimRing = addAimRing();
  hideStudioInPassthrough(world);
  const apps = setupApps({ world, onChange: renderWins });
  const ownMeshes = () => [ansP.mesh, pdfP.mesh, hud.mesh, ...apps.meshes(), ...(pins ? pins.meshes() : [])];
  const ownEls = () => [$('rag-ui'), $('rag-drawer'), aimRing];
  const capture = async (ring = true) => (await captureVrView(world, ownMeshes(), ownEls(), ring)).image;
  const media = setupMedia({ onAsk: (c) => askAboutCanvas(c), toast: hud.toast, captureView: capture, viewStream: (fps) => viewStream(world, fps) });
  const pins = setupPins({ world, hud, getTargets: () => [pdfP.mesh], enabled: () => S.lookToAsk && !state.busy, onDwell });
  const workflow = setupWorkflow({ hud, session, say, getMachine: () => S.machine,
    show: (wf, s) => { if (s.cite) showPage(s.cite.doc, s.cite.page); state.usedPhoto = false; state.query = `${wf.title} — step`; state.answer = `Step: ${s.title}\n${s.detail}${s.warning ? '\n⚠ ' + s.warning : ''}`; state.mode = 'assistant'; state.hits = []; syncDom(); } });
  const exporter = setupExport({ session, hud });
  const expert = setupExpert({ hud, viewStream: (fps) => viewStream(world, fps), say, session });
  const live = setupLiveAI({ hud, capture: () => capture(true), onCode: (t) => resolveMachine(t, false), intervalSec: () => Math.max(8, +S.liveInterval || 20) });
  const assistant = setupAssistant({ hud, say, act, reply });
  const listener = setupListener({
    onCommand: (text) => { hud.toast('👂 ' + text, 'info', 2500); ask(text); },
    onCaption,
    onState: (k, m) => {
      if (k === 'wake') hud.toast('👂 ' + m, 'info', 3000);
      if (k === 'error') hud.toast(m, 'bad', 7000);
      if (k === 'info') hud.toast('👂 ' + m, 'warn', 6000);
      if (k === 'on' || k === 'info') setTimeout(() => { refreshChips(); showEngine(); }, 300);
    },
    // what the microphone last understood - shown in Settings, so you can see why "Hey Glasses" did not match
    onHeard: (text) => { const el = document.getElementById('s-heard'); if (el) el.textContent = `Last heard: “${text.slice(0, 80)}”`; },
  }, S.wakeEngine);
  function showEngine() {
    const el = document.getElementById('s-note');
    if (!el) return;
    el.textContent = listener.engine === 'chrome'
      ? 'Wake word: Chrome\'s built-in speech engine - no calls to our server or Groq while it waits for "Hey Glasses".'
      : `⚠ Whisper mode: each sentence spoken near the mic is sent to Groq to look for "Hey Glasses" (max 8 a minute, never while the app talks). ${nativeSpeech() ? 'Choose Auto to avoid these calls.' : ''} Calls so far: ${whisperStats.calls}.`;
  }
  setInterval(() => { if (listener.engine === 'whisper' && listener.active && !document.hidden) showEngine(); }, 5000);

  const extras = setupExtras({ hud, say, reply, act, listener, session, privacy, micToggle: () => $('rag-mic').click() });
  privacy.on((p) => {                                  // the recording light, on the lens and on screen
    hud.setRec(p);
    const any = p.camera || p.mic || p.screen || p.recording;
    $('chip-priv').style.display = any ? 'inline-flex' : 'none';
    $('chip-priv').textContent = p.recording ? '● REC' : p.camera ? '● Camera on' : p.screen ? '● Screen shared' : '● Mic on';
    $('chip-priv').style.background = p.camera || p.screen || p.recording ? '#4a1220' : '#4a3610';
  });

  function reply(display, spoken, lang) {
    state.answer = display; state.mode = 'assistant'; state.hits = []; state.lowConf = false; state.usedPhoto = false; syncDom();
    hud.toast(display.split('\n')[0], 'info', 6000);
    say(spoken || display, lang);
    session.log('assistant', { text: display.slice(0, 600) });
  }

  // ---------- live captions (+ translation) ----------
  let lastTr = '';
  async function onCaption(text, final) {
    $('rag-cap').style.display = 'block';
    if (!final) { hud.setCaption(text, state.translateTo ? lastTr : '', false); $('rag-cap').textContent = text; return; }
    let tr = '';
    if (state.translateTo) { try { tr = (await api('/api/translate', { text, target: state.translateTo })).text; } catch { tr = '(translation failed)'; } lastTr = tr; }
    hud.setCaption(text, tr, true);
    $('rag-cap').textContent = text + (tr ? `\n→ ${state.translateTo}: ${tr}` : '');
    session.log('caption', { text: text + (tr ? ` → ${tr}` : '') });
  }
  function captions(on, translateTo) {
    if (!speechSupported()) { hud.toast('Captions need a microphone', 'bad'); return; }
    state.translateTo = on ? (translateTo || '') : '';
    listener.setLang(S.voiceLang); listener.setCaptions(on);
    if (!on) { hud.setCaption(null); $('rag-cap').style.display = 'none'; }
    hud.toast(on ? `💬 Captions on (${langByCode(S.voiceLang).name})${state.translateTo ? ' → ' + state.translateTo : ''}` : '💬 Captions off', 'info');
    refreshChips();
  }

  // ---------- look-to-ask ----------
  async function onDwell(mesh) {
    let docId = -1, page = 0;
    if (mesh === pdfP.mesh && state.doc >= 0) { docId = state.doc; page = state.page; }
    else if (mesh.userData.pin && mesh.userData.pin.uid) {
      const d = state.index.data.docs.find((x) => x.uid === mesh.userData.pin.uid);
      if (d) { docId = d.id; page = mesh.userData.pin.page; }
    }
    if (docId < 0 || state.busy) return;
    const hits = state.index.data.chunks.map((c) => ({ chunk: c, doc: state.index.data.docs[c.doc] })).filter((h) => h.chunk.doc === docId && h.chunk.page === page).slice(0, 6);
    if (!hits.length) return;
    hud.toast('👁 You are looking at this page - explaining it', 'info', 3500);
    state.busy = true; state.query = `(looking at) ${hits[0].doc.title} p.${page}`; state.answer = 'Reading the page...'; syncDom();
    const st = streamer();
    const res = await answerSmart('Explain what this page says, briefly and simply. Point out any steps, limits or warnings.', hits, state.llm, st.onDelta);
    state.answer = res.text; state.mode = 'look'; state.hits = res.cited.map((i) => hits[i]); state.busy = false; syncDom();
    st.finish(res.text);
    session.log('look', { text: `Looked at ${hits[0].doc.title} p.${page}`, answer: res.text.slice(0, 400) });
  }

  // ---------- machines (QR / barcode / serial) ----------
  async function resolveMachine(text, announce = true) {
    let r;
    try { r = await api('/api/machines/resolve', { text }); } catch { return null; }
    if (!r.machine_id) { if (announce) hud.toast(`🏷 Read "${text.slice(0, 40)}" - no machine with this id in your documents`, 'warn', 6000); return null; }
    if (S.machine !== r.machine_id) {
      S.machine = r.machine_id;
      hud.toast(`🏷 Machine ${r.machine_id}: ${r.docs.length} document${r.docs.length === 1 ? '' : 's'} - answers now use these first`, 'ok', 7000);
      say(`Machine ${r.machine_id} found.`);
      session.log('machine', { machine_id: r.machine_id, text: `Machine identified: ${r.machine_id} (from "${text.slice(0, 60)}")` });
      refreshChips(); drawAnswer();
    }
    return r;
  }
  async function scanCode() {
    hud.toast('🏷 Looking for a QR code / barcode...', 'info', 2500);
    const c = await dataUrlToCanvas(await capture(false));
    const codes = await readCodes(c);
    if (!codes.length) { hud.toast('No QR code / barcode in view. Hold it closer, or use 📷 Photo with the webcam.', 'warn', 7000); return; }
    session.log('code', { text: `${codes[0].format}: ${codes[0].text}` });
    const r = await resolveMachine(codes[0].text);
    if (r) ask(`What is ${r.machine_id} and what are its main troubleshooting steps?`, { raw: true });
  }

  // ---------- answering ----------
  async function ask(q, opts = {}) {
    q = (q || '').trim();
    if (!q && state.photo) q = PHOTO_Q;
    if (!q || state.busy || !state.index) return;
    stopSpeaking();
    if (!opts.raw) {
      try { if ((await extras.route(q)) || (await assistant.route(q))) { state.query = q; drawAnswer(); return; } }
      catch (e) { hud.toast('Command failed: ' + e.message, 'bad', 6000); }
    }
    const t0 = performance.now();
    state.busy = true; state.query = q;
    state.mode = ''; state.hits = []; state.hitIdx = 0; state.usedPhoto = false; state.lowConf = false;
    session.log('question', { text: q, machine: S.machine || undefined });
    if (state.photo) return askWithPhoto(q, t0);
    const fresh = /\b(this|that|it|here|in front)\b/i.test(q) && live.recent();        // Live AI saw something
    const searchQ = fresh ? `${q} ${fresh.object} ${fresh.text_seen || ''}` : q;
    await syncIndex();
    let srv = await serverSearch(state.index, searchQ, 8, S.machine);
    let found = srv ? srv.hits : state.index.search(searchQ, 8);
    if (srv && srv.version && srv.version !== state.index.data.version) {
      await syncIndex();
      srv = await serverSearch(state.index, searchQ, 8, S.machine);
      found = srv ? srv.hits : state.index.search(searchQ, 8);
    }
    if (srv && S.machine && srv.machineMatched === false) hud.toast(`No documents tagged ${S.machine} - searched all documents`, 'warn', 5000);
    state.searchMode = srv ? srv.mode + (srv.engine === 'pgvector' ? ' (pgvector)' : '') : 'keyword (browser)';
    state.conf = srv ? srv.conf : null;
    state.answer = state.llm.provider === 'none' ? 'Searching the documents...' : `Asking ${state.llm.provider}...`;
    syncDom();
    const st = streamer(t0);
    const res = fresh ? await answerVision(q, fresh, found, null, VR_VIEW_HINT, state.llm, st.onDelta) : await answerSmart(q, found, state.llm, st.onDelta);
    finishAnswer(q, res, found, srv, t0, fresh ? `[live ai: ${fresh.object.slice(0, 50)}] ` : '', st);
  }

  function finishAnswer(q, res, found, srv, t0, tag = '', st = null) {
    state.answer = res.text; state.mode = res.mode;
    state.hits = res.cited.map((i) => found[i]);
    state.lowConf = !!(srv && srv.lowConfidence && res.mode === 'pdf');
    const top = state.hits[0];
    if (top) { if (!(S.answerMode === 'glance' && showFigure(top.doc.id, top.chunk.page))) showPage(top.doc.id, top.chunk.page); }
    else { state.doc = -1; drawPdf(null, res.mode === 'general' ? 'No document page - this answer is general knowledge' : 'No matching page'); }
    state.busy = false; syncDom();
    hud.toast((res.mode === 'pdf' ? '📄 ' : '🌐 ') + res.text.replace(/\s+/g, ' ').slice(0, 70), res.mode === 'pdf' ? 'ok' : 'info', 6000);
    if (state.lowConf) hud.toast('⚠ Low confidence: the documents only partly match. Say "call expert" to escalate.', 'warn', 8000);
    if (st) st.finish(res.text); else say(res.text);
    state.lastMs = Math.round(performance.now() - t0);
    const sources = state.hits.map((h) => `${h.doc.title} - p.${h.chunk.page}`);
    session.log('answer', { text: res.text.slice(0, 1200), mode: res.mode, sources, conf: state.conf });
    if (expert.active) expert.context({ question: q, answer: res.text, sources: sources.join('; '), machine: S.machine });
    logProcOps(top ? { q: tag + q, uid: top.doc.uid, page: top.chunk.page, mode: res.mode, ms: Math.round(performance.now() - t0), conf: state.conf, hits: found.length, machine: S.machine }
      : { q: tag + q, mode: res.mode, ms: Math.round(performance.now() - t0), conf: state.conf, hits: found.length, machine: S.machine });
    $('rag-status').textContent = state.status + ` · ${state.searchMode || ''} · first words ${state.firstWordMs ?? '-'} ms · total ${(state.lastMs / 1000).toFixed(1)} s`;
  }

  // Scan, then ask: the vision model describes the photo once, documents are searched with those words,
  // then the chat model answers (photo description + excerpts).
  async function askWithPhoto(q, t0) {
    const photo = state.photo;
    state.usedPhoto = true;
    try {
      if (!state.photoInfo) {
        state.answer = 'Looking at your photo...'; syncDom();
        try { state.photoInfo = await describePhoto(photo, state.photoHint); }
        catch (e) { state.answer = 'Could not read the photo: ' + e.message; state.mode = 'offline'; return; }
        $('rag-photo-txt').textContent = 'Photo: ' + (state.photoInfo.object || 'attached').slice(0, 60);
        session.log('scan', { text: `Saw: ${state.photoInfo.object}${state.photoInfo.text_seen ? ' · text: ' + state.photoInfo.text_seen : ''}` });
        if (state.photoInfo.text_seen) resolveMachine(state.photoInfo.text_seen, false);     // a serial / model number on the label?
      }
      const info = state.photoInfo;
      const searchQ = [q, info.object, info.text_seen, info.keywords].filter(Boolean).join(' ');
      await syncIndex();
      const srv = await serverSearch(state.index, searchQ, 8, S.machine);
      const found = srv ? srv.hits : state.index.search(searchQ, 8);
      state.searchMode = (srv ? srv.mode : 'keyword (browser)') + ' + photo';
      state.conf = srv ? srv.conf : null;
      state.answer = `I see: ${info.object || 'an object'}. Checking your documents...`; syncDom();
      const st = streamer(t0);
      const res = await answerVision(q, info, found, photo, state.photoHint, state.llm, st.onDelta);
      state.busy = false;
      finishAnswer(q, res, found, srv, t0, `[${state.photoHint ? 'vr scan' : 'photo'}: ${(info.object || 'object').slice(0, 60)}] `, st);
      state.usedPhoto = true; syncDom();
    } finally {
      state.busy = false; syncDom();
    }
  }

  /** The vision check at start-up can time out while the server is still busy: check again when it is needed. */
  async function ensureVision() {
    if (state.vision && state.vision.available) return true;
    state.vision = await visionStatus();
    if (state.vision.available) {
      for (const id of ['rag-cam', 'rag-scan', 't-live']) { const b = $(id); if (b) { b.disabled = false; b.title = ''; } }
      state.status = state.status.replace('photo: off', 'photo: on');
    }
    return state.vision.available;
  }
  let visionTries = 0;                                  // re-check a few times only (a key without a vision model never gets one)
  const visionTimer = setInterval(() => {
    if (state.vision && state.vision.available) { clearInterval(visionTimer); return; }
    if (document.hidden) return;
    if (++visionTries > 5) { clearInterval(visionTimer); return; }
    ensureVision();
  }, 20000);

  function setPhoto(dataUrl, hint = '') {
    state.photoHint = hint;
    state.photo = dataUrl; state.photoInfo = null; state.photoImg = null;
    if (!dataUrl) { $('rag-photo').style.display = 'none'; drawAnswer(); return; }
    const img = new Image();
    img.onload = () => { state.photoImg = img; drawAnswer(); };
    img.src = dataUrl;
    $('rag-photo-img').src = dataUrl; $('rag-photo-txt').textContent = 'Photo attached - ask about it';
    $('rag-photo').style.display = 'flex';
  }
  /** Any photo (webcam, gallery, filter camera): read codes, blur faces, then ask about it. */
  async function askAboutCanvas(c) {
    const codes = await readCodes(c).catch(() => []);
    if (codes.length) { session.log('code', { text: codes[0].text }); await resolveMachine(codes[0].text); }
    if (S.blurFaces) { const n = await blurFaces(c); if (n > 0) hud.toast(`🙈 Blurred ${n} face${n > 1 ? 's' : ''} before sending to the AI`, 'info'); }
    setPhoto(c.toDataURL('image/jpeg', 0.85)); ask(PHOTO_Q, { raw: true });
  }

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

  let scanning = false;
  async function scanView() {
    if (scanning || state.busy || !state.index) return;
    if (!(await ensureVision())) { state.answer = 'Scan needs a vision model: ' + ((state.vision && state.vision.reason) || 'not ready yet'); syncDom(); return; }
    let shot;
    scanning = true;
    try {
      privacy.set('scan', 'camera');
      const cap = await captureVrView(world, ownMeshes(), ownEls()).finally(() => setTimeout(() => privacy.set('scan', null), 600));
      shot = cap.image; state.scanHow = cap.how;
      const c = await dataUrlToCanvas(shot);
      const codes = await readCodes(c).catch(() => []);
      if (codes.length) await resolveMachine(codes[0].text);
      if (S.blurFaces && (await blurFaces(c)) > 0) shot = c.toDataURL('image/jpeg', 0.85);
    } catch (e) {
      state.answer = (e.name === 'NotAllowedError' ? 'Tab sharing was not allowed, so the room could not be captured. ' : 'Could not capture the VR view: ' + e.message + '. ') +
        'Tip: turn off the Immersive Web Emulator extension, or use 📷 to upload a photo.';
      state.mode = 'offline'; syncDom(); return;
    } finally { scanning = false; }
    setPhoto(shot, VR_VIEW_HINT);
    $('rag-photo-txt').textContent = 'VR view scanned';
    bringPanelsToView();
    ask(VIEW_Q, { raw: true });
  }

  // ---------- every action (buttons, voice commands, keys) goes through here ----------
  async function act(cmd, a = {}) {
    try {
      switch (cmd) {
        case 'next': if (extras.teleprompter.on) extras.teleprompter.next(); else if (!workflow.next() && !extras.nav.next()) showPage(state.doc, state.page + 1); break;
        case 'prev': if (extras.teleprompter.on) extras.teleprompter.prev(); else if (!workflow.prev() && !extras.nav.prev()) showPage(state.doc, state.page - 1); break;
        case 'repeat': if (!workflow.repeat() && !extras.nav.repeat()) say(state.answer); break;
        case 'pause': if (extras.teleprompter.on) extras.teleprompter.pause(); else stopSpeaking(); break;
        case 'teleprompter': openPrompterDialog(); break;
        case 'meeting': await (extras.meeting.on ? extras.meeting.stop() : extras.meeting.start()); refreshChips(); break;
        case 'gestures': await (extras.gestures.on ? extras.gestures.stop() : extras.gestures.start()); refreshChips(); break;
        case 'directions': openDirectionsDialog(); break;
        case 'wfstop': workflow.stop(); hud.toast('Procedure stopped', 'info'); break;
        case 'wfstart': await workflow.start(a.topic); break;
        case 'scan': await scanView(); break;
        case 'code': await scanCode(); break;
        case 'photo': { const { canvas } = await media.capturePhoto(); session.log('photo', { text: 'Photo of the view saved' }); if (a.ask) askAboutCanvas(canvas); break; }
        case 'videoOn': if (!media.recording) media.toggleViewRecording(); refreshChips(); break;
        case 'videoOff': if (media.recording) media.toggleViewRecording(); setTimeout(refreshChips, 500); break;
        case 'gallery': media.openGallery(); break;
        case 'filters': media.openCamera(); break;
        case 'capOn': captions(true, a.translateTo ? (langByName(a.translateTo) || { name: a.translateTo }).name : ''); break;
        case 'capOff': captions(false); break;
        case 'liveOn': if (!(await ensureVision())) hud.toast('Live AI needs a vision model: ' + (state.vision.reason || ''), 'bad'); else live.start(); refreshChips(); break;
        case 'liveOff': live.stop(); refreshChips(); break;
        case 'pin':
          if (state.doc >= 0) await pinPage(); else if (state.answer) await pinAnswer(); else hud.toast('Nothing to pin yet', 'warn');
          break;
        case 'diagram': if (state.doc < 0 || !showFigure(state.doc, state.page)) hud.toast('No diagram or table on this page', 'warn'); break;
        case 'export': exporter.open(a.kind, a.format); break;
        case 'expert': await callExpert(); break;
        case 'hangup': expert.hangup(); refreshChips(); hud.toast('Call ended', 'info'); break;
        case 'clearMachine': S.machine = ''; refreshChips(); drawAnswer(); hud.toast('Machine filter cleared', 'info'); break;
        case 'quiet': stopSpeaking(); break;
        default: hud.toast('Unknown command: ' + cmd, 'warn');
      }
    } catch (e) { hud.toast(`${cmd}: ${e.message}`, 'bad', 7000); }
  }
  async function pinPage() {
    const d = state.index.data.docs[state.doc];
    await pins.pin({ kind: 'page', uid: d.uid, page: state.page, title: d.title, text: '' });
    session.log('pin', { text: `Pinned ${d.title} p.${state.page}` });
  }
  async function pinAnswer() {
    await pins.pin({ kind: 'answer', title: state.query.slice(0, 80) || 'Answer', text: state.answer.slice(0, 1500) });
    session.log('pin', { text: 'Pinned answer: ' + state.query.slice(0, 80) });
  }
  async function callExpert() {
    const r = await expert.call({ question: state.query || '(no question yet)', answer: state.query ? state.answer : '', sources: state.hits.map((h) => `${h.doc.title} p.${h.chunk.page}`).join('; '),
      machine: S.machine, last_scan: state.photoInfo ? state.photoInfo.object : '', confidence: state.conf == null ? '' : String(state.conf) });
    if (r && r.link) { reply(`📞 Calling a remote expert...\nThe expert opens:\n${r.link}\n(or the Expert page from the panel)`, 'Calling an expert. Please wait.'); }
    refreshChips();
  }

  function refreshChips() {
    const show = (id, on) => { $(id).style.display = on ? 'inline-flex' : 'none'; };
    show('chip-machine', !!S.machine); $('chip-machine-t').textContent = S.machine;
    show('chip-wake', S.wake && listener.active); show('chip-cap', !!state.captionsOn || $('rag-cap').style.display === 'block');
    show('chip-live', live.on); show('chip-rec', false); show('chip-call', expert.active);
    show('chip-meet', extras.meeting.on); show('chip-gest', extras.gestures.on);
    $('t-meet').classList.toggle('on', extras.meeting.on); $('t-gest').classList.toggle('on', extras.gestures.on);
    $('t-meet').textContent = extras.meeting.on ? '⏹ Stop meeting' : '📝 Meeting notes';
    $('t-cap').classList.toggle('on', $('rag-cap').style.display === 'block' && !state.translateTo);
    $('t-tr').classList.toggle('on', !!state.translateTo); $('t-live').classList.toggle('on', live.on); $('t-rec').classList.toggle('on', media.recording);
    $('t-expert').textContent = expert.active ? '📴 End expert call' : '🧑‍🔧 Call remote expert';
  }
  $('chip-machine-x').onclick = () => act('clearMachine');
  $('chip-call-x').onclick = () => act('hangup');

  // ---------- buttons ----------
  const on = (id, f) => { $(id).onclick = f; };
  on('rag-scan', () => act('scan'));
  on('t-cap', () => captions($('rag-cap').style.display !== 'block' || !!state.translateTo, ''));
  on('t-tr', () => captions(!state.translateTo, S.answerLang === 'English' && S.voiceLang.startsWith('en') ? 'Hindi' : S.answerLang));
  on('t-live', () => act(live.on ? 'liveOff' : 'liveOn'));
  on('t-rem', () => assistant.listReminders());
  on('t-cal', () => assistant.calendar('today'));
  on('t-wx', () => assistant.weather(''));
  on('t-photo', () => act('photo'));
  on('t-rec', () => act(media.recording ? 'videoOff' : 'videoOn'));
  on('t-code', () => act('code'));
  on('t-filters', () => act('filters'));
  on('t-gallery', () => act('gallery'));
  on('t-notes', () => ask('show my notes'));
  on('t-wf', () => (state.query && state.mode === 'pdf' && !/^\(|step$/.test(state.query) ? act('wfstart', { topic: state.query })
    : hud.toast('Ask about a task first (e.g. "how do I start pump P-101?"), or say "guide me through <task>"', 'info', 6000)));
  on('t-next', () => act('next'));
  on('t-diag', () => act('diagram'));
  on('t-pinp', () => (state.doc >= 0 ? pinPage() : hud.toast('No page shown', 'warn')).catch((e) => hud.toast(e.message, 'bad')));
  on('t-pina', () => (state.answer ? pinAnswer() : hud.toast('No answer yet', 'warn')).catch((e) => hud.toast(e.message, 'bad')));
  on('t-exp', () => act('export'));
  on('t-expert', () => act(expert.active ? 'hangup' : 'expert'));
  on('t-tp', () => act('teleprompter'));
  on('t-meet', () => act('meeting'));
  on('t-gest', () => act('gestures'));
  on('t-nav', () => act('directions'));
  on('t-calx', () => window.open('/workspace#calendar', '_blank', 'noopener'));
  on('t-mtl', () => window.open('/workspace#meetings', '_blank', 'noopener'));

  // ---------- settings ----------
  $('s-vl').value = S.voiceLang; $('s-al').value = S.answerLang; $('s-am').value = S.answerMode;
  $('s-sp').checked = S.speak; $('s-wk').checked = S.wake; $('s-la').checked = S.lookToAsk; $('s-bf').checked = S.blurFaces;
  $('s-li').value = S.liveInterval; $('s-tk').value = S.token;
  $('s-vl').onchange = (e) => { S.voiceLang = e.target.value; listener.setLang(S.voiceLang); };
  $('s-al').onchange = (e) => { S.answerLang = e.target.value; };
  $('s-am').onchange = (e) => { S.answerMode = e.target.value; };
  $('s-sp').onchange = (e) => { S.speak = e.target.checked; if (!S.speak) stopSpeaking(); };
  $('s-wk').onchange = (e) => { S.wake = e.target.checked; listener.setLang(S.voiceLang); listener.setWake(S.wake); if (S.wake) hud.toast('👂 Say "Hey Glasses, ..." any time', 'info'); setTimeout(refreshChips, 600); };
  $('s-la').onchange = (e) => { S.lookToAsk = e.target.checked; };
  $('s-bf').onchange = (e) => { S.blurFaces = e.target.checked; };
  $('s-li').onchange = (e) => { S.liveInterval = Math.max(8, Math.min(120, +e.target.value || 20)); };
  $('s-tk').onchange = async (e) => { S.token = e.target.value.trim(); state.index = await RagIndex.load(); cache.clear(); hud.toast('Access token saved - documents reloaded', 'ok'); };
  onSetting((k) => { if (k === 'answerLang' || k === 'answerMode') { setAnswerStyle(S.answerLang, S.answerMode); $('s-al').value = S.answerLang; } });
  if (!speechSupported()) { $('s-wk').disabled = true; $('t-cap').disabled = true; $('t-tr').disabled = true; }
  showEngine();
  $('s-we').value = S.wakeEngine || 'auto';
  $('s-we').onchange = (e) => { S.wakeEngine = e.target.value; listener.useEngine(S.wakeEngine); showEngine(); hud.toast('👂 Wake word engine: ' + (listener.engine === 'chrome' ? 'Chrome' : 'Whisper'), 'info'); setTimeout(refreshChips, 600); };
  $('s-test').onclick = async () => {
    const b = $('s-test'), note = $('s-heard');
    b.disabled = true; listener.pause();
    try {
      const d = await diagnoseMic(S.voiceLang, (m) => { note.textContent = m; });
      const msg = explainDiagnosis(d);
      note.textContent = msg + `  [mic ${d.permission}, level ${d.level.toFixed(3)}, engine: ${d.events.join(' → ') || d.engine}]`;
      hud.toast(msg, msg.startsWith('✅') ? 'ok' : 'warn', 9000);
    } finally { b.disabled = false; listener.resume(); }
  };
  $('s-gs').checked = !!S.gestures; $('s-tf').checked = S.tpFollow !== false; $('s-tw').value = S.tpWpm || 140;
  $('s-gs').onchange = (e) => { if (e.target.checked !== extras.gestures.on) act('gestures'); };
  $('s-tf').onchange = (e) => { S.tpFollow = e.target.checked; };
  $('s-tw').onchange = (e) => { S.tpWpm = Math.max(80, Math.min(220, +e.target.value || 140)); };

  // ---------- ☰ Apps: mirror real Chrome tabs (WhatsApp, Instagram, Email...) as VR windows ----------
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
  $('rag-photo-x').onclick = () => setPhoto(null);

  // Reloads index.json when the server's document set changed (upload page), so new files are searchable in VR.
  async function syncIndex() {
    const v = await serverIndexVersion();
    if (v === null || v === state.index.data.version) return;
    state.index = await RagIndex.load();
    cache.clear();
    const d = state.index.data;
    state.status = state.status.replace(/^\d+ docs?/, `${d.docs.length} docs`);
  }

  function nextHit() {
    if (!state.hits.length) return;
    state.hitIdx = (state.hitIdx + 1) % state.hits.length;
    const h = state.hits[state.hitIdx];
    showPage(h.doc.id, h.chunk.page); syncDom();
  }

  // ---------- small dialogs (teleprompter script, directions) ----------
  function modal(title, html) {
    const old = $('rg-modal'); if (old) old.remove();
    const m = document.createElement('div'); m.id = 'rg-modal';
    m.innerHTML = `<div class="rg-mc"><div class="rg-mh"><b>${title}</b><button id="rg-mx" title="Close">✕</button></div>${html}</div>`;
    document.body.appendChild(m);
    m.querySelectorAll('input, textarea, select').forEach((el) => el.addEventListener('keydown', (e) => e.stopPropagation(), true));
    $('rg-mx').onclick = () => m.remove();
    m.onclick = (e) => { if (e.target === m) m.remove(); };
    return m;
  }
  async function openPrompterDialog() {
    if (extras.teleprompter.on) { extras.teleprompter.stop(); return; }
    const m = modal('🎤 Teleprompter', `
      <label>Write it from your documents</label>
      <div class="rg-mrow"><input id="tp-topic" placeholder="e.g. safety briefing for pump P-101"><input id="tp-min" type="number" min="0.5" max="10" step="0.5" value="1.5" title="minutes" style="width:70px"><button id="tp-gen" class="go">✍ Write</button></div>
      <label>…or paste / edit your script</label>
      <textarea id="tp-text" rows="8" placeholder="Paste the text you want to read…"></textarea>
      <div class="rg-mrow"><select id="tp-saved"><option value="">Saved scripts…</option></select>
        <button id="tp-save">💾 Save</button><button id="tp-go" class="go">▶ Start</button></div>
      <div class="rg-note">${nativeSpeech() && S.tpFollow !== false ? 'It follows your voice: the line you are saying is highlighted.' : 'It scrolls at ' + (S.tpWpm || 140) + ' words a minute.'} Space = pause · ↑↓ = line · + / − = speed · Esc = close.</div>`);
    let saved = [];
    try { saved = (await api('/api/teleprompter/scripts')).scripts; } catch { /* offline */ }
    $('tp-saved').innerHTML += saved.map((x) => `<option value="${x.id}">${x.title.replace(/</g, '&lt;')}</option>`).join('');
    $('tp-saved').onchange = (e) => { const x = saved.find((y) => y.id === e.target.value); if (x) $('tp-text').value = x.text; };
    $('tp-gen').onclick = async () => {
      const t = $('tp-topic').value.trim(); if (!t) return;
      $('tp-gen').disabled = true; $('tp-gen').textContent = '…';
      try { const sc = await api('/api/teleprompter/script', { topic: t, minutes: +$('tp-min').value || 1.5 }); $('tp-text').value = sc.text; }
      catch (e) { hud.toast('Could not write the script: ' + e.message, 'bad'); }
      $('tp-gen').disabled = false; $('tp-gen').textContent = '✍ Write';
    };
    $('tp-save').onclick = async () => { const t = $('tp-text').value.trim(); if (t) { await api('/api/teleprompter/scripts', { text: t, title: $('tp-topic').value.trim() || undefined }); hud.toast('💾 Script saved', 'ok'); } };
    $('tp-go').onclick = () => {
      const t = $('tp-text').value.trim(), title = $('tp-topic').value.trim() || t.split('\n')[0].slice(0, 40);
      if (!t) return;
      m.remove(); extras.teleprompter.start(t, title);
    };
  }
  function openDirectionsDialog() {
    if (extras.nav.on) { extras.nav.stop(); return; }
    const m = modal('🧭 Directions', `
      <label>Where to?</label><input id="nv-to" placeholder="e.g. India Gate, New Delhi">
      <label>From (empty = my location)</label><input id="nv-from" placeholder="my current location">
      <div class="rg-mrow"><select id="nv-mode"><option value="walk">🚶 Walk</option><option value="bike">🚲 Cycle</option><option value="drive">🚗 Drive</option></select>
        <button id="nv-go" class="go">▶ Go</button></div>
      <div class="rg-note">The next turn shows on the lens and moves on as you walk (GPS). Say "next" to skip ahead, "stop navigation" to end. Maps © OpenStreetMap.</div>`);
    $('nv-to').focus();
    $('nv-go').onclick = () => {
      const to = $('nv-to').value.trim(), mode = $('nv-mode').value, from = $('nv-from').value.trim();
      if (!to) return;
      m.remove(); extras.nav.start(to, mode, from);
    };
  }

  // ---------- keys ----------
  window.addEventListener('keydown', (e) => {
    if (/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName) || e.repeat) return;
    if (extras.teleprompter.on) {                       // Space pause · ↑↓ line · +/- speed · Esc close
      if (e.key === ' ') { e.preventDefault(); extras.teleprompter.pause(); return; }
      if (e.key === 'ArrowDown') { extras.teleprompter.next(); return; }
      if (e.key === 'ArrowUp') { extras.teleprompter.prev(); return; }
      if (e.key === '+' || e.key === '=') { extras.teleprompter.faster(20); return; }
      if (e.key === '-') { extras.teleprompter.faster(-20); return; }
      if (e.key === 'Escape') { extras.teleprompter.stop(); return; }
    }
    if (e.key === ']') showPage(state.doc, state.page + 1);
    else if (e.key === '[') showPage(state.doc, state.page - 1);
    else if (e.key === 'Tab') { e.preventDefault(); nextHit(); }
    else if (e.key === 'x' || e.key === 'X') act('scan');
    else if (e.key === 'n' || e.key === 'N') act('next');
    else if (e.key === 'b' || e.key === 'B') act('prev');
  });

  // gaze + pinch / controller input inside the XR session
  world.renderer.xr.addEventListener('sessionstart', () => {
    const s = world.renderer.xr.getSession();
    s.addEventListener('select', () => act('next'));
    s.addEventListener('squeeze', nextHit);
  });

  // ---------- boot ----------
  (async () => {
    try {
      state.index = await RagIndex.load();
      state.llm = await loadLlmConfig();
      const d = state.index.data;
      state.status = `${d.docs.length} docs · AI: ${state.llm.provider} · search: ${(state.llm.search || 'keyword').split(' ')[0]}`;
      state.answer = 'Hold V (or tap the mic) and ASK by voice - or turn on "Hey Glasses" in ⚙ Settings.\n' +
        'Answers from your documents show the page; anything else gets a general AI answer.\n\nTry saying:\n' + EXAMPLES.map((s) => '• ' + s).join('\n') +
        '\n\n🔍 Scan = what is in the red ring · 🛠 Tools = captions, Live AI, camera, procedures, reports, expert';
    } catch (e) {
      state.status = 'ERROR: ' + e.message; state.answer = state.status;
    }
    syncDom(); drawPdf(null, 'Document page appears here');

    const v = setupVoice({
      button: $('rag-mic'),
      useServer: !!(state.llm && state.llm.stt),
      getLang: () => langByCode(S.voiceLang),
      onStart: () => { stopSpeaking(); listener.pause(); },
      onStop: () => listener.resume(),
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
    if (S.wake) { listener.setLang(S.voiceLang); listener.setWake(true); }
    syncDom();

    setupCamera({ button: $('rag-cam'), onPhoto: async (p) => askAboutCanvas(await dataUrlToCanvas(p)) });
    state.vision = await visionStatus();
    if (!state.vision.available && !/not reachable/.test(state.vision.reason)) for (const id of ['rag-cam', 'rag-scan', 't-live']) { $(id).disabled = true; $(id).title = 'Photo questions unavailable: ' + state.vision.reason; }
    state.status += ` · photo: ${state.vision.available ? 'on' : 'off'}`;
    syncDom(); refreshChips();
    setInterval(refreshChips, 2000);
  })();

  window.__vrRag = { state, ask, act, setPhoto, world, apps, hud, pins, workflow, live, media, expert, extras, privacy, S }; // for debugging in DevTools
}
