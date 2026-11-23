// ui-overlay.js - the DOM side of the app: control panel (mic, scan, photo, voice toggle, answer text, status)
// and the "Apps" drawer. It holds no RAG logic: the coordinator (rag-scene.js) feeds it state and binds handlers.
import { CONFIDENCE } from './xr-panels.js';

const OVERLAY_HTML = `
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
      #rag-conf{display:none;margin-top:10px;padding:4px 10px;border-radius:8px;font-size:12px;font-weight:700;color:#fff;width:fit-content}
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
          <button id="rag-speak" title="Read answers aloud (on/off)">🔊 Voice on</button>
          <button id="rag-apps" title="Open apps (Chrome, WhatsApp, Instagram, Email...) as VR windows">☰ Apps</button>
        </div>
        <div id="rag-photo"><img id="rag-photo-img" alt=""><span id="rag-photo-txt">Photo attached</span><button id="rag-photo-x" title="Remove photo">✕</button></div>
        <div id="rag-conf"></div><div id="rag-ans"></div><div id="rag-src"></div>
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

const LABELS = {
  pdf: '📄 From the SEBI PDFs',
  general: '🌐 General AI answer (not from the PDFs)',
  offline: '⚙ Offline (best matching PDF sentences)',
};
const CONF_COLOR = { high: '#2e7d4f', medium: '#a37b14', low: '#a8323f' };

export class UIOverlay {
  constructor() {
    this.root = document.createElement('div');
    this.root.innerHTML = OVERLAY_HTML;
    document.body.appendChild(this.root);
    this.$ = (id) => document.getElementById(id);
    this.$('rag-min').onclick = () => this.$('rag-ui').classList.toggle('min');
  }

  /** elements the VR scan hides while it grabs a frame */
  get hideOnScan() { return [this.$('rag-ui'), this.$('rag-drawer')]; }

  setStatus(text) { this.$('rag-status').textContent = text; }
  setVoiceHint(text) { this.$('rag-voice').textContent = text; }
  setSpeakLabel(on) { this.$('rag-speak').textContent = on ? '🔊 Voice on' : '🔇 Voice off'; }

  /** answer text, mode label, confidence chip and the list of used sources */
  render(state) {
    this.setStatus(state.status);
    const label = LABELS[state.mode] || '';
    this.$('rag-ans').textContent = (label ? (state.usedPhoto ? '📷 Photo + ' : '') + label + '\n\n' : '') + state.answer;
    const chip = this.$('rag-conf'), conf = CONFIDENCE[state.level];
    if (conf) {
      chip.style.display = 'block'; chip.style.background = CONF_COLOR[state.level];
      chip.textContent = conf[0] + (state.confidence != null ? ' · ' + Math.round(state.confidence * 100) + '%' : '');
    } else chip.style.display = 'none';
    this.$('rag-src').textContent = state.hits.length
      ? 'PDF sources used:\n' + state.hits.map((h, i) => `${i === state.hitIdx ? '▶' : '  '} [${i + 1}] ${h.doc.title} - p.${h.chunk.page}`).join('\n') : '';
  }

  showPhoto(dataUrl, text) {
    const box = this.$('rag-photo');
    if (!dataUrl) { box.style.display = 'none'; return; }
    this.$('rag-photo-img').src = dataUrl; this.$('rag-photo-txt').textContent = text;
    box.style.display = 'flex';
  }
  setPhotoText(text) { this.$('rag-photo-txt').textContent = text; }

  // ---------- ☰ Apps drawer: mirror real Chrome tabs (WhatsApp, Instagram, Email...) as VR windows ----------
  renderWins(list, apps) {
    const $ = this.$;
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

  showStep(app, apps) {
    const $ = this.$;
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

  buildAppButtons(APPS, apps) {
    const $ = this.$;
    APPS.forEach((app) => {
      const b = document.createElement('button'); b.className = 'rg-app';
      b.innerHTML = `<i>${app.icon}</i>${app.name}`; b.title = app.note || ('Open ' + app.name + ' and show it in VR');
      b.onclick = () => { if (app.url) window.open(app.url, '_blank', 'noopener'); this.showStep(app, apps); };
      $('rag-applist').appendChild(b);
    });
    if (!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia)) $('rag-applist').insertAdjacentHTML('afterend', '<div class="rg-note">This browser cannot share tabs - use Chrome on a computer.</div>');
    $('rag-apps').onclick = () => $('rag-drawer').classList.toggle('open');
    $('rag-apps-x').onclick = () => $('rag-drawer').classList.remove('open');
  }
}
