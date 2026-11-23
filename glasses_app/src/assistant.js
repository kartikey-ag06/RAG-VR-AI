// assistant.js - voice commands, like "Hey Meta" on the glasses:
//   simple commands are recognised right here (next step, scan, take a photo, captions on...);
//   sentences with a time or a task ("remind me at 5 to call Ravi", "guide me through...") go to the
//   backend intent router (/api/assistant/intent). Everything else is a normal question for the documents.
import { api } from './rag.js';
import { S, langByName } from './settings.js';

const LOCAL = [
  [/^(next|next step|go on|continue|done|step done)[.!]?$/i, 'next'],
  [/^(back|previous|previous step|go back)[.!]?$/i, 'prev'],
  [/^(repeat|repeat (that|step)|say (it|that) again)[.!]?$/i, 'repeat'],
  [/^(stop|cancel|exit|end) (the )?(procedure|workflow|guide|steps)[.!]?$/i, 'wfstop'],
  [/^(scan|scan (this|that|it)|what('?s| is) (this|that)\??|identify (this|that))[.!]?$/i, 'scan'],
  [/^(scan|read) (the )?(qr|qr code|barcode|code|tag|label)[.!]?$/i, 'code'],
  [/^(take|capture) (a )?(photo|picture|pic|snapshot)[.!]?$/i, 'photo'],
  [/^(start|begin) (a )?(recording|video)[.!]?$/i, 'videoOn'],
  [/^(stop|end) (the )?(recording|video)[.!]?$/i, 'videoOff'],
  [/^(open|show) (the )?(gallery|photos)[.!]?$/i, 'gallery'],
  [/^(open |show )?(the )?(filter camera|filters|camera filters)[.!]?$/i, 'filters'],
  [/^(captions|subtitles|live captions) (on|start)[.!]?$|^(start|turn on|show) (live )?(captions|subtitles)[.!]?$/i, 'capOn'],
  [/^(captions|subtitles) off[.!]?$|^(stop|turn off|hide) (live )?(captions|subtitles)[.!]?$/i, 'capOff'],
  [/^(live ai|live a\.?i\.?) (on|start)[.!]?$|^(start|turn on) live ai[.!]?$/i, 'liveOn'],
  [/^(live ai|live a\.?i\.?) (off|stop)[.!]?$|^(stop|turn off) live ai[.!]?$/i, 'liveOff'],
  [/^(pin|pin (this|it|that)|pin (the )?(page|answer))[.!]?$/i, 'pin'],
  [/^(show|open) (the )?(diagram|figure|table|schematic)[.!]?$/i, 'diagram'],
  [/^(call|escalate to|get|contact) (an? |the )?(remote )?(expert|senior|supervisor|technician)[.!]?$|^escalate[.!]?$/i, 'expert'],
  [/^(end|hang up|stop) (the )?(call)[.!]?$/i, 'hangup'],
  [/^(clear|forget) (the )?machine[.!]?$/i, 'clearMachine'],
  [/^(stop talking|be quiet|silence|shut up)[.!]?$/i, 'quiet'],
];
const NEEDS_ROUTER = /\b(remind|reminders?|minder|remainder|alarm|timer|note|notes|weather|temperature|calendar|schedule|appointment|meeting|procedure|guide me|walk me through|step by step|steps to|translate|caption|export|report|work order|incident|inspection|language|speak in|answer in)\b/i;

export function setupAssistant(ctx) {
  // ctx: { hud, say(text), act: { next, prev, ... }, setAnswer(text, mode), setLang, ... }
  let lastCmd = '', lastAt = 0;
  async function route(text) {
    // common speech-to-text slips: "Minder for 11.05", "remainder to call Ravi" -> reminder
    const t = text.trim().replace(/^(please|hey glasses|ok glasses|hey vr)[, ]+/i, '')
      .replace(/^(?:a |the )?(?:minder|remainder|reminder)\b(?:\s+(?:for|to|at))?/i, 'Remind me')
      .replace(/\b(minder|remainder)\b/gi, 'reminder');
    for (const [re, cmd] of LOCAL) if (re.test(t)) { await ctx.act(cmd, {}); return true; }   // "next" twice = two steps
    // the same sentence twice within 3 s (double tap, wake word + push-to-talk) runs once
    const key = t.toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
    if (key && key === lastCmd && Date.now() - lastAt < 3000) return true;
    lastCmd = key; lastAt = Date.now();
    const tm = t.match(/^(?:set |start )?(?:a )?timer (?:for )?(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)(?: (?:for|called|to) (.+))?[.!]?$/i);
    if (tm) {
      const n = parseFloat(tm[1]), u = tm[2][0].toLowerCase();
      return timer(Math.round(n * (u === 's' ? 1 : u === 'm' ? 60 : 3600)), tm[3]);
    }
    if (!NEEDS_ROUTER.test(t)) return false;
    let r;
    try { r = await api('/api/assistant/intent', { text: t, now: localIso() }); }
    catch { return false; }
    const a = r.args || {};
    switch (r.intent) {
      case 'reminder_add': return addReminder(a.text || t, a.due);
      case 'reminder_update': return updateReminder(a);
      case 'reminder_delete': return deleteReminder(a.which || 'last');
      case 'timer': return timer(parseInt(a.seconds, 10) || 60, a.label);
      case 'reminder_list': return listReminders();
      case 'note_add': { await api('/api/notes', { text: a.text || t }); ctx.reply('📝 Noted: ' + (a.text || t), 'Note saved.'); return true; }
      case 'note_list': {
        const n = (await api('/api/notes')).notes;
        ctx.reply(n.length ? '📝 Your notes:\n' + n.slice(0, 8).map((x) => '• ' + x.text).join('\n') : 'You have no notes yet.', n.length ? `You have ${n.length} notes.` : 'No notes yet.');
        return true;
      }
      case 'calendar': return calendar(a.day || 'today');
      case 'weather': return weather(a.city);
      case 'workflow_start': await ctx.act('wfstart', { topic: a.topic || t }); return true;
      case 'translate': {
        const lang = langByName(a.target) || { name: a.target || 'English' };
        if (!a.text) { await ctx.act('capOn', { translateTo: lang.name }); return true; }
        const out = (await api('/api/translate', { text: a.text, target: lang.name })).text;
        ctx.reply(`🌐 ${lang.name}:\n${out}`, out, lang.code); return true;
      }
      case 'captions': await ctx.act(a.on === false ? 'capOff' : 'capOn', { translateTo: a.translate_to }); return true;
      case 'export': await ctx.act('export', { kind: a.kind, format: a.format }); return true;
      case 'expert': await ctx.act('expert', {}); return true;
      case 'liveai': await ctx.act(a.on === false ? 'liveOff' : 'liveOn', {}); return true;
      case 'photo': await ctx.act('photo', {}); return true;
      case 'video': await ctx.act(a.on === false ? 'videoOff' : 'videoOn', {}); return true;
      case 'scan': await ctx.act('scan', {}); return true;
      case 'code': await ctx.act('code', {}); return true;
      case 'pin': await ctx.act('pin', {}); return true;
      case 'diagram': await ctx.act('diagram', {}); return true;
      case 'language': {
        const l = langByName(a.lang);
        if (l) { S.answerLang = l.name; ctx.reply(`🌐 Answers are now in ${l.name}.`, `Okay, I will answer in ${l.name}.`, l.code); return true; }
        return false;
      }
      default: return false;
    }
  }

  async function addReminder(text, due) {
    const r = await api('/api/reminders', { text, due: due || null });
    const when = r.due ? new Date(r.due).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' }) : '';
    ctx.reply(`⏰ Reminder set: ${r.text}${when ? ' — ' + when : ''}`, `Okay, I'll remind you${when ? ' at ' + when : ''}: ${r.text}`);
    return true;
  }
  const fmtDue = (d) => new Date(d).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  async function findReminder(which) {
    const rs = (await api('/api/reminders')).reminders;
    if (!rs.length) return { rs, r: null };
    const w = String(which || 'last').toLowerCase();
    if (w === 'last' || w === 'latest' || w === 'that') return { rs, r: [...rs].sort((a, b) => b.created - a.created)[0] };
    const words = w.split(/\W+/).filter((x) => x.length > 2);
    const score = (r) => words.filter((x) => r.text.toLowerCase().includes(x)).length;
    const best = [...rs].sort((a, b) => score(b) - score(a))[0];
    return { rs, r: score(best) ? best : [...rs].sort((a, b) => b.created - a.created)[0] };
  }
  async function updateReminder(a) {
    const { r } = await findReminder(a.which);
    if (!r) { ctx.reply('⏰ No reminders to change.', 'You have no reminders.'); return true; }
    const patch = {};
    if (a.due) patch.due = a.due;
    if (a.text) patch.text = a.text;
    if (!Object.keys(patch).length) { ctx.reply(`⏰ What should "${r.text}" change to? Say e.g. "move it to 7 pm".`, 'What time?'); return true; }
    const u = await api('/api/reminders/' + r.id, patch, 'PATCH');
    ctx.reply(`⏰ Updated: ${u.text}${u.due ? ' — ' + fmtDue(u.due) : ''}`, `Done. ${u.text}${u.due ? ' at ' + fmtDue(u.due) : ''}.`);
    return true;
  }
  async function deleteReminder(which) {
    const { rs, r } = await findReminder(which);
    if (!r) { ctx.reply('⏰ No reminders.', 'You have no reminders.'); return true; }
    const del = String(which).toLowerCase() === 'all' ? rs : [r];
    for (const x of del) await api('/api/reminders/' + x.id, undefined, 'DELETE');
    ctx.reply(del.length > 1 ? `🗑 Deleted ${del.length} reminders.` : `🗑 Deleted: ${r.text}`, del.length > 1 ? `Deleted ${del.length} reminders.` : `Deleted ${r.text}.`);
    return true;
  }
  async function timer(sec, label) {
    sec = Math.max(1, Math.min(sec, 86400));
    const name = (label ? String(label).replace(/[.!]$/, '') : '') || `${sec >= 60 ? Math.round(sec / 60) + ' min' : sec + ' s'} timer`;
    const due = new Date(Date.now() + sec * 1000);
    const r = await api('/api/reminders', { text: '⏱ ' + name, due: due.toISOString() });
    setTimeout(checkDue, sec * 1000 + 300);                   // exact, not on the 30 s check
    ctx.hud.toast(`⏱ ${name} — rings at ${due.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`, 'ok', 5000);
    ctx.reply(`⏱ Timer set: ${name}`, `Timer set for ${sec >= 60 ? Math.round(sec / 60) + ' minutes' : sec + ' seconds'}.`);
    return r && true;
  }
  async function listReminders() {
    const rs = (await api('/api/reminders')).reminders;
    ctx.reply(rs.length ? '⏰ Reminders:\n' + rs.slice(0, 10).map((r) => `• ${r.text}${r.due ? ' — ' + new Date(r.due).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' }) : ''}`).join('\n') : 'No reminders.',
      rs.length ? `You have ${rs.length} reminders. Next: ${rs[0].text}.` : 'You have no reminders.');
    return true;
  }
  async function calendar(day) {
    const tz = -new Date().getTimezoneOffset();
    const c = await api(`/api/calendar?day=${encodeURIComponent(day)}&tz=${tz}`);
    ctx.reply(c.events.length ? `📅 ${c.day}:\n` + c.events.map((e) => `• ${e.local}  ${e.text}`).join('\n') : `📅 Nothing on ${c.day}.`,
      c.events.length ? `${c.events.length} things on ${day}. First at ${c.events[0].local}: ${c.events[0].text}.` : `Nothing on your calendar for ${day}.`);
    return true;
  }
  async function weather(city) {
    let q = city ? `city=${encodeURIComponent(city)}` : '';
    if (!q) {
      const pos = await new Promise((res) => navigator.geolocation ? navigator.geolocation.getCurrentPosition(res, () => res(null), { timeout: 6000, maximumAge: 6e5 }) : res(null));
      if (pos) q = `lat=${pos.coords.latitude}&lon=${pos.coords.longitude}`;
      else { ctx.reply('☀ Which city? Say "weather in Delhi" (or allow location for this site).', 'Which city?'); return true; }
    }
    const w = await api('/api/weather?' + q);
    const d = w.days[0] || {};
    ctx.reply(`☀ ${w.place}: ${Math.round(w.temp)}°C, ${w.desc}\nToday ${Math.round(d.min)}–${Math.round(d.max)}°C, rain ${d.rain ?? 0}%` +
      (w.days[1] ? `\nTomorrow ${Math.round(w.days[1].min)}–${Math.round(w.days[1].max)}°C, ${w.days[1].desc}` : ''),
      `${Math.round(w.temp)} degrees and ${w.desc} in ${w.place.split(',')[0]}. Rain chance ${d.rain ?? 0} percent.`);
    return true;
  }

  // due reminders -> notification on the lens + read aloud
  async function checkDue() {
    try {
      const rs = (await api('/api/reminders')).reminders;
      const now = Date.now();
      for (const r of rs) {
        if (r.due && !r.notified && new Date(r.due).getTime() <= now) {
          ctx.hud.toast('⏰ ' + r.text, 'warn', 12000);
          ctx.say('Reminder: ' + r.text);
          await api('/api/reminders/' + r.id, { notified: true }, 'PATCH');
        }
      }
    } catch { /* offline */ }
  }
  setInterval(() => { if (!document.hidden) checkDue(); }, 60000); setTimeout(checkDue, 4000);   // timers also schedule their own exact check

  return { route, weather, calendar, listReminders };
}

function localIso() {
  const d = new Date(), off = -d.getTimezoneOffset(), sign = off >= 0 ? '+' : '-', p = (n) => String(Math.floor(Math.abs(n))).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}${sign}${p(off / 60)}:${p(off % 60)}`;
}
