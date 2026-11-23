// workflow.js - work sessions (audit trail), guided step-by-step procedures, 3D pins, look-to-ask,
// and report exports.
import { Mesh, PlaneGeometry, MeshBasicMaterial, CanvasTexture, SRGBColorSpace, DoubleSide, Raycaster, Vector2, Vector3 } from '@iwsdk/core';
import { api, apiUrl } from './rag.js';

// ---------------------------------------------------------------- session log
export function setupSession() {
  let id = null;
  try { id = sessionStorage.getItem('vr-session'); } catch { /* ignore */ }
  const ready = (async () => {
    if (id) { try { await api('/api/sessions/' + id); return id; } catch { id = null; } }
    try { id = (await api('/api/sessions', { device: navigator.userAgent.slice(0, 70) })).id; sessionStorage.setItem('vr-session', id); } catch { id = null; }
    return id;
  })();
  return {
    ready,
    get id() { return id; },
    log(type, data) { ready.then((sid) => sid && api(`/api/sessions/${sid}/log`, { type, data }).catch(() => {})); },
  };
}

// ---------------------------------------------------------------- 3D pins + look-to-ask
function textCanvas(title, body, accent = '#8cc8ff') {
  const c = document.createElement('canvas'); c.width = 800; c.height = 600;
  const x = c.getContext('2d');
  x.fillStyle = 'rgba(16,22,36,0.96)'; x.fillRect(0, 0, 800, 600);
  x.strokeStyle = accent; x.lineWidth = 6; x.strokeRect(3, 3, 794, 594);
  x.fillStyle = accent; x.font = 'bold 34px Segoe UI, Arial'; x.textBaseline = 'top'; x.fillText(('📌 ' + title).slice(0, 38), 26, 22);
  x.fillStyle = '#e8ecf4'; x.font = '27px Segoe UI, Arial';
  let y = 82;
  for (const para of String(body || '').split('\n')) {
    let line = '';
    for (const w of para.split(' ')) {
      const t = line ? line + ' ' + w : w;
      if (x.measureText(t).width > 748 && line) { x.fillText(line, 26, y); y += 34; line = w; if (y > 560) return c; } else line = t;
    }
    x.fillText(line, 26, y); y += 40; if (y > 560) break;
  }
  return c;
}

