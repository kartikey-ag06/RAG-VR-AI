// hud.js - the "in-lens display": a small panel that stays in front of your eyes (head-locked) like the
// Meta Ray-Ban Display. Shows notifications, live captions / translation, the current procedure step and
// Live AI notes, turn-by-turn directions, the teleprompter, and a privacy light (red = camera / recording,
// amber = microphone) whenever anything is captured. Hidden when there is nothing to show.
import { Mesh, PlaneGeometry, MeshBasicMaterial, CanvasTexture, SRGBColorSpace, Vector3, Quaternion } from '@iwsdk/core';

const W = 1024, H = 720;
const COLORS = { info: '#8cc8ff', ok: '#5fd08f', warn: '#ffcf6a', bad: '#ff7a8a', expert: '#c9a2ff' };

function wrap(ctx, text, x, y, maxW, lh, maxLines) {
  const words = String(text || '').split(/\s+/);
  let line = '', n = 0;
  for (const w of words) {
    const t = line ? line + ' ' + w : w;
    if (ctx.measureText(t).width > maxW && line) {
      ctx.fillText(line, x, y); y += lh; n++; line = w;
      if (n >= maxLines) return y;
    } else line = t;
  }
  if (line && n < maxLines) { ctx.fillText(line, x, y); y += lh; }
  return y;
}

