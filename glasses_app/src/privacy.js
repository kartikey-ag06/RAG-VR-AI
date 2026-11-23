// privacy.js - the "recording light" of the glasses (like the capture LED on Meta / Snap glasses).
//
// Every camera, microphone and screen capture in the app goes through navigator.mediaDevices, so this module
// wraps getUserMedia / getDisplayMedia once and keeps count of the live tracks. Features that capture without a
// media track (Chrome's speech recogniser, a MediaRecorder recording) report themselves with privacy.set().
// Anyone wearing or watching sees: red REC = recording, red CAM / SCREEN = camera or tab captured, amber MIC = listening.

const live = { camera: new Set(), mic: new Set(), screen: new Set() };
const flags = new Map();                       // name -> 'mic' | 'camera' | 'recording'
const subs = new Set();

export function privacyState() {
  const has = (k) => [...flags.values()].includes(k);
  return { camera: live.camera.size > 0 || has('camera'), mic: live.mic.size > 0 || has('mic'),
    screen: live.screen.size > 0, recording: has('recording'), sources: [...flags.keys()] };
}
function emit() { const s = privacyState(); subs.forEach((f) => { try { f(s); } catch { /* ignore */ } }); }

function watch(stream, screen) {
  for (const t of stream.getTracks()) {
    const bag = screen && t.kind === 'video' ? live.screen : t.kind === 'audio' ? live.mic : live.camera;
    bag.add(t);
    const done = () => { if (bag.delete(t)) emit(); };
    t.addEventListener('ended', done);
    const stop = t.stop.bind(t);
    t.stop = () => { stop(); done(); };          // stop() does not fire 'ended', so catch it here
  }
  emit();
  return stream;
}

const md = navigator.mediaDevices;
if (md && !md.__vrPrivacy) {
  md.__vrPrivacy = true;
  if (md.getUserMedia) { const g = md.getUserMedia.bind(md); md.getUserMedia = async (c) => watch(await g(c), false); }
  if (md.getDisplayMedia) { const g = md.getDisplayMedia.bind(md); md.getDisplayMedia = async (c) => watch(await g(c), true); }
}

export const privacy = {
  /** set('wake-word', 'mic') while listening; set('video', 'recording'); set(name, null) when it stops */
  set(name, kind) { if (kind) flags.set(name, kind); else flags.delete(name); emit(); },
  on(fn) { subs.add(fn); fn(privacyState()); return () => subs.delete(fn); },
  get state() { return privacyState(); },
};
