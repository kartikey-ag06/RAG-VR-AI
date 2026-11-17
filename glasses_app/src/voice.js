// voice.js - push-to-talk voice input.
//   Primary : record with MediaRecorder -> /api/stt -> Groq Whisper (accurate, works with any accent)
//   Fallback: Chrome's built-in Web Speech API (used when the server has no Groq key)
// Controls: click the mic button (click again to stop), or HOLD the V key while speaking.
import { transcribe } from './rag.js';

const MAX_SECONDS = 15;

export function setupVoice({ button, onState, onText, useServer }) {
  let rec = null, chunks = [], stream = null, timer = null, speech = null, active = false, startedAt = 0;

  const pickMime = () => ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
    .find((m) => window.MediaRecorder && MediaRecorder.isTypeSupported(m)) || '';

  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mode = useServer && window.MediaRecorder && navigator.mediaDevices ? 'groq' : (SpeechRec ? 'browser' : 'none');

  async function start() {
    if (active) return;
    if (mode === 'none') { onState('error', 'Voice not supported in this browser - use Chrome'); return; }
    active = true; startedAt = Date.now();
    button.classList.add('rec'); button.textContent = '■ Stop';
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
    onState('listening', `Listening... speak now (max ${MAX_SECONDS}s)`);
    timer = setTimeout(stop, MAX_SECONDS * 1000);
  }

  function stop() {
    if (!active) return;
    clearTimeout(timer);
    if (mode === 'browser') { speech && speech.stop(); return; }
    if (rec && rec.state !== 'inactive') rec.stop(); else reset();
  }

  async function finishGroq() {
    stream && stream.getTracks().forEach((t) => t.stop());
    const blob = new Blob(chunks, { type: (rec && rec.mimeType) || 'audio/webm' });
    const secs = (Date.now() - startedAt) / 1000;
    reset();
    if (secs < 0.6 || blob.size < 800) { onState('error', 'Too short - hold V (or keep recording) while you speak'); return; }
    onState('transcribing', 'Transcribing with Groq Whisper...');
    try {
      const text = await transcribe(blob);
      if (!text) { onState('error', 'Could not hear any words - try again closer to the mic'); return; }
      onState('done', ''); onText(text);
    } catch (e) {
      onState('error', 'Voice error: ' + e.message);
    }
  }

  function startBrowser() {
    speech = new SpeechRec();
    speech.lang = 'en-IN'; speech.interimResults = true; speech.maxAlternatives = 1;
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
    active = false; clearTimeout(timer);
    button.classList.remove('rec'); button.textContent = '🎤 Speak';
  }

  // click = toggle
  button.onclick = () => (active ? stop() : start());
  // hold V = push-to-talk (not while typing in a text box)
  window.addEventListener('keydown', (e) => {
    if ((e.key === 'v' || e.key === 'V') && !e.repeat && !(document.activeElement && document.activeElement.tagName === 'INPUT')) {
      e.preventDefault(); start();
    }
  });
  window.addEventListener('keyup', (e) => {
    if ((e.key === 'v' || e.key === 'V') && active) { e.preventDefault(); stop(); }
  });

  return { mode };
}
