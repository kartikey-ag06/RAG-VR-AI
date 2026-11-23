// vrscan.js - "Scan VR view": grabs what the user sees as a photo, so the vision model can say what the
// object in the middle of the view is.
//  - In the emulator's passthrough (AR) session the "real world" is IWER's simulated room, drawn on its own
//    canvas behind the app canvas: both layers are combined, like the user's eyes see them.
//  - Our own answer / PDF panels are hidden for that frame, so they don't cover the object.

const MAX_SIDE = 1024;
const KEEP = 0.7;          // keep the middle 70% of the view - that is where the user is looking

const nextFrame = () => new Promise((r) => {
  let done = false;
  const go = () => { if (!done) { done = true; r(); } };
  requestAnimationFrame(go); setTimeout(go, 250);          // some XR sessions pause window rAF
});
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// ---- paint order: which canvases are drawn below (= behind) the app canvas ----
// Different emulators put the simulated room on different canvases (the IWSDK built-in emulator, the
// Immersive Web Emulator Chrome extension...), so we don't guess names: we take every visible canvas
// that the browser paints at or below the app canvas, at its real place on screen.
function zPath(el) {
  const out = [];
  for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
    const cs = getComputedStyle(e);
    const pd = e.parentElement ? getComputedStyle(e.parentElement).display : '';
    const flexItem = /flex|grid/.test(pd);                       // z-index also works on flex/grid items
    if (cs.zIndex !== 'auto' && (cs.position !== 'static' || flexItem)) out.unshift(parseInt(cs.zIndex, 10) || 0);
  }
  return out;
}
function paintCmp(a, b) {
  const pa = zPath(a), pb = zPath(b);
  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d) return d;
  }
  return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
}
function visible(c) {
  const r = c.getBoundingClientRect();
  if (r.width < 50 || r.height < 50) return false;
  return c.checkVisibility ? c.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) : true;
}
function layers(app) {
  const all = [...document.querySelectorAll('canvas')].filter((c) => c === app || visible(c)).sort(paintCmp);
  return all.slice(0, all.indexOf(app) + 1);
}

/** Draws the canvases at their on-screen position into one viewport-sized picture. */
function composite(list) {
  const dpr = window.devicePixelRatio || 1;
  const out = document.createElement('canvas');
  out.width = Math.round(innerWidth * dpr); out.height = Math.round(innerHeight * dpr);
  const x = out.getContext('2d');
  x.fillStyle = '#000'; x.fillRect(0, 0, out.width, out.height);
  for (const c of list) {
    const r = c.getBoundingClientRect();
    try { x.drawImage(c, r.left * dpr, r.top * dpr, r.width * dpr, r.height * dpr); } catch { /* unreadable layer */ }
  }
  return out;
}

/** Share of almost-black pixels in the middle of the picture (1 = nothing visible). */
function blackShare(c) {
  const t = document.createElement('canvas'); t.width = 96; t.height = 60;
  const x = t.getContext('2d');
  x.drawImage(c, c.width * 0.15, c.height * 0.15, c.width * 0.7, c.height * 0.7, 0, 0, 96, 60);
  const d = x.getImageData(0, 0, 96, 60).data;
  let n = 0;
  for (let i = 0; i < d.length; i += 4) if (d[i] + d[i + 1] + d[i + 2] < 24) n++;
  return n / (d.length / 4);
}

// ---- fallback: capture this browser tab (works with any emulator; Chrome asks once to share the tab) ----
let tabStream = null;
async function grabTab(hideEls) {
  if (!tabStream || !tabStream.active) {
    tabStream = await navigator.mediaDevices.getDisplayMedia({ video: { displaySurface: 'browser' }, audio: false,
      preferCurrentTab: true, selfBrowserSurface: 'include' });
  }
  const v = document.createElement('video');
  v.muted = true; v.playsInline = true; v.srcObject = tabStream;
  await v.play();
  const old = hideEls.map((e) => e.style.visibility);
  hideEls.forEach((e) => { e.style.visibility = 'hidden'; });
  try {
    await wait(450);                                       // let a few frames without our UI reach the stream
    const c = document.createElement('canvas');
    c.width = v.videoWidth; c.height = v.videoHeight;
    c.getContext('2d').drawImage(v, 0, 0);
    return c;
  } finally {
    hideEls.forEach((e, i) => { e.style.visibility = old[i]; });
    v.srcObject = null;
  }
}

