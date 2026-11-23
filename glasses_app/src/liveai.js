// liveai.js - "Live AI": the assistant keeps looking at your view every few seconds (like Meta's Live AI),
// writes one short note on the lens, notices QR codes / labels, and remembers what it saw so that
// "what is this?" can be answered from the latest look. Backs off automatically on Groq rate limits.
import { privacy } from './privacy.js';
import { describePhoto } from './rag.js';
import { readCodes } from './codes.js';
import { dataUrlToCanvas } from './media.js';

const HINT = 'LIVE AI MODE: this is a frame of what the user sees through smart glasses. In "object" write ONE short useful ' +
  'sentence about the most important thing in the middle of the view (a machine, a label, an error code, a risk). ';

export function setupLiveAI({ hud, capture, onCode, intervalSec }) {
  let on = false, timer = null, wait = intervalSec(), busy = false, last = null;

  async function look() {
    if (!on || busy || document.hidden) return schedule();
    busy = true;
    try {
      const url = await capture();
      const c = await dataUrlToCanvas(url);
      const codes = await readCodes(c).catch(() => []);
      if (codes.length) onCode(codes[0].text);
      const d = await describePhoto(url, HINT);
      last = { at: Date.now(), ...d };
      hud.setLive(d.object + (d.text_seen ? ` · "${d.text_seen.slice(0, 40)}"` : ''));
      wait = intervalSec();
    } catch (e) {
      if (/limit|429/i.test(e.message)) { wait = Math.min(120, wait * 2); hud.setLive(`(paused ${wait}s - AI rate limit)`); }
      else hud.setLive('(Live AI: ' + e.message.slice(0, 50) + ')');
    }
    busy = false;
    schedule();
  }
  function schedule() { clearTimeout(timer); if (on) timer = setTimeout(look, wait * 1000); }

  return {
    start() { if (on) return; on = true; privacy.set('live-ai', 'camera'); wait = intervalSec(); hud.toast('👁 Live AI on - I will look every ' + wait + ' s', 'info'); look(); },
    stop() { on = false; privacy.set('live-ai', null); clearTimeout(timer); hud.setLive(''); hud.toast('👁 Live AI off', 'info'); },
    get on() { return on; },
    /** latest look if it is fresh (used for "what is this?") */
    recent(maxAgeMs = 15000) { return last && Date.now() - last.at < maxAgeMs ? last : null; },
  };
}
