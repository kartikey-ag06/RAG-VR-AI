// extras.js - teleprompter, meeting notes, hand-gesture control and turn-by-turn directions for the glasses.
//
//  Teleprompter   your script on the lens; in Chrome / Edge it follows your voice (the line you are saying is
//                 highlighted), elsewhere it scrolls at your speaking speed. Scripts: pasted, saved, or written
//                 by the AI from your documents ("teleprompter for the pump safety briefing").
//  Meeting notes  every sentence is transcribed (Groq Whisper); at the end the AI writes a summary, decisions
//                 and action items, saved on the Workspace page (export as Markdown / Word / PDF).
//  Hand gestures  the webcam watches your hand (MediaPipe): pinch = talk, swipe = next / back, open palm =
//                 pause / stop talking, thumbs up = scan what is in front of you.
//  Directions     walking / cycling / driving routes from OpenStreetMap; the next turn shows on the lens and
//                 advances by itself as your GPS position moves (or say "next").
import { api, transcribe } from './rag.js';
import { createSegmenter, cleanTranscript } from './pcm.js';

import { nativeSpeech } from './speech.js';
import { S } from './settings.js';

const MP_WASM = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.2.3/wasm';
const HAND_MODEL = 'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task';
const norm = (w) => w.toLowerCase().replace(/[^\p{L}\p{N}]+/gu, '');

/** Script -> short lines that fit the lens (about 7 words each, split at sentence ends). */
export function scriptLines(text) {
  const out = [];
  for (const para of String(text || '').split(/\n+/)) {
    for (const sent of para.match(/[^.!?।]+[.!?।]*/g) || []) {
      const words = sent.trim().split(/\s+/).filter(Boolean);
      for (let i = 0; i < words.length; i += 7) {
        const chunk = words.slice(i, i + 7);
        if (words.length - i - 7 > 0 && words.length - i - 7 < 3) { chunk.push(...words.slice(i + 7)); i = words.length; }
        out.push(chunk.join(' '));
      }
    }
  }
  return out.filter(Boolean);
}

