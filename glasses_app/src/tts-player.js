// tts-player.js - spoken answers. Primary: backend /api/tts (edge-tts neural voice, MP3).
// Fallback: the browser's speechSynthesis when the backend or the internet is not available.
// Sentences are queued and the next one is fetched while the current one plays; stop() = barge-in.
import { apiUrl } from './rag.js';

const MAX_IN_FLIGHT = 2;

export class VRVoicePlayer {
  constructor() {
    this.queue = [];          // sentences waiting to be played
    this.playing = false;
    this.audio = null;
    this.gen = 0;             // bumped by stop() so late async results are dropped
    this.lastError = '';
    this.onState = null;      // optional (state: 'speaking' | 'idle') => void
    this.aborters = new Set(); // in-flight /api/tts requests, cancelled by stop()
    this.firstAudioAt = 0;    // performance.now() when the first sentence of the current answer started playing
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
    this.queue.push({ text: t, blob: null });
    this.pump();
    if (!this.playing) this.playNext();
  }

  /** synthesise at most MAX_IN_FLIGHT sentences at a time (parallel synthesis slows every request down) */
  pump() {
    let active = this.current && !this.current.done ? 1 : 0;
    for (const it of this.queue) if (it.blob && !it.done) active++;
    for (const it of this.queue) {
      if (active >= MAX_IN_FLIGHT) break;
      if (!it.blob) { it.blob = this.fetchAudio(it.text).then((b) => { it.done = true; this.pump(); return b; }); active++; }
    }
  }

  fetchAudio(text) {
    const gen = this.gen, ac = new AbortController();
    this.aborters.add(ac);
    return fetch(apiUrl('/api/tts'), { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text }), signal: ac.signal })
      .then(async (r) => { if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || 'HTTP ' + r.status); return r.blob(); })
      .then((b) => (gen === this.gen ? b : null))
      .catch((e) => { this.lastError = String(e.message || e); return null; })
      .finally(() => this.aborters.delete(ac));
  }

  async playNext() {
    const gen = this.gen;
    const item = this.queue.shift();
    if (!item) { this.playing = false; this.onState && this.onState('idle'); return; }
    this.playing = true; this.onState && this.onState('speaking');
    this.current = item;
    if (!item.blob) { item.blob = this.fetchAudio(item.text).then((b) => { item.done = true; this.pump(); return b; }); }
    this.pump();
    const blob = await item.blob;
    if (gen !== this.gen) return;                               // stopped while waiting
    if (!blob) return this.fallbackSpeak(item.text, gen);
    const url = URL.createObjectURL(blob);
    const a = this.audio = new Audio(url);
    const next = () => { URL.revokeObjectURL(url); if (gen === this.gen) this.playNext(); };
    a.onended = next;
    a.onerror = () => { URL.revokeObjectURL(url); if (gen === this.gen) this.fallbackSpeak(item.text, gen); };
    try { await a.play(); if (!this.firstAudioAt) this.firstAudioAt = performance.now(); } catch (e) { this.lastError = String(e.message || e); next(); }
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
    this.current = null;
    this.firstAudioAt = 0;
    for (const ac of this.aborters) ac.abort();   // cancel synthesis requests still in flight
    this.aborters.clear();
    if (this.audio) { this.audio.pause(); this.audio = null; }
    if ('speechSynthesis' in window) window.speechSynthesis.cancel();
    this.playing = false;
    this.onState && this.onState('idle');
  }
}

/** Speaks an answer WHILE it streams in: feed() gets the growing answer text, every sentence that is complete
 *  (ends with . ? ! and a space) goes to the player at once; finish() speaks whatever is left. */
const FIRST_CLAUSE_MIN = 40;   // chars before the first comma may be spoken as a chunk of its own

export class LiveSpeaker {
  constructor(player) { this.player = player; this.reset(); }

  reset() { this.done = 0; this.startedAt = performance.now(); }

  /** same clean-up the player applies, but keeps offsets stable while text is still arriving */
  static norm(t) { return String(t).replace(/\[\d+(?:\s*,\s*\d+)*\]/g, '').replace(/[*_#•]/g, ''); }

  feed(partial) {
    const t = LiveSpeaker.norm(partial);
    const re = /[.!?]+["')\]]*\s/g;               // sentence end followed by whitespace (so "13.00" is not a boundary)
    let end = this.done, m;
    re.lastIndex = this.done;
    while ((m = re.exec(t))) end = m.index + m[0].length;
    if (this.done === 0 && end === 0) {                // nothing complete yet: start with the first clause (short = fast to synthesise)
      const c = /[,;:—]\s/g; let cm;
      while ((cm = c.exec(t))) if (cm.index >= FIRST_CLAUSE_MIN) { end = cm.index + cm[0].length; break; }
    }
    if (end > this.done) { this.player.speak(t.slice(this.done, end)); this.done = end; }
  }

  finish(fullText) {
    const t = LiveSpeaker.norm(fullText);
    if (t.length > this.done) this.player.speak(t.slice(this.done));
    this.done = t.length;
  }
}