/** Middle 70% of the view, max 1024 px, with the red ring that marks where the user looks. */
function finish(src, sw = src.width, ring = true) {
  const sh = src.height;
  const cw = sw * KEEP, ch = sh * KEEP, cx = (sw - cw) / 2, cy = (sh - ch) / 2;
  const s = Math.min(1, MAX_SIDE / Math.max(cw, ch));
  const out = document.createElement('canvas');
  out.width = Math.round(cw * s); out.height = Math.round(ch * s);
  const x = out.getContext('2d');
  x.fillStyle = '#000'; x.fillRect(0, 0, out.width, out.height);
  x.drawImage(src, cx, cy, cw, ch, 0, 0, out.width, out.height);
  if (!ring) return out.toDataURL('image/jpeg', 0.9);
  const R = Math.round(Math.min(out.width, out.height) * 0.12);
  x.strokeStyle = 'rgba(255,40,40,0.9)'; x.lineWidth = Math.max(2, R / 14);
  x.beginPath(); x.arc(out.width / 2, out.height / 2, R, 0, Math.PI * 2); x.stroke();
  return out.toDataURL('image/jpeg', 0.85);
}

/**
 * Returns { image, how }. hideMeshes = our 3D panels, hideEls = our DOM overlays (only needed for the
 * tab-capture fallback, which films the whole tab).
 */
export async function captureVrView(world, hideMeshes = [], hideEls = [], ring = true) {
  const r = world.renderer, app = r.domElement;
  const was = hideMeshes.map((m) => m.visible);
  hideMeshes.forEach((m) => { m.visible = false; });
  try {
    if (!r.xr.isPresenting) {
      r.render(world.scene, world.camera);                 // read in the same task (buffer still valid)
      return { image: finish(composite([app]), undefined, ring), how: 'scene' };
    }
    await nextFrame(); await nextFrame();                  // a frame rendered without our panels
    const list = layers(app);
    const pic = await new Promise((res) => requestAnimationFrame(() => res(composite(list))));
    const roomLayers = list.length - 1;
    if (blackShare(pic) < 0.85) return { image: finish(pic, undefined, ring), how: roomLayers ? 'room+app' : 'app' };
    // the room was not readable from its canvas (e.g. Immersive Web Emulator extension) -> film the tab
    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) return { image: finish(pic, undefined, ring), how: 'app-only' };
    const tab = await grabTab(hideEls);
    return { image: finish(tab, undefined, ring), how: 'tab' };
  } finally {
    hideMeshes.forEach((m, i) => { m.visible = was[i]; });
  }
}

/** In a passthrough (AR) session the user should see the simulated real world, not the demo studio set. */
const STUDIO = new Set(['Studio Floor', 'Stage', 'Gallery Set', 'Sideboard Set']);
export function hideStudioInPassthrough(world) {
  const set = (vis) => world.scene.traverse((o) => { if (STUDIO.has(o.name)) o.visible = vis; });
  world.renderer.xr.addEventListener('sessionstart', () => {
    const s = world.renderer.xr.getSession();
    if (s && s.environmentBlendMode && s.environmentBlendMode !== 'opaque') set(false);
  });
  world.renderer.xr.addEventListener('sessionend', () => set(true));
}

/** Small aiming ring in the middle of the desktop view (DOM only, not part of the 3D scene). */
export function addAimRing() {
  const d = document.createElement('div');
  d.title = 'Scan aims here';
  d.style.cssText = 'position:fixed;left:50%;top:50%;width:26px;height:26px;margin:-13px 0 0 -13px;border:2px solid rgba(255,60,60,.75);' +
    'border-radius:50%;pointer-events:none;z-index:2147482000;box-shadow:0 0 0 1px rgba(0,0,0,.35)';
  document.body.appendChild(d);
  return d;
}

/** Live video of what the user sees (room + app layers), for recording and for the remote expert. */
export function viewStream(world, fps = 24) {
  const app = world.renderer.domElement;
  const out = document.createElement('canvas');
  const x = out.getContext('2d');
  let on = true;
  const tick = () => {
    if (!on) return;
    const dpr = window.devicePixelRatio || 1;
    const w = Math.round(innerWidth * dpr / 2), h = Math.round(innerHeight * dpr / 2);   // half size is plenty
    if (out.width !== w || out.height !== h) { out.width = w; out.height = h; }
    x.fillStyle = '#000'; x.fillRect(0, 0, w, h);
    for (const c of (world.renderer.xr.isPresenting ? layers(app) : [app])) {
      const r = c.getBoundingClientRect();
      try { x.drawImage(c, r.left * dpr / 2, r.top * dpr / 2, r.width * dpr / 2, r.height * dpr / 2); } catch { /* ignore */ }
    }
    if (document.hidden) setTimeout(tick, 1000 / fps); else requestAnimationFrame(tick);
  };
  tick();
  return { stream: out.captureStream(fps), stop: () => { on = false; } };
}
