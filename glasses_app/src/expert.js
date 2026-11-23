// expert.js - "Escalate to remote expert": share your view + voice with a senior technician (WebRTC).
// The expert opens /expert on any computer; signalling goes through the backend (HTTP polling).
import { api } from './rag.js';

let ICE = { iceServers: [{ urls: 'stun:stun.l.google.com:19302' }], iceTransportPolicy: 'all', turn: false };
// STUN + TURN relay settings come from the server, so calls also work through office / mobile networks
api('/api/expert/ice').then((j) => { if (j && j.iceServers) ICE = j; }).catch(() => {});

export function setupExpert({ hud, viewStream, say, session }) {
  let room = null, pc = null, since = 0, timer = null, view = null, mic = null;

  async function send(type, data) { if (room) await api(`/api/expert/${room}/send`, { from: 'worker', type, data }).catch(() => {}); }

  async function connect() {                  // (re)build the connection when the expert says hello
    if (pc) pc.close();
    pc = new RTCPeerConnection({ iceServers: ICE.iceServers, iceTransportPolicy: ICE.iceTransportPolicy });
    view.stream.getVideoTracks().forEach((t) => pc.addTrack(t, view.stream));
    if (mic) mic.getAudioTracks().forEach((t) => pc.addTrack(t, mic));
    pc.ontrack = (e) => {                     // expert's voice
      const a = document.getElementById('xp-audio') || Object.assign(document.createElement('audio'), { id: 'xp-audio', autoplay: true });
      if (!a.isConnected) document.body.appendChild(a);
      a.srcObject = e.streams[0];
    };
    pc.onicecandidate = (e) => { if (e.candidate) send('ice', e.candidate.toJSON()); };
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'connected') hud.toast('🧑‍🔧 Expert connected - they can see your view', 'expert', 6000);
      if (pc.connectionState === 'failed') hud.toast(ICE.turn ? 'Call could not connect, even through the relay server' : 'Call could not connect: this network blocks direct calls (admin: set a TURN relay)', 'bad', 9000);
    };
    const offer = await pc.createOffer(); await pc.setLocalDescription(offer);
    await send('offer', pc.localDescription.toJSON());
  }

  async function poll() {
    if (!room) return;
    let r;
    try { r = await api(`/api/expert/${room}/poll?me=worker&since=${since}`); } catch { return; }
    for (const m of r.msgs) {
      since = Math.max(since, m.n);
      if (m.type === 'hello') await connect();
      else if (m.type === 'answer' && pc) await pc.setRemoteDescription(m.data);
      else if (m.type === 'ice' && pc) { try { await pc.addIceCandidate(m.data); } catch { /* late */ } }
      else if (m.type === 'chat') { hud.toast('🧑‍🔧 ' + m.data.text, 'expert', 12000); say('Expert says: ' + m.data.text); session.log('expert', { text: 'Expert: ' + m.data.text }); }
      else if (m.type === 'end') { hud.toast('Expert ended the call', 'info'); hangup(false); }
    }
  }

  async function call(context) {
    if (room) { hud.toast('Already in a call — say "end call"', 'info'); return room; }
    const r = await api('/api/expert/call', { context });
    room = r.room; since = 0;
    view = viewStream(15);
    try { mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
    catch { mic = null; hud.toast('Mic blocked: the expert will see you but not hear you', 'warn'); }
    timer = setInterval(poll, 800);
    const link = location.origin + r.expert_url;
    hud.toast('📞 Calling an expert... waiting for them to join', 'expert', 10000);
    session.log('expert', { text: 'Escalated to remote expert', room });
    return { room, link };
  }

  function hangup(notify = true) {
    if (!room) return;
    if (notify) api(`/api/expert/${room}/end`, {}).catch(() => {});
    clearInterval(timer);
    if (pc) pc.close(); pc = null;
    if (view) view.stop(); view = null;
    if (mic) mic.getTracks().forEach((t) => t.stop()); mic = null;
    room = null;
  }

  return { call, hangup, chat: (text) => send('chat', { text }), context: (ctx) => send('context', ctx), get active() { return !!room; } };
}
