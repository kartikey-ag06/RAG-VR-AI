// voice.js - push-to-talk voice input.
//   Primary : record with MediaRecorder -> /api/stt -> Groq Whisper (accurate, works with any accent)
//   Fallback: Chrome's built-in Web Speech API (used when the server has no Groq key)
// Controls: click the mic button (click again to stop), or HOLD the V key while speaking.
import { transcribe } from './rag.js';

const MAX_SECONDS = 15;
const END_SILENCE_MS = 1300;   // tap-to-ask: stop by itself after this much quiet once you have spoken
const NO_SPEECH_MS = 7000;     // ...or if nothing is said at all

export function setupVoice({ button, onState, onText, useServer, label, getLang, onStart, onStop }) {
  const idleLabel = label || button.textContent;
  let rec = null, chunks = [], stream = null, timer = null, speech = null, active = false, startedAt = 0;
  let byKey = false, vad = null, silentOnly = false;

  const pickMime = () => ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
    .find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || '';

  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mode = useServer && window.MediaRecorder && navigator.mediaDevices ? 'groq' : (SpeechRec ? 'browser' : 'none');

  async function start(key = false) {
    if (active) return;
    byKey = key; silentOnly = false;
    if (mode === 'none') { onState('error', 'Voice not supported in this browser - use Chrome'); return; }
    active = true; startedAt = Date.now();
    if (onStart) onStart();                 // e.g. pause the always-on listener (one mic user at a time)
    button.classList.add('rec'); button.textContent = '■ Listening… tap to stop';
    if (mode === 'browser') return startBrowser();
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    } catch (e) {
      reset(); onState('error', 'Microphone blocked: allow mic access for localhost in Chrome (lock icon in the address bar)'); return;
    }
    if (!active) { stream.getTracks().forEach((t) => t.stop()); onState('error', 'Too short - hold V (or keep recording) while you speak'); return; } // released before mic opened
    const mime = pickMime();
    rec = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    chunks = [];
    rec.ondataavailable = (e) => { if (e.data.size) chunks.push(e.data); };
    rec.onstop = finishGroq;
    rec.start();
    if (!byKey) vad = watchSilence(stream, stop);       // holding V: you decide when to stop
    onState('listening', byKey ? 'Listening... release V when done' : 'Listening... I stop when you pause');
    timer = setTimeout(stop, MAX_SECONDS * 1000);
  }

  // Simple voice-activity check: measures loudness ~20 times a second, learns the room noise,
  // and calls done() after END_SILENCE_MS of quiet that follows real speech.
  function watchSilence(st, done) {
    let ctx;
    try { ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch { return null; }
    const an = ctx.createAnalyser(); an.fftSize = 1024;
    ctx.createMediaStreamSource(st).connect(an);
    const buf = new Float32Array(an.fftSize), t0 = Date.now();
    let floor = 0.004, spoke = 0, quietSince = 0;
    const iv = setInterval(() => {
      an.getFloatTimeDomainData(buf);
      let sum = 0; for (const x of buf) sum += x * x;
      const rms = Math.sqrt(sum / buf.length), gate = Math.max(0.012, floor * 3), now = Date.now();
      if (rms > gate) { spoke += 50; quietSince = 0; }
      else { if (!spoke) floor = floor * 0.95 + Math.min(rms, 0.05) * 0.05; if (!quietSince) quietSince = now; }
      if (spoke >= 250 && quietSince && now - quietSince >= END_SILENCE_MS) done();
      else if (spoke < 250 && now - t0 >= NO_SPEECH_MS) { silentOnly = true; done(); }
    }, 50);
    return { stop() { clearInterval(iv); try { ctx.close(); } catch { /* ignore */ } } };
  }

  function stop() {
    if (!active) return;
    clearTimeout(timer);
    if (vad) { vad.stop(); vad = null; }
    if (mode === 'browser') { speech && speech.stop(); return; }
    if (rec && rec.state !== 'inactive') rec.stop(); else reset();
  }

  async function finishGroq() {
    stream && stream.getTracks().forEach((t) => t.stop());
    const blob = new Blob(chunks, { type: (rec && rec.mimeType) || 'audio/webm' });
    const secs = (Date.now() - startedAt) / 1000;
    reset();
    if (silentOnly) { onState('error', "Didn't hear anything - tap the mic and speak"); return; }   // no API call for silence
    if (secs < 0.6 || blob.size < 800) { onState('error', 'Too short - hold V (or keep recording) while you speak'); return; }
    onState('transcribing', 'Transcribing with Groq Whisper...');
    try {
      const text = await transcribe(blob, getLang ? getLang().iso : '');
      if (!text) { onState('error', 'Could not hear any words - try again closer to the mic'); return; }
      onState('done', ''); onText(text);
    } catch (e) {
      onState('error', 'Voice error: ' + e.message);
    }
  }

  function startBrowser() {
    speech = new SpeechRec();
    speech.lang = getLang ? getLang().code : 'en-IN'; speech.interimResults = true; speech.maxAlternatives = 1;
    let finalText = '';
    speech.onresult = (e) => {
      let interim = '';
      for (const r of e.results) (r.isFinal ? (finalText = r[0].transcript) : (interim += r[0].transcript));
      onState('listening', 'Listening: ' + (finalText || interim));
    };
    speech.onerror = (e) => { onState('error', 'Voice error: ' + e.error); };
    speech.onend = () => { reset(); if (finalText.trim()) { onState('done', ''); onText(finalText.trim()); } };
    speech.start();
    onState('listening', 'Listening (Chrome speech)...');
    timer = setTimeout(stop, MAX_SECONDS * 1000);
  }

  function reset() {
    if (vad) { vad.stop(); vad = null; }
    if (active && onStop) setTimeout(onStop, 400);
    active = false; clearTimeout(timer);
    button.classList.remove('rec'); button.textContent = idleLabel;
  }

  // click = toggle
  button.onclick = () => (active ? stop() : start(false));
  // hold V = push-to-talk (not while typing in a text box)
  window.addEventListener('keydown', (e) => {
    if ((e.key === 'v' || e.key === 'V') && !e.repeat && !(document.activeElement && document.activeElement.tagName === 'INPUT')) {
      e.preventDefault(); start(true);
    }
  });
  window.addEventListener('keyup', (e) => {
    if ((e.key === 'v' || e.key === 'V') && active) { e.preventDefault(); stop(); }
  });

  return { mode };
}
