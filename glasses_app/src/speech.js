// speech.js - always-on listening: "Hey Glasses" wake word + live captions, and reading answers aloud.
// Chrome / Edge: their built-in speech engine (word-by-word captions). Other browsers (Firefox, Safari, some
// headset browsers): the microphone is cut into short clips at the pauses and each clip goes to Groq Whisper
// (pcm.js) - a sentence appears ~1 s after it is spoken. Push-to-talk (hold V) stays in voice.js.
//
// One recognizer serves both: every finished sentence goes to the captions (if on) and is checked for the
// wake word (if on). "Hey Glasses, what is the penalty?" -> asks straight away; "Hey Glasses" alone -> the next
// sentence is the question.

// The wake word is "Hey Glasses" (also "OK / Hi / Hello Glasses"). Speech engines sometimes write it as
// "hey glass", "hey classes", "hey grasses", "hey glasses," ... and in Hindi mode "हे ग्लासेस".
const WAKE = /(?:^|[\s,.!?])(?:hey|hay|hei|hi|hai|hello|ok|okay|o\.k\.)[\s,]+(?:glasses|glass(?:[\s,]+is)?|classes|grasses|glossies|glosses|blasses)(?=$|[\s,.!?])[\s,.!?]*|(?:हे|है|हाय|हेलो|ओके)\s*(?:ग्लासेस|ग्लास|क्लासेस)[\s,.!?।]*/i;
// sound-alike check for the word after the greeting: "glasses" ~ "classes" ~ "grasses" ~ "glass is"
const GREET = /^(hey|hay|hei|hi|hai|hello|ok|okay|o\.k\.)$/;
const skeleton = (w) => w.toLowerCase().replace(/[^a-z]/g, '').replace(/[ck]/g, 'g').replace(/r/g, 'l').replace(/[aeiouy]/g, '').replace(/z/g, 's').replace(/(.)\1+/g, '$1');
function soundsLikeWake(text) {
  const toks = String(text || '').trim().split(/\s+/);
  for (let g = 0; g < Math.min(2, toks.length - 1); g++) {
    if (!GREET.test(toks[g].toLowerCase().replace(/[^a-z.]/g, ''))) continue;
    for (const n of [1, 2]) {                                       // "glasses" (1 word) or "glass is" (2 words)
      const part = toks.slice(g + 1, g + 1 + n);
      if (part.length < n) continue;
      if (n === 2 && part[1].replace(/[^a-z]/gi, '').length > 3) continue;
      if (skeleton(part.join('')) !== 'glss') continue;           // plural sound: "glasses", not "class"
      return { rest: toks.slice(g + 1 + n).join(' ').replace(/^[\s,.!?]+/, '').trim() };
    }
  }
  return null;
}
export function findWake(text) {
  const m = String(text || '').match(WAKE);
  if (m) return { rest: text.slice(m.index + m[0].length).trim() };
  return soundsLikeWake(text);
}
import { createSegmenter, cleanTranscript } from './pcm.js';
import { transcribe } from './rag.js';
import { privacy } from './privacy.js';

const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
// "stop" / "cancel" / Hindi "bas", "ruko" - same words the app uses to stop an answer
const STOP_WORD = /^(please\s+)?(stop|stop it|stop now|stop talking|cancel|enough|that'?s enough|be quiet|quiet|silence|shut up|bas|ruko|ruk jao|band karo)[.!]?$/i;
const canFallback = !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && (window.AudioContext || window.webkitAudioContext));

export function speechSupported() { return !!Rec || canFallback; }
export function nativeSpeech() { return !!Rec; }

/** One listener for the app. Uses the browser's engine when it works; if that engine is missing, blocked or
 *  keeps failing (Brave, no Google speech service, some headsets), it switches to Whisper by itself. */
