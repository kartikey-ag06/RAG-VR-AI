// apps.js - "☰ Apps" in VR: open Chrome, WhatsApp, Instagram, Gmail... and see them as floating windows in VR.
//
// How: big sites (WhatsApp Web, Instagram, Gmail) refuse to be shown inside another page, so the app MIRRORS
// a real browser tab instead: Chrome's "share a tab" (getDisplayMedia) gives a live video of that tab,
// which is drawn on a panel in the 3D scene. The window is VIEW-ONLY: you reply / click in the real tab.
import { Mesh, PlaneGeometry, MeshBasicMaterial, VideoTexture, CanvasTexture, SRGBColorSpace, DoubleSide, Group, Vector3 } from '@iwsdk/core';

export const APPS = [
  { id: 'chrome', name: 'Chrome', icon: '🌐', url: '', note: 'Any tab or window you choose' },
  { id: 'whatsapp', name: 'WhatsApp', icon: '💬', url: 'https://web.whatsapp.com/' },
  { id: 'instagram', name: 'Instagram', icon: '📸', url: 'https://www.instagram.com/' },
  { id: 'gmail', name: 'Email', icon: '✉️', url: 'https://mail.google.com/' },
  { id: 'youtube', name: 'YouTube', icon: '▶️', url: 'https://www.youtube.com/' },
  { id: 'procops', name: 'ProcOps', icon: '📊', url: '/procops' },
  { id: 'upload', name: 'Upload', icon: '⬆️', url: '/upload' },
];

const WIDTH_M = 0.95;            // window width in metres
const DIST_M = 1.15;             // distance in front of the user

function titleStrip(text) {
  const c = document.createElement('canvas');
  c.width = 1024; c.height = 64;
  const x = c.getContext('2d');
  x.fillStyle = 'rgba(18,26,43,0.96)'; x.fillRect(0, 0, c.width, c.height);
  x.fillStyle = '#8cc8ff'; x.font = 'bold 30px Segoe UI, Arial'; x.textBaseline = 'middle';
  x.fillText(text, 22, 33);
  x.fillStyle = '#ffcf7a'; x.font = '24px Segoe UI, Arial'; x.textAlign = 'right';
  x.fillText('mirror · view only', c.width - 22, 33);
  const t = new CanvasTexture(c); t.colorSpace = SRGBColorSpace;
  return t;
}

/**
 * opts: { world, onChange(list) }  ->  { open(app), close(id), front(id), list(), meshes() }
 */
export function setupApps({ world, onChange }) {
  const wins = [];               // { id, app, stream, video, group, mesh }
  let seq = 0;

  function cameraPose() {
    const pos = new Vector3(), f = new Vector3();
    world.camera.getWorldPosition(pos); world.camera.getWorldDirection(f);
    f.y = 0; if (f.lengthSq() < 1e-6) f.set(0, 0, -1); f.normalize();
    return { pos, f, yaw: Math.atan2(-f.x, -f.z) };
  }

  // newest window straight ahead, older ones step out to the sides (right, left, right...)
  function layout() {
    const { pos, yaw } = cameraPose();
    const n = wins.length;
    wins.forEach((w, i) => {
      const k = n - 1 - i;                                   // 0 = newest
      const side = k === 0 ? 0 : (k % 2 ? -1 : 1) * Math.ceil(k / 2);
      const a = yaw + side * 0.75;                           // ~43 degrees per step
      const d = k === 0 ? DIST_M : DIST_M + 0.25;
      w.group.position.set(pos.x - Math.sin(a) * d, pos.y + (k === 0 ? 0.05 : 0), pos.z - Math.cos(a) * d);
      w.group.rotation.set(0, a, 0);
    });
  }

  function fit(w) {
    const vw = w.video.videoWidth || 16, vh = w.video.videoHeight || 9;
    const h = WIDTH_M * (vh / vw);
    w.mesh.geometry.dispose();
    w.mesh.geometry = new PlaneGeometry(WIDTH_M, h);
    w.bar.position.set(0, h / 2 + 0.032, 0);
  }

  async function open(app) {
    // Chrome asks which tab/window to share. Pick the tab that has the app open.
    const stream = await navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: app.id === 'chrome' ? 'window' : 'browser', frameRate: { ideal: 15, max: 30 } },
      audio: false, selfBrowserSurface: 'exclude', surfaceSwitching: 'include',
    });
    const video = document.createElement('video');
    video.muted = true; video.playsInline = true; video.srcObject = stream;
    await video.play().catch(() => {});
    const tex = new VideoTexture(video); tex.colorSpace = SRGBColorSpace;
    const mesh = new Mesh(new PlaneGeometry(WIDTH_M, WIDTH_M * 9 / 16), new MeshBasicMaterial({ map: tex, side: DoubleSide, toneMapped: false }));
    const bar = new Mesh(new PlaneGeometry(WIDTH_M, WIDTH_M / 16), new MeshBasicMaterial({ map: titleStrip(`${app.icon}  ${app.name}`), transparent: true, side: DoubleSide, toneMapped: false }));
    const group = new Group(); group.add(mesh); group.add(bar);
    world.createTransformEntity(group);
    const w = { id: ++seq, app, stream, video, group, mesh, bar, tex };
    wins.push(w);
    video.addEventListener('loadedmetadata', () => fit(w));
    video.addEventListener('resize', () => fit(w));
    fit(w);
    stream.getVideoTracks()[0].addEventListener('ended', () => close(w.id));   // "Stop sharing" in Chrome
    layout(); onChange && onChange(list());
    return w.id;
  }

  function close(id) {
    const i = wins.findIndex((w) => w.id === id);
    if (i < 0) return;
    const w = wins[i];
    wins.splice(i, 1);
    w.stream.getTracks().forEach((t) => t.stop());
    w.video.srcObject = null;
    w.group.visible = false;
    w.group.parent && w.group.parent.remove(w.group);
    w.mesh.geometry.dispose(); w.tex.dispose();
    layout(); onChange && onChange(list());
  }

  function front(id) {                                       // bring a window straight ahead
    const i = wins.findIndex((w) => w.id === id);
    if (i < 0) return;
    wins.push(wins.splice(i, 1)[0]);
    layout(); onChange && onChange(list());
  }

  const list = () => wins.map((w) => ({ id: w.id, name: w.app.name, icon: w.app.icon }));
  const meshes = () => wins.map((w) => w.group);
  return { open, close, front, list, meshes, layout };
}
