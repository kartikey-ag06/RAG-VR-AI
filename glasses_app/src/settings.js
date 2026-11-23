// settings.js - user settings, saved in this browser (localStorage).

export const LANGS = [
  { code: 'en-IN', name: 'English', iso: 'en' }, { code: 'hi-IN', name: 'Hindi', iso: 'hi' },
  { code: 'ta-IN', name: 'Tamil', iso: 'ta' }, { code: 'te-IN', name: 'Telugu', iso: 'te' },
  { code: 'bn-IN', name: 'Bengali', iso: 'bn' }, { code: 'mr-IN', name: 'Marathi', iso: 'mr' },
  { code: 'gu-IN', name: 'Gujarati', iso: 'gu' }, { code: 'kn-IN', name: 'Kannada', iso: 'kn' },
  { code: 'ml-IN', name: 'Malayalam', iso: 'ml' }, { code: 'pa-IN', name: 'Punjabi', iso: 'pa' },
  { code: 'ur-IN', name: 'Urdu', iso: 'ur' }, { code: 'en-US', name: 'English (US)', iso: 'en' },
  { code: 'es-ES', name: 'Spanish', iso: 'es' }, { code: 'fr-FR', name: 'French', iso: 'fr' },
  { code: 'de-DE', name: 'German', iso: 'de' }, { code: 'it-IT', name: 'Italian', iso: 'it' },
  { code: 'pt-BR', name: 'Portuguese', iso: 'pt' }, { code: 'ru-RU', name: 'Russian', iso: 'ru' },
  { code: 'ar-SA', name: 'Arabic', iso: 'ar' }, { code: 'ja-JP', name: 'Japanese', iso: 'ja' },
  { code: 'zh-CN', name: 'Chinese', iso: 'zh' }, { code: 'ko-KR', name: 'Korean', iso: 'ko' },
];
export const langByCode = (c) => LANGS.find((l) => l.code === c) || LANGS[0];
export const langByName = (n) => LANGS.find((l) => l.name.toLowerCase() === String(n || '').toLowerCase().trim()) ||
  LANGS.find((l) => String(n || '').toLowerCase().includes(l.name.toLowerCase()));

const DEFAULTS = {
  voiceLang: 'en-IN',      // language you SPEAK (voice input, captions)
  answerLang: 'English',   // language of the AI answers (and spoken answers)
  answerMode: 'full',      // full | glance (3 short points) | spoken (one sentence, read aloud)
  speak: false,            // read every answer aloud
  wake: false,             // "Hey Glasses" wake word (continuous listening, Chrome)
  liveAI: false,           // look at the view every N seconds
  liveInterval: 20,
  wakeEngine: 'auto',      // 'auto' = browser speech engine (Whisper if it fails) · 'whisper' = always Groq Whisper
  tpWpm: 140,              // teleprompter speed when it cannot follow your voice
  tpFollow: true,          // teleprompter follows your voice (Chrome / Edge)
  gestures: false,         // webcam hand gestures (pinch, swipe, palm, thumbs up)
  lookToAsk: false,        // opt-in: keep looking at a pinned page / the PDF panel for 2.5 s -> it explains it
  blurFaces: true,         // blur faces before a photo goes to the cloud AI
  machine: '',             // current machine (from a QR/barcode scan) - filters the documents
  token: '',               // access token (when the server uses document permissions)
};

const KEY = 'vr-rag-settings';
let data = { ...DEFAULTS };
try { data = { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || '{}') }; } catch { /* private mode */ }
if (data.sv !== 2) {          // v2: look-to-ask became opt-in (it fired by itself when a pin was straight ahead)
  data.lookToAsk = false; data.sv = 2;
  try { localStorage.setItem(KEY, JSON.stringify(data)); } catch { /* ignore */ }
}
const subs = new Set();

export const S = new Proxy(data, {
  set(t, k, v) {
    t[k] = v;
    try { localStorage.setItem(KEY, JSON.stringify(t)); if (k === 'token') localStorage.setItem('vr-user-token', v || ''); } catch { /* ignore */ }
    subs.forEach((f) => f(k, v));
    return true;
  },
});
export const onSetting = (f) => subs.add(f);
try { localStorage.setItem('vr-user-token', data.token || ''); } catch { /* ignore */ }