export function setupListener(handlers, prefer = 'auto') {
  const want = { lang: 'en-IN', wake: false, captions: false, paused: false };
  let impl = null;
  const make = (engine) => {
    const h = { ...handlers, onFatal: engine === 'chrome' ? (why) => switchToWhisper(why) : null };
    const l = engine === 'chrome' ? nativeListener(h) : whisperListener(h);
    l.setLang(want.lang);
    if (want.paused) l.pause();
    l.setCaptions(want.captions); l.setWake(want.wake);
    return l;
  };
  function switchToWhisper(why) {
    if (!canFallback || (impl && impl.engine === 'whisper')) return;
    try { impl.stop(); } catch { /* ignore */ }
    handlers.onState('info', `Browser speech engine unavailable (${why}) - using Whisper for the wake word`);
    impl = make('whisper');
  }
  impl = make(Rec && prefer !== 'whisper' ? 'chrome' : 'whisper');
  return {
    /** 'auto' = browser engine (Whisper if it fails) · 'whisper' = always Whisper */
    useEngine(e) {
      const target = e === 'whisper' || !Rec ? 'whisper' : 'chrome';
      if (impl.engine === target) return;
      try { impl.stop(); } catch { /* ignore */ }
      impl = make(target);
    },
    setLang(l) { want.lang = l; impl.setLang(l); },
    setWake(on) { want.wake = !!on; impl.setWake(on); },
    setCaptions(on) { want.captions = !!on; impl.setCaptions(on); },
    pause() { want.paused = true; impl.pause(); },
    resume() { want.paused = false; impl.resume(); },
    get active() { return impl.active; },
    get engine() { return impl.engine; },
  };
}

/** Same handlers as the Chrome listener, but each spoken sentence is transcribed by Whisper on the server. */
// The app's own voice (answers read aloud) must never be heard as speech: no API call, no self-trigger.
let ttsUntil = 0;
setInterval(() => { if (window.speechSynthesis && speechSynthesis.speaking) ttsUntil = Date.now() + 900; }, 150);
export const appIsSpeaking = () => Date.now() < ttsUntil;

/** Whisper engine. Each clip of speech is one Groq call, so in wake-word-only mode it is careful:
 *  - nothing is sent while the app itself is talking,
 *  - long monologues (> 7 s) are not checked - "Hey Glasses, ..." is short,
 *  - at most 8 checks a minute; beyond that it rests 30 s (busy room, TV) and says so once. */
export const whisperStats = { calls: 0, skipped: 0 };
function whisperListener({ onCommand, onCaption, onState, onHeard }) {
  let lang = 'en-IN', wantWake = false, wantCaptions = false, armedUntil = 0, paused = false, seg = null;
  let recent = [], restUntil = 0, warned = false;
  const heard = (text) => {
    if (onHeard) onHeard(text, 'whisper');
    if (wantCaptions) onCaption(text, true);
    if (!wantWake) return;
    const w = findWake(text);
    if (w) {
      const rest = w.rest;
      if (rest.length > 2) { armedUntil = 0; onCommand(rest); }
      else { armedUntil = Date.now() + 8000; onState('wake', 'Listening... say your question'); }
    } else if (Date.now() < armedUntil) { armedUntil = 0; onCommand(text); }
  };
  async function start() {
    if (seg || paused || !(wantWake || wantCaptions)) return;
    seg = createSegmenter({
      silenceMs: 650, maxMs: wantCaptions ? 8000 : 6000,
      onSegment: async (wav, seconds) => {
        const now = Date.now(), armed = now < armedUntil;
        if (appIsSpeaking()) { whisperStats.skipped++; return; }
        if (!wantCaptions && !armed) {                     // wake word only: check sparingly
          if (seconds > 7 || now < restUntil) { whisperStats.skipped++; return; }
          recent = recent.filter((t) => now - t < 60000);
          if (recent.length >= 8) {
            restUntil = now + 30000; whisperStats.skipped++;
            if (!warned) { warned = true; onState('info', 'Lots of talking nearby - wake-word checks paused 30 s to save API calls (Auto engine has no such limit)'); }
            return;
          }
          recent.push(now);
        }
        whisperStats.calls++;
        if (wantCaptions) onCaption('…', false);
        try { const t = cleanTranscript(await transcribe(wav, lang, wantWake ? 'Hey Glasses, ' : '')); if (t) heard(t); }
        catch (e) { if (/429|rate/i.test(String(e))) onState('error', 'Voice service busy - try again in a moment'); }
      },
    });
    try { await seg.start(); privacy.set('listener', 'mic'); onState('on', wantWake ? 'Say "Hey Glasses" ... (Whisper)' : 'Captions on (Whisper)'); }
    catch { seg = null; wantWake = wantCaptions = false; onState('error', 'Microphone blocked for always-on listening'); }
  }
  function stop() { if (seg) seg.stop(); seg = null; privacy.set('listener', null); }
  return {
    stop,
    setLang(l) { lang = l; },
    setWake(on) { wantWake = !!on; if (!wantWake && !wantCaptions) stop(); else start(); },
    setCaptions(on) { wantCaptions = !!on; if (!wantWake && !wantCaptions) stop(); else start(); },
    pause() { paused = true; stop(); },
    resume() { paused = false; start(); },
    get active() { return !!seg; },
    engine: 'whisper',
  };
}