export function setupPins({ world, hud, getTargets, onDwell, enabled }) {
  const meshes = new Map();           // pin id -> mesh
  const tokenQ = () => { let t = ''; try { t = localStorage.getItem('vr-user-token') || ''; } catch { /* ignore */ } return t ? '?token=' + encodeURIComponent(t) : ''; };

  function makeMesh(p) {
    let tex, w = 0.46, h = 0.345;
    if (p.kind === 'page' && p.uid) {
      const img = new Image(); img.crossOrigin = 'anonymous';
      const c = document.createElement('canvas'); c.width = 707; c.height = 1000;
      tex = new CanvasTexture(c); h = w * 1000 / 707;
      img.onload = () => {
        const x = c.getContext('2d'); x.fillStyle = '#fff'; x.fillRect(0, 0, 707, 1000); x.drawImage(img, 0, 0, 707, 1000);
        x.fillStyle = 'rgba(16,22,36,0.85)'; x.fillRect(0, 0, 707, 52); x.fillStyle = '#8cc8ff'; x.font = 'bold 28px Segoe UI, Arial';
        x.fillText(('📌 ' + (p.title || 'Page') + ' · p.' + p.page).slice(0, 44), 14, 36); tex.needsUpdate = true;
      };
      img.src = apiUrl(`/api/pins/page/${p.uid}/${p.page}`) + tokenQ();
    } else tex = new CanvasTexture(textCanvas(p.title || p.kind, p.text, p.kind === 'step' ? '#5fd08f' : '#8cc8ff'));
    tex.colorSpace = SRGBColorSpace;
    const m = new Mesh(new PlaneGeometry(w, h), new MeshBasicMaterial({ map: tex, side: DoubleSide, transparent: true, toneMapped: false }));
    let { pos, yaw } = p.pose;
    // pins saved by older versions sat straight ahead and covered the answer: move those to the side
    const dir = Math.atan2(-pos[0], -pos[2]);
    if (Math.abs(dir) < 0.6 && Math.hypot(pos[0], pos[2]) < 2.5) {
      const k = meshes.size, side = k % 2 ? 1 : -1;
      yaw = side * (0.95 + 0.42 * Math.floor(k / 2));
      const d = Math.max(1.1, Math.hypot(pos[0], pos[2]));
      pos = [-Math.sin(yaw) * d, pos[1], -Math.cos(yaw) * d];
    }
    m.position.set(...pos); m.rotation.set(0, yaw, 0);
    m.userData.pin = p;
    world.createTransformEntity(m);
    return m;
  }
  function remove(id) {
    const m = meshes.get(id); if (!m) return;
    m.visible = false; if (m.parent) m.parent.remove(m); m.geometry.dispose(); meshes.delete(id);
  }
  async function sync() {
    let pins;
    try { pins = (await api('/api/pins')).pins; } catch { return; }
    const ids = new Set(pins.map((p) => p.id));
    for (const id of [...meshes.keys()]) if (!ids.has(id)) remove(id);           // unpinned on the dashboard
    for (const p of pins) if (!meshes.has(p.id)) meshes.set(p.id, makeMesh(p));
  }
  /** New pins go around you (right, left, further right...) so they never cover the answer panels. */
  function poseInFront(dist = 1.3) {
    const pos = new Vector3(), f = new Vector3();
    world.camera.getWorldPosition(pos); world.camera.getWorldDirection(f);
    f.y = 0; if (f.lengthSq() < 1e-6) f.set(0, 0, -1); f.normalize();
    const k = meshes.size, side = k % 2 ? 1 : -1, yaw = Math.atan2(-f.x, -f.z) + side * (0.95 + 0.42 * Math.floor(k / 2));
    return { pos: [pos.x - Math.sin(yaw) * dist, pos.y, pos.z - Math.cos(yaw) * dist], yaw };
  }
  async function pin(data) {
    const p = await api('/api/pins', { ...data, pose: poseInFront() });
    if (!meshes.has(p.id)) meshes.set(p.id, makeMesh(p));       // the 10 s sync may already have added it
    hud.toast('📌 Pinned here — also on the desktop Workspace', 'ok');
    return p;
  }
  async function unpinNearest() {
    const t = gazeTarget(); if (!t || !t.userData.pin) return false;
    await api('/api/pins/' + t.userData.pin.id, undefined, 'DELETE'); remove(t.userData.pin.id); return true;
  }

  // ---- look-to-ask: keep looking at a pinned page or the PDF panel for 2 s
  const ray = new Raycaster(), centre = new Vector2(0, 0);
  function gazeTarget() {
    ray.setFromCamera(centre, world.camera);
    const hit = ray.intersectObjects([...meshes.values(), ...getTargets()].filter((m) => m.visible), false)[0];
    return hit ? hit.object : null;
  }
  // only after the head has TURNED to the target (not whatever is ahead when the app opens), and once per look
  let cur = null, since = 0, fired = null, prev = undefined, hinted = false;
  const started = Date.now();
  setInterval(() => {
    if (!enabled() || Date.now() - started < 6000) { cur = null; prev = undefined; return; }
    const t = gazeTarget();
    if (t !== cur) { prev = cur; cur = t; since = Date.now(); hinted = false; if (!t) fired = null; return; }
    if (!t || t === fired || prev === undefined) return;
    const held = Date.now() - since;
    if (held > 1000 && !hinted) { hinted = true; hud.toast('👁 Keep looking to have this page explained…', 'info', 1500); }
    if (held > 2500) { fired = t; onDwell(t); }
  }, 200);

  sync(); setInterval(() => { if (!document.hidden) sync(); }, 30000);   // pins made on another device appear within 30 s
  return { pin, unpinNearest, sync, meshes: () => [...meshes.values()], poseInFront };
}