export function setupHud(world) {
  const canvas = document.createElement('canvas');
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext('2d');
  const tex = new CanvasTexture(canvas); tex.colorSpace = SRGBColorSpace;
  const mesh = new Mesh(new PlaneGeometry(0.56, 0.56 * H / W),
    new MeshBasicMaterial({ map: tex, transparent: true, depthTest: false, depthWrite: false, toneMapped: false }));
  mesh.renderOrder = 999;
  mesh.visible = false;
  world.createTransformEntity(mesh);

  const st = { toasts: [], caption: null, step: null, live: '', rec: null, nav: null, prompter: null };
  const dom = document.createElement('div');
  dom.style.cssText = 'position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:2147483001;display:flex;flex-direction:column;gap:6px;align-items:center;pointer-events:none';
  document.body.appendChild(dom);

  function rr(x, y, w, h, r) {
    ctx.beginPath(); ctx.moveTo(x + r, y); ctx.arcTo(x + w, y, x + w, y + h, r); ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r); ctx.arcTo(x, y, x + w, y, r); ctx.closePath();
  }
  // a lens-display card: dark glass, soft accent glow, a coloured icon dot
  function card(y, h, color, icon) {
    ctx.save();
    ctx.shadowColor = color; ctx.shadowBlur = 18;
    rr(14, y, W - 28, h, 22);
    const g = ctx.createLinearGradient(0, y, 0, y + h);
    g.addColorStop(0, 'rgba(22,28,44,0.88)'); g.addColorStop(1, 'rgba(10,13,22,0.88)');
    ctx.fillStyle = g; ctx.fill();
    ctx.restore();
    rr(14, y, W - 28, h, 22); ctx.strokeStyle = color + '66'; ctx.lineWidth = 2; ctx.stroke();
    if (icon) {
      ctx.fillStyle = color + '33'; ctx.beginPath(); ctx.arc(52, y + Math.min(h / 2, 32), 22, 0, Math.PI * 2); ctx.fill();
      ctx.font = '24px Segoe UI Emoji, Apple Color Emoji, sans-serif'; ctx.textAlign = 'center'; ctx.fillStyle = '#fff';
      ctx.fillText(icon, 52, y + Math.min(h / 2, 32) - 13); ctx.textAlign = 'left';
    }
  }
  const ICON = { info: 'ℹ', ok: '✓', warn: '!', bad: '✕', expert: '☎' };
  function split(t) {                                 // leading emoji becomes the icon
    const m = String(t).match(/^(\p{Extended_Pictographic}\uFE0F?)\s*/u);
    return m ? [m[1], t.slice(m[0].length)] : [null, t];
  }

  // teleprompter: the line to say now is big and bright, words already said turn green, the next lines wait below
  function drawPrompter(y0) {
    const p = st.prompter, lines = p.lines || [];
    const h = H - y0 - 14;
    card(y0, h, '#4fb3ff', null);
    ctx.fillStyle = '#8cc8ff'; ctx.font = '700 20px Segoe UI, Arial';
    ctx.fillText(`TELEPROMPTER · ${p.paused ? 'paused' : p.follow ? 'follows your voice' : p.wpm + ' words/min'} · ${Math.min(lines.length, p.cur + 1)}/${lines.length}`, 40, y0 + 18);
    rr(40, y0 + 48, W - 80, 6, 3); ctx.fillStyle = '#26324d'; ctx.fill();
    rr(40, y0 + 48, Math.max(6, (W - 80) * (lines.length ? (p.cur + p.within) / lines.length : 0)), 6, 3); ctx.fillStyle = '#4fb3ff'; ctx.fill();
    let y = y0 + 72;
    const prev = lines[p.cur - 1];
    if (prev) { ctx.fillStyle = '#5f6b86'; ctx.font = '28px Segoe UI, Arial'; ctx.fillText(prev.slice(0, 60), 40, y); y += 44; }
    const cur = lines[p.cur] || '';
    ctx.font = '700 40px Segoe UI, Arial';
    const words = cur.split(/\s+/), said = Math.round(words.length * (p.within || 0));
    let x = 40, row = 0;
    words.forEach((w, i) => {
      const ww = ctx.measureText(w + ' ').width;
      if (x + ww > W - 40) { x = 40; row++; }
      if (row > 2) return;
      ctx.fillStyle = i < said ? '#5fd08f' : '#ffffff'; ctx.fillText(w, x, y + row * 50); x += ww;
    });
    y += (row + 1) * 50 + 10;
    ctx.font = '30px Segoe UI, Arial'; ctx.fillStyle = '#aab6cf';
    for (let i = p.cur + 1; i < lines.length && y < H - 60; i++) { ctx.fillText(lines[i].slice(0, 58), 40, y); y += 42; }
  }

  function draw() {
    const now = Date.now();
    st.toasts = st.toasts.filter((t) => t.until > now);
    const any = st.toasts.length || st.caption || st.step || st.live || st.rec || st.nav || st.prompter;
    mesh.visible = !!any;
    if (!any) return;
    ctx.clearRect(0, 0, W, H);
    let y = 14;
    ctx.textBaseline = 'top';
    if (st.rec) {                                       // privacy light: always on top, never hidden by other cards
      const r = st.rec, red = r.camera || r.screen || r.recording, label = r.recording ? 'REC' : r.camera ? 'CAM' : r.screen ? 'SCREEN' : 'MIC';
      const blink = (Date.now() / 500 | 0) % 2 === 0 || !r.recording;
      ctx.font = '700 24px Segoe UI, Arial';
      const w = ctx.measureText(label).width + 62;
      rr(W - w - 18, 6, w, 42, 21); ctx.fillStyle = 'rgba(10,12,20,0.85)'; ctx.fill();
      ctx.fillStyle = red ? (blink ? '#ff3b5c' : '#7a1d2c') : '#ffb020';
      ctx.beginPath(); ctx.arc(W - w + 6, 27, 10, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = '#fff'; ctx.fillText(label, W - w + 26, 15);
      y = 58;
    }
    if (st.prompter) { drawPrompter(y); tex.needsUpdate = true; return; }   // a talk in progress: only the script
    if (st.nav) {
      const n = st.nav;
      card(y, 112, '#34d399', null);
      ctx.fillStyle = '#34d399'; ctx.font = '700 64px Segoe UI Symbol, Segoe UI, Arial'; ctx.textAlign = 'center';
      ctx.fillText(n.arrow || '↑', 70, y + 18); ctx.textAlign = 'left';
      ctx.fillStyle = '#fff'; ctx.font = '700 36px Segoe UI, Arial'; ctx.fillText(n.dist || '', 128, y + 14);
      ctx.fillStyle = '#d5f5e6'; ctx.font = '26px Segoe UI, Arial'; ctx.fillText((n.text || '').slice(0, 52), 128, y + 60);
      ctx.fillStyle = '#8fb8a6'; ctx.font = '20px Segoe UI, Arial'; ctx.textAlign = 'right';
      ctx.fillText(n.eta || '', W - 40, y + 22); ctx.textAlign = 'left';
      y += 122;
    }
    for (const t of st.toasts.slice(-2)) {
      if (y > H - 80) break;
      const c = COLORS[t.kind] || COLORS.info, [ic, txt] = split(t.text);
      card(y, 64, c, ic || ICON[t.kind] || 'ℹ');
      ctx.font = '600 29px Segoe UI, Arial'; ctx.fillStyle = '#f4f7fd';
      ctx.fillText(txt.length > 54 ? txt.slice(0, 54) + '…' : txt, 88, y + 16);
      y += 74;
    }
    if (st.step) {
      const s = st.step;
      card(y, 160, '#5aa9ff', '🧭');
      ctx.fillStyle = '#8cc8ff'; ctx.font = '700 22px Segoe UI, Arial';
      ctx.fillText(`STEP ${s.n} OF ${s.total}`, 88, y + 14);
      rr(300, y + 22, W - 340, 10, 5); ctx.fillStyle = '#26324d'; ctx.fill();
      rr(300, y + 22, Math.max(12, (W - 340) * (s.n / s.total)), 10, 5); ctx.fillStyle = '#5aa9ff'; ctx.fill();
      ctx.fillStyle = '#fff'; ctx.font = '700 32px Segoe UI, Arial'; ctx.fillText(s.title.slice(0, 44), 88, y + 46);
      ctx.fillStyle = '#d5dbea'; ctx.font = '25px Segoe UI, Arial';
      const yy = wrap(ctx, s.detail, 88, y + 88, W - 130, 30, 1);
      if (s.warning) { ctx.fillStyle = '#ffcf6a'; wrap(ctx, '⚠ ' + s.warning, 88, yy, W - 130, 30, 1); }
      y += 170;
    }
    if (st.live) {
      card(y, 58, '#c9a2ff', '👁');
      ctx.font = '26px Segoe UI, Arial'; ctx.fillStyle = '#efe6ff';
      ctx.fillText(st.live.length > 58 ? st.live.slice(0, 58) + '…' : st.live, 88, y + 15);
      y += 68;
    }
    if (st.caption) {
      const c = st.caption;
      const h = c.translation ? 104 : 62;
      card(y, h, '#ffffff', '💬');
      ctx.fillStyle = c.final ? '#ffffff' : '#b9c1d3'; ctx.font = '28px Segoe UI, Arial';
      const tail = (q) => (q.length > 56 ? '…' + q.slice(-56) : q);
      ctx.fillText(tail(c.text), 88, y + 15);
      if (c.translation) { ctx.fillStyle = '#ffd98a'; ctx.fillText(tail(c.translation), 88, y + 56); }
    }
    tex.needsUpdate = true;
  }

  // keep it in front of the eyes, a little low (like a lens display) - follows smoothly
  const cp = new Vector3(), cq = new Quaternion(), target = new Vector3(), off = new Vector3();
  const prev = world.scene.onBeforeRender;
  world.scene.onBeforeRender = function (renderer, scene, camera, ...rest) {
    if (prev) prev.call(this, renderer, scene, camera, ...rest);
    if (!mesh.visible) return;
    camera.matrixWorld.decompose(cp, cq, off);
    off.set(0.05, -0.275, -0.9).applyQuaternion(cq);
    target.copy(cp).add(off);
    if (mesh.position.distanceTo(target) > 1.5) mesh.position.copy(target); else mesh.position.lerp(target, 0.25);
    mesh.quaternion.slerp(cq, 0.25);
    mesh.updateMatrixWorld(true);
  };
  setInterval(() => { if (st.toasts.length || (st.rec && st.rec.recording)) draw(); }, 500);   // expire toasts, blink REC

  return {
    mesh,
    toast(text, kind = 'info', ms = 4500) {
      st.toasts.push({ text: String(text), kind, until: Date.now() + ms }); draw();
      if (!['bad', 'warn', 'expert'].includes(kind)) return;   // the lens shows it; problems + expert messages are also on screen
      const d = document.createElement('div');
      d.textContent = text;
      d.style.cssText = `background:rgba(13,18,30,.95);border:1px solid ${COLORS[kind] || COLORS.info};color:#eef2fa;padding:8px 14px;border-radius:10px;font:13px Segoe UI,Arial;max-width:560px;box-shadow:0 6px 18px rgba(0,0,0,.35)`;
      dom.appendChild(d); setTimeout(() => d.remove(), ms);
    },
    setCaption(text, translation, final) { st.caption = text ? { text, translation, final } : null; draw(); },
    setStep(s) { st.step = s; draw(); },
    setLive(text) { st.live = text || ''; draw(); },
    setRec(r) { st.rec = r && (r.camera || r.mic || r.screen || r.recording) ? r : null; draw(); },
    setNav(n) { st.nav = n; draw(); },
    setPrompter(p) { st.prompter = p; draw(); },
    redraw: draw,
  };
}