function nativeListener({ onCommand, onCaption, onState, onHeard, onFatal }) {
  let rec = null, lang = 'en-IN', wantWake = false, wantCaptions = false, armedUntil = 0, running = false, paused = false;
  let startedAt = 0, quickEnds = 0, heardAny = false;
  // End-of-question detection: Chrome can take many seconds to "finalise" a sentence when there is
  // background noise. If the words stop changing for END_MS after the wake word, we treat the question as done.
  const END_MS = 1200;
  let pending = '', endTimer = null;
  const clearPending = () => { clearTimeout(endTimer); endTimer = null; pending = ''; };
  function commit() {
    const q = pending.trim(); clearPending();
    if (q.length < 3) return;
    armedUntil = 0;
    restart(true);                     // drop Chrome's half-finished sentence so it is not sent a second time
    onCommand(q);
  }
  function waitForEnd(text) {
    if (text === pending) return;
    pending = text; clearTimeout(endTimer); endTimer = setTimeout(commit, STOP_WORD.test(text) ? 250 : END_MS);   // "stop" acts at once
  }

  function start() {
    if (!Rec || running || paused || !(wantWake || wantCaptions)) return;
    rec = new Rec();
    rec.lang = lang; rec.continuous = true; rec.interimResults = true;
    rec.onresult = (e) => {
      let interim = '';
      if (appIsSpeaking() && !wantCaptions) {                   // our own spoken answer, not the user...
        if (!wantWake) return;                                   // ...except "Hey Glasses, stop"
        let all = '';
        for (let i = e.resultIndex; i < e.results.length; i++) all += e.results[i][0].transcript + ' ';
        const w = findWake(all.trim());
        if (w && STOP_WORD.test(w.rest)) { clearPending(); armedUntil = 0; restart(true); onCommand('stop'); }
        return;
      }
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const r = e.results[i], text = r[0].transcript.trim();
        heardAny = true;
        if (!r.isFinal) { interim += text + ' '; continue; }
        if (onHeard) onHeard(text, 'chrome');
        if (wantCaptions) onCaption(text, true);
        if (!wantWake || !text) continue;
        clearPending();                                          // Chrome finished the sentence itself
        const w = findWake(text);
        if (w) {
          const rest = w.rest;
          if (rest.length > 2) { armedUntil = 0; onCommand(rest); }
          else { armedUntil = Date.now() + 8000; onState('wake', 'Listening... say your question'); }
        } else if (Date.now() < armedUntil) { armedUntil = 0; onCommand(text); }
      }
      if (interim && wantCaptions) onCaption(interim.trim(), false);
      // react while you are still talking: "Hey Glasses" shows "Listening..." at once, the question follows in the final text
      if (interim && wantWake) {
        const it = interim.trim(), w = findWake(it);
        if (w && w.rest.length > 2) waitForEnd(w.rest);                     // "Hey Glasses, what is ..." in one go
        else if (w) { if (Date.now() > armedUntil) { armedUntil = Date.now() + 8000; onState('wake', 'Listening... say your question'); } }
        else if (Date.now() < armedUntil) waitForEnd(it);                    // question after a short pause
      }
    };
    rec.onerror = (e) => {
      if (e.error === 'not-allowed') { wantWake = wantCaptions = false; onState('error', 'Microphone blocked for always-on listening - allow it in the address bar'); return; }
      // the engine exists but cannot work here (Brave, offline, blocked Google speech service, unsupported language)
      if (['network', 'service-not-allowed', 'language-not-supported', 'audio-capture'].includes(e.error) && onFatal) { stop(); onFatal(e.error); }
    };
    rec.onend = () => {                                      // Chrome stops after a while: restart
      running = false;
      if (paused || !(wantWake || wantCaptions)) return;
      quickEnds = Date.now() - startedAt < 1500 && !heardAny ? quickEnds + 1 : 0;
      if (quickEnds >= 5 && onFatal) { onFatal('it keeps stopping'); return; }     // never loop silently
      setTimeout(start, quickEnds ? 300 * quickEnds : 300);
    };
    heardAny = false; startedAt = Date.now();
    try { rec.start(); running = true; privacy.set('listener', 'mic'); onState('on', wantWake ? 'Say "Hey Glasses" ...' : 'Captions on'); } catch { running = false; }
  }
  function stop(discard) {
    clearPending();
    if (rec) { try { rec.onend = null; if (discard) { rec.onresult = null; rec.abort(); } else rec.stop(); } catch { /* ignore */ } }
    running = false; rec = null; privacy.set('listener', null);
  }
  function restart(discard) { stop(discard); start(); }

  return {
    stop,
    setLang(l) { if (l !== lang) { lang = l; if (running) restart(); } },
    setWake(on) { wantWake = !!on; if (!wantWake && !wantCaptions) stop(); else start(); },
    setCaptions(on) { wantCaptions = !!on; if (!wantWake && !wantCaptions) stop(); else start(); },
    pause() { paused = true; stop(); },          // while push-to-talk records (one mic user at a time)
    resume() { paused = false; start(); },
    get active() { return running; },
    engine: 'chrome',
  };
}