// ---------------------------------------------------------------- guided procedures
export function setupWorkflow({ hud, show, say, session, getMachine }) {
  let wf = null, i = 0;
  const step = () => wf && wf.steps[i];
  function render() {
    const s = step();
    if (!s) { hud.setStep(null); return; }
    hud.setStep({ n: i + 1, total: wf.steps.length, title: s.title, detail: s.detail, warning: s.warning });
    show(wf, s, i);
    say(`Step ${i + 1}. ${s.title}. ${s.detail}${s.warning ? ' Warning: ' + s.warning : ''}`);
  }
  return {
    get active() { return !!wf; },
    get current() { return wf ? { wf, i, step: step() } : null; },
    async start(topic) {
      hud.toast('🧭 Building the procedure from your documents...', 'info', 6000);
      wf = await api('/api/workflows/generate', { topic, machine: getMachine() });
      i = 0;
      session.log('workflow', { text: `Started: ${wf.title}`, id: wf.id, steps: wf.steps.length });
      hud.toast(`🧭 ${wf.title}: ${wf.steps.length} steps — say "next" when done`, 'ok', 6000);
      render();
      return wf;
    },
    next() {
      if (!wf) return false;
      session.log('step', { text: `Done step ${i + 1}: ${step().title}` });
      if (i < wf.steps.length - 1) { i++; render(); return true; }
      const t = wf.title; wf = null; hud.setStep(null);
      hud.toast(`✅ ${t} complete — say "export report"`, 'ok', 8000);
      say('Procedure complete. You can say: export report.');
      session.log('workflow', { text: `Completed: ${t}` });
      return true;
    },
    prev() { if (wf && i > 0) { i--; render(); return true; } return false; },
    repeat() { if (wf) render(); return !!wf; },
    stop() { if (wf) { session.log('workflow', { text: `Stopped at step ${i + 1}: ${wf.title}` }); wf = null; hud.setStep(null); } },
  };
}

// ---------------------------------------------------------------- export dialog
export function setupExport({ session, hud }) {
  const box = document.createElement('div');
  box.style.cssText = 'position:fixed;inset:0;z-index:2147483002;background:rgba(5,8,15,.8);display:none;align-items:center;justify-content:center';
  box.innerHTML = `<div style="background:#0f1526;border:1px solid #2a3550;border-radius:14px;padding:16px;width:min(420px,94vw);color:#e8e8e8;font:13px Segoe UI,Arial">
    <div style="font-size:15px;color:#8cc8ff;margin-bottom:8px">📄 Export this work session</div>
    <label>Type <select id="ex-k"><option value="inspection">Inspection report</option><option value="incident">Incident log</option><option value="workorder">Work order</option></select></label>
    <div style="margin:10px 0" id="ex-btns"></div><div id="ex-conn"></div>
    <div style="color:#8a95ab;font-size:11px;margin-top:8px">The AI turns your questions, scans and steps into the report. All sessions are on the desktop Workspace page too.</div>
    <button id="ex-x" style="margin-top:10px">Close</button></div>`;
  document.body.appendChild(box);
  const $ = (s) => box.querySelector(s);
  box.querySelectorAll('select,button').forEach((e) => { e.style.cssText = 'margin:3px;padding:7px 11px;border-radius:8px;border:1px solid #3a4766;background:#1b2540;color:#dfe8ff;cursor:pointer;font:inherit'; });
  $('#ex-x').onclick = () => { box.style.display = 'none'; };
  async function open(kind, format) {
    await session.ready;
    if (!session.id) { hud.toast('No session yet (backend not reachable)', 'bad'); return; }
    if (kind) $('#ex-k').value = kind;
    if (format) { download(format); return; }
    box.style.display = 'flex';
    $('#ex-btns').innerHTML = ['pdf', 'docx', 'json'].map((f) => `<button data-f="${f}">⬇ ${f.toUpperCase()}</button>`).join('');
    $('#ex-btns').querySelectorAll('button').forEach((b) => { b.style.cssText = $('#ex-x').style.cssText; b.onclick = () => download(b.dataset.f); });
    try {
      const c = await api('/api/connectors');
      const outs = c.connectors.filter((x) => (c.types[x.type] || {}).dir === 'out' && x.enabled);
      $('#ex-conn').innerHTML = outs.length ? 'Send to: ' + outs.map((x) => `<button data-c="${x.id}">${x.name}</button>`).join('') : '<span style="color:#8a95ab">Add Jira / ServiceNow / email / webhook on the Admin page to send reports.</span>';
      $('#ex-conn').querySelectorAll('button').forEach((b) => {
        b.style.cssText = $('#ex-x').style.cssText;
        b.onclick = async () => {
          b.disabled = true;
          try { const r = await api(`/api/sessions/${session.id}/send`, { connector: b.dataset.c, kind: $('#ex-k').value }); hud.toast('📤 Sent: ' + r.sent, 'ok'); }
          catch (e) { hud.toast('Send failed: ' + e.message, 'bad', 7000); }
          b.disabled = false;
        };
      });
    } catch { $('#ex-conn').textContent = ''; }
  }
  function download(f) {
    window.open(apiUrl(`/api/sessions/${session.id}/export?format=${f}&kind=${$('#ex-k').value}`), '_blank');
    hud.toast(`📄 Building the ${$('#ex-k').selectedOptions[0].textContent} (${f.toUpperCase()})...`, 'info');
  }
  return { open };
}