export function setupExtras(ctx) {
  const { hud, say, reply, act, listener, session } = ctx;

  // ================================================================== teleprompter
  const tp = { on: false, lines: [], words: [], cur: 0, within: 0, paused: false, follow: false, wpm: 140, rec: null, timer: null, title: '' };
  function tpDraw() { hud.setPrompter(tp.on ? { lines: tp.lines, cur: tp.cur, within: tp.within, paused: tp.paused, follow: tp.follow, wpm: tp.wpm } : null); }
  function tpStart(text, title = 'Script') {
    tp.lines = scriptLines(text);
    if (!tp.lines.length) { hud.toast('The script is empty', 'warn'); return; }
    tp.words = tp.lines.map((l) => l.split(/\s+/).map(norm));
    Object.assign(tp, { on: true, cur: 0, within: 0, paused: false, title, wpm: Math.max(80, Math.min(220, +S.tpWpm || 140)) });
    listener.pause();                                          // one microphone user at a time
    tp.follow = nativeSpeech() && S.tpFollow !== false;
    if (tp.follow) tpFollow(); else tpAuto();
    session.log('teleprompter', { text: `Teleprompter started: ${title} (${tp.lines.length} lines)` });
    hud.toast(`🎤 Teleprompter: ${title} — ${tp.follow ? 'start speaking, it follows you' : 'scrolling at ' + tp.wpm + ' words/min'}`, 'info', 4000);
    tpDraw();
  }
  function tpStop() {
    if (!tp.on) return;
    tp.on = false; clearInterval(tp.timer);
    if (tp.rec) { try { tp.rec.onend = null; tp.rec.stop(); } catch { /* ignore */ } tp.rec = null; }
    ctx.privacy.set('teleprompter', null);
    tpDraw(); listener.resume();
    hud.toast('🎤 Teleprompter closed', 'info');
  }
  function tpAuto() {                                          // no speech engine: scroll at speaking speed
    clearInterval(tp.timer);
    tp.timer = setInterval(() => {
      if (!tp.on || tp.paused) return;
      const n = Math.max(1, tp.words[tp.cur].length);
      tp.within += (tp.wpm / 60) * 0.2 / n;
      if (tp.within >= 1) { tp.within = 0; if (tp.cur < tp.lines.length - 1) tp.cur++; else tp.paused = true; }
      tpDraw();
    }, 200);
  }
  function tpFollow() {                                        // Chrome: match the words you say to the script
    const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
    const rec = new Rec(); tp.rec = rec;
    rec.lang = S.voiceLang || 'en-IN'; rec.continuous = true; rec.interimResults = true;
    rec.onresult = (e) => {
      if (tp.paused) return;
      let heard = '';
      for (let i = e.resultIndex; i < e.results.length; i++) heard += ' ' + e.results[i][0].transcript;
      const said = heard.split(/\s+/).map(norm).filter(Boolean).slice(-6);
      if (!said.length) return;
      // search this line and the next two for the last words spoken
      let best = null;
      for (let li = tp.cur; li < Math.min(tp.lines.length, tp.cur + 3); li++) {
        const ws = tp.words[li];
        for (let wi = 0; wi < ws.length; wi++) {
          let hit = 0;
          for (let k = 0; k < said.length; k++) { const w = ws[wi - (said.length - 1 - k)]; if (w && w === said[k]) hit++; }
          const score = hit - (li - tp.cur) * 0.3;
          if (hit >= Math.min(2, said.length) && (!best || score > best.score)) best = { li, wi, score };
        }
      }
      if (!best) return;
      const ws = tp.words[best.li];
      if (best.wi >= ws.length - 1 && best.li < tp.lines.length - 1) { tp.cur = best.li + 1; tp.within = 0; }
      else { tp.cur = best.li; tp.within = (best.wi + 1) / ws.length; }
      tpDraw();
    };
    rec.onend = () => { if (tp.on && tp.rec === rec) setTimeout(() => { try { rec.start(); } catch { /* ignore */ } }, 250); };
    rec.onerror = (e) => { if (/not-allowed/.test(e.error)) { tp.follow = false; tpAuto(); tpDraw(); } };
    try { rec.start(); ctx.privacy.set('teleprompter', 'mic'); } catch { tp.follow = false; tpAuto(); }
  }
  const teleprompter = {
    get on() { return tp.on; },
    start: tpStart, stop: tpStop,
    pause(p = !tp.paused) { tp.paused = p; tpDraw(); },
    next() { if (tp.cur < tp.lines.length - 1) { tp.cur++; tp.within = 0; tpDraw(); } },
    prev() { if (tp.cur > 0) { tp.cur--; tp.within = 0; tpDraw(); } },
    faster(d) { tp.wpm = Math.max(80, Math.min(220, tp.wpm + d)); S.tpWpm = tp.wpm; tpDraw(); },
    async fromTopic(topic, minutes = 1.5) {
      hud.toast(`✍ Writing a ${minutes}-minute script on "${topic}" from your documents…`, 'info', 5000);
      const sc = await api('/api/teleprompter/script', { topic, minutes });
      tpStart(sc.text, sc.title);
      return sc;
    },
    async openLast() {
      const { scripts } = await api('/api/teleprompter/scripts');
      if (!scripts.length) { reply('🎤 No saved scripts yet. Say "teleprompter for <topic>" or paste one in 🛠 Tools → Teleprompter.', 'No saved scripts yet.'); return; }
      tpStart(scripts[0].text, scripts[0].title);
    },
  };

  // ================================================================== meeting notes
  const mt = { on: false, id: null, seg: null, n: 0, t0: 0, pending: 0 };
  async function meetingStart(title = 'Meeting') {
    if (mt.on) { hud.toast('Meeting notes are already running', 'info'); return; }
    const m = await api('/api/meetings', { title, lang: S.voiceLang });
    Object.assign(mt, { on: true, id: m.id, n: 0, t0: Date.now(), pending: 0 });
    if (listener.engine === 'whisper') listener.pause();         // the Whisper listener would hear everything twice
    mt.seg = createSegmenter({
      silenceMs: 900, maxMs: 20000, minSpeechMs: 500,
      onSegment: async (wav, sec, at) => {
        mt.pending++;
        try {
          const text = cleanTranscript(await transcribe(wav, S.voiceLang));
          if (text && mt.id) {
            await api(`/api/meetings/${mt.id}/segment`, { text, t: (at - mt.t0) / 1000 });
            mt.n++; hud.setCaption(text, '', true);
            ctx.onMeetingText && ctx.onMeetingText(text);
          }
        } catch (e) { if (/429|rate/i.test(String(e))) hud.toast('Voice service busy - a few seconds were not transcribed', 'warn', 4000); }
        mt.pending--;
      },
    });
    try { await mt.seg.start(); }
    catch { mt.on = false; hud.toast('Microphone blocked - meeting notes need it', 'bad', 6000); return; }
    session.log('meeting', { text: 'Meeting notes started', meeting: m.id });
    reply('📝 Meeting notes are on. Everything said is transcribed.\nSay "stop meeting notes" to get the summary and action items.', 'Meeting notes started.');
  }
  async function meetingStop() {
    if (!mt.on) { hud.toast('No meeting is being recorded', 'info'); return; }
    mt.on = false; mt.seg.flush(); mt.seg.stop();
    hud.toast('📝 Writing the summary…', 'info', 5000);
    for (let i = 0; i < 60 && mt.pending > 0; i++) await new Promise((r) => setTimeout(r, 250));   // last clips
    const m = await api(`/api/meetings/${mt.id}/stop`, {});
    hud.setCaption(null);
    if (listener.engine === 'whisper') listener.resume();
    const s = m.summary || {};
    const acts = (s.action_items || []).map((a) => `☐ ${a.task}${a.owner ? ' — ' + a.owner : ''}${a.due ? ' (due ' + a.due + ')' : ''}`);
    reply(`📝 ${m.title}\n\n${s.summary || ''}` + (s.decisions && s.decisions.length ? `\n\nDecisions:\n• ${s.decisions.join('\n• ')}` : '') +
      (acts.length ? `\n\nAction items:\n${acts.join('\n')}\n\nSay "add the action items to my reminders".` : '') + '\n\nFull notes: Workspace → Meetings.',
      `Meeting summary ready. ${s.summary || ''} ${acts.length ? acts.length + ' action items.' : ''}`);
    session.log('meeting', { text: `Meeting notes: ${m.title}`, meeting: m.id, answer: (s.summary || '').slice(0, 400) });
    mt.last = m.id;
    return m;
  }
  async function actionsToReminders() {
    const id = mt.last || ((await api('/api/meetings')).meetings[0] || {}).id;
    if (!id) { reply('No meeting notes yet.', 'No meeting notes yet.'); return; }
    const r = await api(`/api/meetings/${id}/actions-to-reminders?tz=${-new Date().getTimezoneOffset()}`, {});
    reply(`⏰ Added ${r.reminders} action item${r.reminders === 1 ? '' : 's'} to your reminders.`, `Added ${r.reminders} reminders.`);
  }
  const meeting = { start: meetingStart, stop: meetingStop, actionsToReminders, get on() { return mt.on; } };

  // ================================================================== hand gestures
  const gs = { on: false, video: null, lm: null, raf: 0, last: 0, hist: [], cool: 0, palmSince: 0, thumbSince: 0, pinched: false, bubble: null };
  async function gesturesStart() {
    if (gs.on) return;
    hud.toast('✋ Loading hand tracking…', 'info', 3000);
    try {
      const m = await import('@mediapipe/tasks-vision');
      const files = await m.FilesetResolver.forVisionTasks(MP_WASM);
      gs.lm = await m.HandLandmarker.createFromOptions(files, { baseOptions: { modelAssetPath: HAND_MODEL, delegate: 'GPU' }, runningMode: 'VIDEO', numHands: 1 });
      const stream = await navigator.mediaDevices.getUserMedia({ video: { width: 320, height: 240, facingMode: 'user' }, audio: false });
      gs.video = Object.assign(document.createElement('video'), { muted: true, playsInline: true, srcObject: stream });
      await gs.video.play();
    } catch (e) {
      hud.toast('✋ Hand tracking could not start: ' + (e.name === 'NotAllowedError' ? 'camera blocked' : e.message), 'bad', 7000);
      return;
    }
    gs.bubble = Object.assign(document.createElement('canvas'), { width: 160, height: 120, title: 'Hand gestures: pinch = talk · swipe = next/back · palm = pause · 👍 = scan' });
    gs.bubble.style.cssText = 'position:fixed;left:16px;bottom:16px;width:160px;height:120px;border-radius:14px;border:2px solid #4fb3ff;z-index:2147483002;background:#000;transform:scaleX(-1)';
    document.body.appendChild(gs.bubble);
    gs.on = true; S.gestures = true;
    hud.toast('✋ Gestures on: pinch = talk · swipe = next / back · open palm = pause · 👍 = scan', 'ok', 7000);
    loop();
  }
  function gesturesStop() {
    gs.on = false; S.gestures = false; cancelAnimationFrame(gs.raf);
    if (gs.video) { gs.video.srcObject.getTracks().forEach((t) => t.stop()); gs.video = null; }
    if (gs.bubble) { gs.bubble.remove(); gs.bubble = null; }
    hud.toast('✋ Gestures off', 'info');
  }
  const fire = (name, fn) => { if (Date.now() < gs.cool) return; gs.cool = Date.now() + 900; hud.toast('✋ ' + name, 'info', 1500); fn(); };
  function classify(p) {
    const d = (a, b) => Math.hypot(p[a].x - p[b].x, p[a].y - p[b].y);
    const size = d(0, 9) || 0.1;
    const ext = (tip, pip) => d(tip, 0) > d(pip, 0) * 1.12;                  // finger straight out
    const fingers = [ext(8, 6), ext(12, 10), ext(16, 14), ext(20, 18)];
    const pinch = d(4, 8) / size < 0.28;
    const palm = fingers.every(Boolean) && d(4, 5) / size > 0.55;
    const thumbUp = !fingers.some(Boolean) && p[4].y < p[3].y && p[3].y < p[2].y && (p[2].y - p[4].y) / size > 0.45;
    return { pinch, palm, thumbUp, x: p[0].x, y: p[0].y };
  }
  function loop() {
    if (!gs.on) return;
    gs.raf = requestAnimationFrame(loop);
    const now = performance.now();
    if (now - gs.last < 66 || !gs.video || gs.video.readyState < 2) return;      // ~15 checks a second
    gs.last = now;
    let r;
    try { r = gs.lm.detectForVideo(gs.video, now); } catch { return; }
    const bc = gs.bubble.getContext('2d');
    bc.drawImage(gs.video, 0, 0, 160, 120);
    const pts = r.landmarks && r.landmarks[0];
    if (!pts) { gs.hist = []; gs.palmSince = gs.thumbSince = 0; gs.pinched = false; return; }
    bc.fillStyle = '#4fb3ff'; pts.forEach((q) => bc.fillRect(q.x * 160 - 2, q.y * 120 - 2, 4, 4));
    const g = classify(pts), t = Date.now();
    gs.hist.push({ t, x: g.x }); gs.hist = gs.hist.filter((h) => t - h.t < 450);
    const dx = gs.hist.length > 3 ? gs.hist[gs.hist.length - 1].x - gs.hist[0].x : 0;
    // the picture is mirrored: moving the hand to YOUR right lowers x
    if (Math.abs(dx) > 0.22 && !g.pinch) { gs.hist = []; fire(dx < 0 ? 'Swipe → next' : 'Swipe ← back', () => act(dx < 0 ? 'next' : 'prev')); return; }
    if (g.pinch && !gs.pinched) { gs.pinched = true; fire('Pinch → talk', () => ctx.micToggle()); }
    if (!g.pinch) gs.pinched = false;
    gs.palmSince = g.palm ? gs.palmSince || t : 0;
    if (gs.palmSince && t - gs.palmSince > 700) { gs.palmSince = 0; fire('Open palm → pause', () => act('pause')); }
    gs.thumbSince = g.thumbUp ? gs.thumbSince || t : 0;
    if (gs.thumbSince && t - gs.thumbSince > 600) { gs.thumbSince = 0; fire('Thumbs up → scan', () => act('scan')); }
  }
  const gestures = { start: gesturesStart, stop: gesturesStop, get on() { return gs.on; }, classify };

  // ================================================================== directions
  const nv = { on: false, route: null, i: 0, watch: null, pos: null };
  const fmt = (m) => (m < 950 ? `${Math.round(m / 10) * 10} m` : `${(m / 1000).toFixed(1)} km`);
  const hav = (a, b) => { const R = 6371000, r = Math.PI / 180, dp = (b[0] - a[0]) * r, dl = (b[1] - a[1]) * r;
    const h = Math.sin(dp / 2) ** 2 + Math.cos(a[0] * r) * Math.cos(b[0] * r) * Math.sin(dl / 2) ** 2; return 2 * R * Math.asin(Math.sqrt(h)); };
  const where = () => new Promise((res) => (navigator.geolocation
    ? navigator.geolocation.getCurrentPosition((p) => res(p), () => res(null), { enableHighAccuracy: true, timeout: 8000, maximumAge: 30000 }) : res(null)));
  function navShow() {
    if (!nv.on) { hud.setNav(null); return; }
    const st = nv.route.steps[nv.i], nx = nv.route.steps[nv.i + 1];
    const dist = nv.pos && st.lat != null && nv.i > 0 ? hav(nv.pos, [st.lat, st.lon]) : st.distance_m;
    const left = nv.route.steps.slice(nv.i).reduce((a, s) => a + s.distance_m, 0);
    hud.setNav({ arrow: st.arrow, dist: nv.i === 0 ? fmt(st.distance_m) : 'in ' + fmt(dist), text: st.text,
      eta: `${fmt(left)} · ${Math.max(1, Math.round((nv.route.duration_s * left) / Math.max(1, nv.route.distance_m) / 60))} min`, next: nx ? nx.short : '' });
  }
  function navStep(i) {
    nv.i = Math.max(0, Math.min(nv.route.steps.length - 1, i));
    const st = nv.route.steps[nv.i];
    navShow(); say(st.text);
    if (st.arrow === '⚑') setTimeout(() => { if (nv.on && nv.i === nv.route.steps.length - 1) navStop(true); }, 8000);
  }
  async function navStart(to, mode = 'walk', from = '') {
    hud.toast(`🧭 Finding a ${mode} route to ${to}…`, 'info', 4000);
    let q = `to=${encodeURIComponent(to)}&mode=${mode}`;
    if (from) q += `&frm=${encodeURIComponent(from)}`;
    else {
      const p = await where();
      if (!p) { reply('🧭 I need your location: allow location for this site, or say "directions from <place> to <place>".', 'I need your location first.'); return; }
      nv.pos = [p.coords.latitude, p.coords.longitude];
      q += `&lat=${nv.pos[0]}&lon=${nv.pos[1]}`;
    }
    let r;
    try { r = await api('/api/directions?' + q); } catch (e) { reply('🧭 ' + e.message, e.message); return; }
    if (r.error) { reply('🧭 ' + r.error, r.error); return; }
    Object.assign(nv, { on: true, route: r, i: 0 });
    const mins = Math.max(1, Math.round(r.duration_s / 60));
    reply(`🧭 ${({ walk: 'Walking', bike: 'Cycling', drive: 'Driving' })[r.mode]} to ${r.to.name}: ${fmt(r.distance_m)}, about ${mins} min\n\n` +
      r.steps.map((s) => `${s.arrow}  ${s.short}`).join('\n') + '\n\nSay "next" for the next turn, "stop navigation" to end.',
    `${fmt(r.distance_m)}, about ${mins} minutes. ${r.steps[0].text}.`);
    session.log('directions', { text: `Directions to ${r.to.name} (${r.mode}, ${fmt(r.distance_m)})` });
    navShow();
    if (navigator.geolocation && !from) {                      // move on by itself as you walk
      nv.watch = navigator.geolocation.watchPosition((p) => {
        nv.pos = [p.coords.latitude, p.coords.longitude];
        const nx = nv.route.steps[nv.i + 1];
        if (nx && nx.lat != null && hav(nv.pos, [nx.lat, nx.lon]) < Math.max(15, (p.coords.accuracy || 10) * 0.8)) navStep(nv.i + 1);
        else navShow();
      }, () => {}, { enableHighAccuracy: true, maximumAge: 3000 });
    }
  }
  function navStop(arrived = false) {
    if (!nv.on) return;
    nv.on = false; if (nv.watch != null) navigator.geolocation.clearWatch(nv.watch); nv.watch = null;
    hud.setNav(null); hud.toast(arrived ? '⚑ You have arrived' : '🧭 Navigation ended', arrived ? 'ok' : 'info');
  }
  const nav = { start: navStart, stop: navStop, get on() { return nv.on; },
    next() { if (!nv.on) return false; navStep(nv.i + 1); return true; },
    prev() { if (!nv.on) return false; navStep(nv.i - 1); return true; },
    repeat() { if (!nv.on) return false; say(nv.route.steps[nv.i].text); return true; } };

  // ================================================================== voice commands for all of the above
  const MODE = { walk: 'walk', walking: 'walk', foot: 'walk', cycle: 'bike', cycling: 'bike', bike: 'bike', ride: 'bike', drive: 'drive', driving: 'drive', car: 'drive' };
  async function route(t) {
    let m;
    if ((m = t.match(/^(?:(?:give me |get |show )?directions?|navigate|navigation|route|take me|guide me|how (?:do|can) i get)(?: from (.+?))? to (.+?)(?: by (walk|walking|foot|bike|cycle|cycling|car|drive|driving))?[.!?]?$/i))) {
      await navStart(m[2], MODE[(m[3] || '').toLowerCase()] || 'walk', m[1] || ''); return true;
    }
    if ((m = t.match(/^(walk|cycle|ride|drive) (?:me )?to (.+?)[.!?]?$/i))) { await navStart(m[2], MODE[m[1].toLowerCase()], ''); return true; }
    if (/^(stop|end|cancel|exit) (the )?(navigation|directions|route)[.!]?$/i.test(t)) { navStop(); return true; }
    if ((m = t.match(/^(?:start |open |show )?(?:the )?teleprompter(?: (?:for|on|about) (.+?))?[.!?]?$/i))) {
      if (m[1]) await teleprompter.fromTopic(m[1]); else await teleprompter.openLast(); return true;
    }
    if ((m = t.match(/^write (?:me )?(?:a )?(?:(\d+(?:\.\d+)?)[- ]minute )?(?:speech|talk|script|briefing|presentation) (?:for|on|about) (.+?)[.!?]?$/i))) {
      await teleprompter.fromTopic(m[2], m[1] ? parseFloat(m[1]) : 1.5); return true;
    }
    if (/^(stop|close|end|exit) (the )?teleprompter[.!]?$/i.test(t)) { tpStop(); return true; }
    if (tp.on && /^(pause|resume|continue)[.!]?$/i.test(t)) { teleprompter.pause(/pause/i.test(t)); return true; }
    if (tp.on && /^(faster|speed up)[.!]?$/i.test(t)) { teleprompter.faster(20); return true; }
    if (tp.on && /^(slower|slow down)[.!]?$/i.test(t)) { teleprompter.faster(-20); return true; }
    if (/^(start|begin|record|take) (the )?(meeting|meeting notes|notes of (the|this) meeting|minutes)[.!]?$|^(start )?meeting (notes|mode)( on)?[.!]?$/i.test(t)) { await meetingStart(); return true; }
    if (/^(stop|end|finish) (the )?(meeting|meeting notes|minutes|recording the meeting)[.!]?$|^summari[sz]e (the |this )?meeting[.!]?$/i.test(t)) { await meetingStop(); return true; }
    if (/^(add|put|save) (the )?action items (to|in|as) (my )?reminders?[.!]?$/i.test(t)) { await actionsToReminders(); return true; }
    if (/^(start|turn on|enable) (hand )?gestures?[.!]?$|^gestures? on[.!]?$/i.test(t)) { await gesturesStart(); return true; }
    if (/^(stop|turn off|disable) (hand )?gestures?[.!]?$|^gestures? off[.!]?$/i.test(t)) { gesturesStop(); return true; }
    return false;
  }

  return { teleprompter, meeting, gestures, nav, route };
}
