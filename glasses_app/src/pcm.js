// pcm.js - cut live microphone audio into speech clips (WAV) at the pauses, in any browser.
//
// Used by meeting notes and by the Whisper fallback for wake word / captions in browsers without Chrome's
// speech engine (Firefox, Safari, some headset browsers). A simple loudness gate finds speech; half a second
// before it starts is kept ("pre-roll") so the first word ("Hey ...") is not cut off.

const RATE = 16000;                                   // Whisper works at 16 kHz: small uploads, same accuracy

function toWav(samples) {
  const buf = new ArrayBuffer(44 + samples.length * 2), v = new DataView(buf);
  const w = (o, s) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  w(0, 'RIFF'); v.setUint32(4, 36 + samples.length * 2, true); w(8, 'WAVEfmt ');
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true); v.setUint32(24, RATE, true);
  v.setUint32(28, RATE * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true); w(36, 'data');
  v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) v.setInt16(44 + i * 2, Math.max(-1, Math.min(1, samples[i])) * 0x7fff, true);
  return new Blob([buf], { type: 'audio/wav' });
}

/**
 * opts: onSegment(wavBlob, seconds, startedAtMs) · onLevel(0..1)? · silenceMs (700) · maxMs (15000)
 *       minSpeechMs (350) · threshold (auto: noise floor x 3)
 */
export function createSegmenter(opts) {
  const o = { silenceMs: 700, maxMs: 15000, minSpeechMs: 350, ...opts };
  let ctx = null, src = null, node = null, stream = null;
  let pre = [], seg = null, quietMs = 0, loudMs = 0, floor = 0.004;

  async function start() {
    if (ctx) return;
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
    ctx = new (window.AudioContext || window.webkitAudioContext)();
    src = ctx.createMediaStreamSource(stream);
    node = ctx.createScriptProcessor(4096, 1, 1);
    const ac = ctx, ratio = ac.sampleRate / RATE;
    node.onaudioprocess = (e) => {
      if (ctx !== ac) return;                          // a last callback after stop()
      const inp = e.inputBuffer.getChannelData(0);
      const out = new Float32Array(Math.floor(inp.length / ratio));
      let sum = 0;
      for (let i = 0; i < out.length; i++) { const x = inp[Math.floor(i * ratio)]; out[i] = x; sum += x * x; }
      const rms = Math.sqrt(sum / Math.max(1, out.length)), ms = (inp.length / ac.sampleRate) * 1000;
      if (o.onLevel) o.onLevel(Math.min(1, rms * 12));
      const gate = o.threshold || Math.max(0.012, floor * 3);
      if (!seg) floor = floor * 0.98 + Math.min(rms, 0.05) * 0.02;        // learn the room's background noise
      if (rms > gate) { loudMs += ms; quietMs = 0; } else { quietMs += ms; if (!seg) loudMs = Math.max(0, loudMs - ms); }
      if (!seg) {
        pre.push(out); if (pre.length > 4) pre.shift();                    // ~1 s of pre-roll
        if (loudMs >= 120) { seg = { parts: [...pre], ms: pre.length * ms, t0: Date.now() - pre.length * ms, speech: loudMs }; pre = []; }
        return;
      }
      seg.parts.push(out); seg.ms += ms; if (rms > gate) seg.speech += ms;
      if (quietMs >= o.silenceMs || seg.ms >= o.maxMs) flush();
    };
    src.connect(node); node.connect(ctx.destination);
  }
  function flush() {
    const s = seg; seg = null; loudMs = 0; quietMs = 0;
    if (!s || s.speech < o.minSpeechMs) return;
    const n = s.parts.reduce((a, p) => a + p.length, 0), all = new Float32Array(n);
    let k = 0; for (const p of s.parts) { all.set(p, k); k += p.length; }
    try { o.onSegment(toWav(all), n / RATE, s.t0); } catch { /* caller handles */ }
  }
  function stop() {
    if (!ctx) return;
    flush();
    try { node.disconnect(); src.disconnect(); } catch { /* ignore */ }
    stream.getTracks().forEach((t) => t.stop());
    ctx.close(); ctx = src = node = stream = null; pre = [];
  }
  return { start, stop, flush, get active() { return !!ctx; } };
}

// Whisper invents these on silence or noise - drop them
const HALLUCINATIONS = /^(thank you\.?|thanks for watching!?|you|bye\.?|\.+|subtitles by.*|♪+|music)$/i;
export function cleanTranscript(t) {
  const s = String(t || '').trim();
  return !s || HALLUCINATIONS.test(s) ? '' : s;
}
