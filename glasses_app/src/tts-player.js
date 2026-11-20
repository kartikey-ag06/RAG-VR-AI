// tts-player.js - spoken answers. Primary: backend /api/tts (edge-tts neural voice, MP3).
// Fallback: the browser's speechSynthesis when the backend or the internet is not available.
// Sentences are queued and the next one is fetched while the current one plays; stop() = barge-in.
import { apiUrl } from './rag.js';

export class VRVoicePlayer {
  constructor() {
    this.queue = [];          // sentences waiting to be played
    this.playing = false;
    this.audio = null;
    this.gen = 0;             // bumped by stop() so late async results are dropped
    this.lastError = '';
    this.onState = null;      // optional (state: 'speaking' | 'idle') => void
  }

  /** Speak a whole answer (split into sentences). */
  speak(text) {
    for (const s of VRVoicePlayer.sentences(text)) this.enqueue(s);
  }

  static sentences(text) {
    const clean = String(text || '').replace(/\[\d+(?:\s*,\s*\d+)*\]/g, '').replace(/[•*_#]/g, ' ').replace(/\s+/g, ' ').trim();
    if (!clean) return [];
    return (clean.match(/[^.!?]+[.!?]+["')\]]*|[^.!?]+$/g) || [clean]).map((s) => s.trim()).filter((s) => /\w/.test(s));
  }

  enqueue(sentence) {
    const t = sentence.trim();
    if (!t) return;
    this.queue.push({ text: t, blob: this.fetchAudio(t) });   // start synthesising right away (prefetch)
    if (!this.playing) this.playNext();
  }

  fetchAudio(text) {
    const gen = this.gen;
    return fetch(apiUrl('/api/tts'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }) })
      .then(async (r) => { if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || 'HTTP ' + r.status); return r.blob(); })
      .then((b) => (gen === this.gen ? b : null))
      .catch((e) => { this.lastError = String(e.message || e); return null; });
  }

  async playNext() {
    const gen = this.gen;
    const item = this.queue.shift();
    if (!item) { this.playing = false; this.onState && this.onState('idle'); return; }
    this.playing = true; this.onState && this.onState('speaking');
    const blob = await item.blob;
    if (gen !== this.gen) return;                               // stopped while waiting
    if (!blob) return this.fallbackSpeak(item.text, gen);
    const url = URL.createObjectURL(blob);
    const a = this.audio = new Audio(url);
    const next = () => { URL.revokeObjectURL(url); if (gen === this.gen) this.playNext(); };
    a.onended = next;
    a.onerror = () => { URL.revokeObjectURL(url); if (gen === this.gen) this.fallbackSpeak(item.text, gen); };
    try { await a.play(); } catch (e) { this.lastError = String(e.message || e); next(); }
  }

  fallbackSpeak(text, gen) {
    if (!('speechSynthesis' in window)) { this.playNext(); return; }
    const u = new SpeechSynthesisUtterance(text);
    u.onend = u.onerror = () => { if (gen === this.gen) this.playNext(); };
    window.speechSynthesis.speak(u);
  }

  /** Barge-in: cut off speech and drop everything queued. */
  stop() {
    this.gen++;
    this.queue = [];
    if (this.audio) { this.audio.pause(); this.audio = null; }
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    this.playing = false;
    this.onState && this.onState('idle');
  }
}