// ---------- read aloud ----------
let voices = [];
if (window.speechSynthesis) {
  const load = () => { voices = speechSynthesis.getVoices(); };
  load(); speechSynthesis.onvoiceschanged = load;
}
export function speak(text, langCode = 'en-IN') {
  if (!window.speechSynthesis || !text) return;
  speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(String(text).replace(/\[\d+(,\s*\d+)*\]/g, '').slice(0, 600));
  u.lang = langCode;
  const base = langCode.split('-')[0];
  u.voice = voices.find((v) => v.lang === langCode) || voices.find((v) => v.lang.startsWith(base)) || null;
  u.rate = 1.03;
  speechSynthesis.speak(u);
}
/** Adds a piece to the speech queue without cutting off what is being said (used while an answer streams in). */
export function speakChunk(text, langCode = 'en-IN') {
  if (!window.speechSynthesis) return;
  const t = String(text || '').replace(/\[\d+(,\s*\d+)*\]/g, '').replace(/[•*#]/g, ' ').trim();
  if (!t) return;
  const u = new SpeechSynthesisUtterance(t.slice(0, 400));
  u.lang = langCode;
  const base = langCode.split('-')[0];
  u.voice = voices.find((v) => v.lang === langCode) || voices.find((v) => v.lang.startsWith(base)) || null;
  u.rate = 1.03;
  speechSynthesis.speak(u);
}
export function stopSpeaking() { if (window.speechSynthesis) speechSynthesis.cancel(); }

/** Settings -> "Test wake word": checks permission, microphone level and the browser speech engine (≈8 s). */
export async function diagnoseMic(lang = 'en-IN', report = () => {}) {
  const out = { permission: 'unknown', level: 0, engine: Rec ? 'present' : 'missing', events: [], heard: '' };
  try { out.permission = (await navigator.permissions.query({ name: 'microphone' })).state; } catch { /* not supported */ }
  report('Checking the microphone…');
  try {
    const st = await navigator.mediaDevices.getUserMedia({ audio: true });
    const ac = new (window.AudioContext || window.webkitAudioContext)(), an = ac.createAnalyser();
    ac.createMediaStreamSource(st).connect(an);
    const buf = new Float32Array(an.fftSize), t0 = Date.now();
    report('Say something now…');
    while (Date.now() - t0 < 2500) {
      an.getFloatTimeDomainData(buf);
      out.level = Math.max(out.level, Math.sqrt(buf.reduce((a, x) => a + x * x, 0) / buf.length));
      await new Promise((r) => setTimeout(r, 60));
    }
    st.getTracks().forEach((t) => t.stop()); ac.close();
    out.permission = 'granted';
  } catch (e) { out.permission = e.name === 'NotAllowedError' ? 'denied' : 'error: ' + e.name; return out; }
  if (!Rec) return out;
  report('Testing the browser speech engine - say “Hey Glasses, hello”…');
  await new Promise((done) => {
    const r = new Rec(); r.lang = lang; r.continuous = true; r.interimResults = true;
    const end = setTimeout(() => { try { r.stop(); } catch { /* ignore */ } }, 5500);
    ['start', 'audiostart', 'speechstart', 'speechend', 'end'].forEach((k) => r.addEventListener(k, () => { out.events.push(k); if (k === 'end') { clearTimeout(end); done(); } }));
    r.onerror = (e) => out.events.push('error:' + e.error);
    r.onresult = (e) => { out.heard = [...e.results].map((x) => x[0].transcript).join(' '); };
    try { r.start(); } catch (e) { out.events.push('throw:' + e.message); clearTimeout(end); done(); }
    setTimeout(done, 9000);
  });
  return out;
}

export function explainDiagnosis(d) {
  if (d.permission === 'denied') return '❌ Microphone blocked. Click the lock/tune icon left of the address bar → Microphone → Allow, then reload.';
  if (String(d.permission).startsWith('error')) return '❌ No microphone found (' + d.permission + '). Check Windows sound settings → Input.';
  if (d.level < 0.004) return '⚠ The microphone is silent (level ' + d.level.toFixed(3) + '). Pick the right mic in Windows / Chrome settings, or unmute it.';
  if (d.engine === 'missing') return '✅ Microphone works. This browser has no speech engine: the wake word uses Whisper.';
  const err = d.events.find((e) => e.startsWith('error:') || e.startsWith('throw:'));
  if (err) return `❌ Chrome's speech engine failed (${err.replace(/^\w+:/, '')}). Set “Wake word engine” to Whisper - it uses the same mic as push-to-talk.`;
  if (!d.events.includes('audiostart')) return '❌ Chrome\'s speech engine never opened the microphone. Set “Wake word engine” to Whisper.';
  if (!d.heard) return `⚠ Chrome's engine listened but understood nothing (mic level ${d.level.toFixed(3)}). Speak closer, or set “Wake word engine” to Whisper.`;
  return `✅ All good. Chrome heard: “${d.heard.slice(0, 60)}”${findWake(d.heard) ? ' - wake word recognised.' : ' - say “Hey Glasses” clearly at the start.'}`;
}
