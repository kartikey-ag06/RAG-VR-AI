// media.js - camera features of the glasses:
//   * photo capture (VR view or webcam) and video recording, kept in a gallery in this browser (IndexedDB)
//   * troubleshooting filters: OCR boost (serial numbers), low-light boost, edge highlight (wiring)
//   * look filters: B&W, vintage, warm, cool, cartoon
//   * privacy: blur faces before a photo goes to the cloud AI (MediaPipe face detector, runs on this computer)
//   * AR face filters: glasses, hat, moustache, sparkles on faces in the webcam (MediaPipe face landmarks)

export const FILTERS = [
  { id: 'none', name: 'No filter' }, { id: 'ocr', name: 'OCR boost' }, { id: 'lowlight', name: 'Low-light boost' },
  { id: 'edges', name: 'Edge highlight' }, { id: 'bw', name: 'Black & white' }, { id: 'vintage', name: 'Vintage' },
  { id: 'warm', name: 'Warm' }, { id: 'cool', name: 'Cool' }, { id: 'cartoon', name: 'Cartoon' },
];
export const STICKERS = [{ id: 'none', name: 'No AR' }, { id: 'glasses', name: '🕶 Glasses' }, { id: 'hat', name: '🎩 Hat' },
  { id: 'moustache', name: '🥸 Moustache' }, { id: 'sparkles', name: '✨ Sparkles' }];

// ---------------------------------------------------------------- pixel filters (canvas 2D, in place)
export function applyFilter(canvas, id) {
  if (!id || id === 'none') return canvas;
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  const { width: w, height: h } = canvas;
  const img = ctx.getImageData(0, 0, w, h), d = img.data, n = w * h;
  const gray = new Float32Array(n);
  for (let i = 0, p = 0; i < n; i++, p += 4) gray[i] = 0.299 * d[p] + 0.587 * d[p + 1] + 0.114 * d[p + 2];
  const clamp = (v) => (v < 0 ? 0 : v > 255 ? 255 : v);
  const sobel = () => {
    const e = new Float32Array(n);
    for (let y = 1; y < h - 1; y++) for (let x = 1; x < w - 1; x++) {
      const i = y * w + x;
      const gx = -gray[i - w - 1] - 2 * gray[i - 1] - gray[i + w - 1] + gray[i - w + 1] + 2 * gray[i + 1] + gray[i + w + 1];
      const gy = -gray[i - w - 1] - 2 * gray[i - w] - gray[i - w + 1] + gray[i + w - 1] + 2 * gray[i + w] + gray[i + w + 1];
      e[i] = Math.min(255, Math.hypot(gx, gy));
    }
    return e;
  };
  if (id === 'ocr') {                       // grey, stretch contrast (2%-98%), sharpen: makes labels / serials readable
    const hist = new Uint32Array(256); for (let i = 0; i < n; i++) hist[gray[i] | 0]++;
    let lo = 0, hi = 255, acc = 0;
    for (let v = 0; v < 256; v++) { acc += hist[v]; if (acc > n * 0.02) { lo = v; break; } }
    acc = 0; for (let v = 255; v >= 0; v--) { acc += hist[v]; if (acc > n * 0.02) { hi = v; break; } }
    const s = 255 / Math.max(1, hi - lo), g2 = new Float32Array(n);
    for (let i = 0; i < n; i++) g2[i] = clamp((gray[i] - lo) * s);
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const i = y * w + x, p = i * 4;
      const v = x > 0 && y > 0 && x < w - 1 && y < h - 1 ? clamp(5 * g2[i] - g2[i - 1] - g2[i + 1] - g2[i - w] - g2[i + w]) : g2[i];
      d[p] = d[p + 1] = d[p + 2] = v;
    }
  } else if (id === 'lowlight') {           // gamma lift + auto gain towards a normal brightness
    let mean = 0; for (let i = 0; i < n; i++) mean += gray[i]; mean /= n;
    const gain = Math.min(3, 125 / Math.max(10, mean));
    for (let p = 0; p < d.length; p += 4) for (let c = 0; c < 3; c++) d[p + c] = clamp(255 * Math.pow((d[p + c] * gain) / 255, 0.7));
  } else if (id === 'edges') {               // darkened picture + bright cyan edges (wires, cracks, outlines)
    const e = sobel();
    for (let i = 0, p = 0; i < n; i++, p += 4) {
      const k = e[i] > 60 ? 1 : 0;
      d[p] = clamp(d[p] * 0.45 + k * 40); d[p + 1] = clamp(d[p + 1] * 0.45 + k * 255); d[p + 2] = clamp(d[p + 2] * 0.45 + k * 255);
    }
  } else if (id === 'bw') {
    for (let i = 0, p = 0; i < n; i++, p += 4) d[p] = d[p + 1] = d[p + 2] = clamp((gray[i] - 128) * 1.15 + 128);
  } else if (id === 'vintage') {
    for (let y = 0; y < h; y++) for (let x = 0; x < w; x++) {
      const p = (y * w + x) * 4, r = d[p], g = d[p + 1], b = d[p + 2];
      const dx = x / w - 0.5, dy = y / h - 0.5, vig = 1 - Math.min(0.55, (dx * dx + dy * dy) * 1.6);
      d[p] = clamp((r * 0.393 + g * 0.769 + b * 0.189) * vig); d[p + 1] = clamp((r * 0.349 + g * 0.686 + b * 0.168) * vig);
      d[p + 2] = clamp((r * 0.272 + g * 0.534 + b * 0.131) * vig);
    }
  } else if (id === 'warm' || id === 'cool') {
    const s = id === 'warm' ? 1 : -1;
    for (let p = 0; p < d.length; p += 4) { d[p] = clamp(d[p] + 22 * s); d[p + 1] = clamp(d[p + 1] + 6 * s); d[p + 2] = clamp(d[p + 2] - 22 * s); }
  } else if (id === 'cartoon') {             // posterize + dark outlines
    const e = sobel(), q = (v) => Math.round(v / 51) * 51;
    for (let i = 0, p = 0; i < n; i++, p += 4) {
      if (e[i] > 90) { d[p] = d[p + 1] = d[p + 2] = 20; continue; }
      d[p] = q(d[p]); d[p + 1] = q(d[p + 1]); d[p + 2] = q(d[p + 2]);
    }
  }
  ctx.putImageData(img, 0, 0);
  return canvas;
}

// ---------------------------------------------------------------- MediaPipe (loaded only when first needed)
const WASM = 'https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@1.2.3/wasm';
const MODELS = {
  detector: 'https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite',
  landmarker: 'https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task',
};
let mp = null, detector = null, landmarker = null;
async function vision() {
  if (!mp) {
    const m = await import('@mediapipe/tasks-vision');
    mp = { m, files: await m.FilesetResolver.forVisionTasks(WASM) };
  }
  return mp;
}
async function getDetector() {
  if (!detector) {
    const { m, files } = await vision();
    detector = await m.FaceDetector.createFromOptions(files, { baseOptions: { modelAssetPath: MODELS.detector }, runningMode: 'IMAGE', minDetectionConfidence: 0.45 });
  }
  return detector;
}
async function getLandmarker() {
  if (!landmarker) {
    const { m, files } = await vision();
    landmarker = await m.FaceLandmarker.createFromOptions(files, { baseOptions: { modelAssetPath: MODELS.landmarker }, runningMode: 'VIDEO', numFaces: 3 });
  }
  return landmarker;
}

/** Blurs every face in the canvas (in place). Returns the number of faces, or -1 if the detector could not load. */
export async function blurFaces(canvas) {
  let det;
  try { det = await getDetector(); } catch (e) { console.warn('[privacy] face detector unavailable', e); return -1; }
  const res = det.detect(canvas);
  const ctx = canvas.getContext('2d');
  for (const f of res.detections || []) {
    const b = f.boundingBox; if (!b) continue;
    const pad = 0.25, x = Math.max(0, b.originX - b.width * pad), y = Math.max(0, b.originY - b.height * pad * 1.4);
    const w = Math.min(canvas.width - x, b.width * (1 + 2 * pad)), h = Math.min(canvas.height - y, b.height * (1 + 2.4 * pad));
    const small = document.createElement('canvas'); small.width = 12; small.height = 12;
    small.getContext('2d').drawImage(canvas, x, y, w, h, 0, 0, 12, 12);               // pixelate = cannot be undone
    ctx.imageSmoothingEnabled = false; ctx.drawImage(small, 0, 0, 12, 12, x, y, w, h); ctx.imageSmoothingEnabled = true;
  }
  return (res.detections || []).length;
}

function drawSticker(ctx, lm, kind, W, H, t) {
  const P = (i) => ({ x: lm[i].x * W, y: lm[i].y * H });
  const L = P(33), R = P(263), nose = P(1), top = P(10), lip = P(13);
  const eyeDist = Math.hypot(R.x - L.x, R.y - L.y), ang = Math.atan2(R.y - L.y, R.x - L.x);
  ctx.save();
  if (kind === 'glasses') {
    const cx = (L.x + R.x) / 2, cy = (L.y + R.y) / 2; ctx.translate(cx, cy); ctx.rotate(ang);
    ctx.fillStyle = 'rgba(10,10,10,0.85)'; ctx.strokeStyle = '#111'; ctx.lineWidth = eyeDist * 0.06;
    for (const s of [-1, 1]) { ctx.beginPath(); ctx.ellipse(s * eyeDist * 0.5, 0, eyeDist * 0.42, eyeDist * 0.3, 0, 0, Math.PI * 2); ctx.fill(); ctx.stroke(); }
    ctx.beginPath(); ctx.moveTo(-eyeDist * 0.1, -eyeDist * 0.05); ctx.lineTo(eyeDist * 0.1, -eyeDist * 0.05); ctx.stroke();
  } else if (kind === 'hat') {
    ctx.translate(top.x, top.y); ctx.rotate(ang);
    ctx.fillStyle = '#1b1b1f'; ctx.fillRect(-eyeDist * 1.3, -eyeDist * 0.25, eyeDist * 2.6, eyeDist * 0.25);
    ctx.fillRect(-eyeDist * 0.8, -eyeDist * 1.5, eyeDist * 1.6, eyeDist * 1.3);
    ctx.fillStyle = '#b3243b'; ctx.fillRect(-eyeDist * 0.8, -eyeDist * 0.5, eyeDist * 1.6, eyeDist * 0.22);
  } else if (kind === 'moustache') {
    const cx = (nose.x + lip.x) / 2, cy = (nose.y * 0.4 + lip.y * 0.6); ctx.translate(cx, cy); ctx.rotate(ang);
    ctx.fillStyle = '#2a1a10';
    for (const s of [-1, 1]) { ctx.beginPath(); ctx.ellipse(s * eyeDist * 0.28, 0, eyeDist * 0.32, eyeDist * 0.11, s * 0.25, 0, Math.PI * 2); ctx.fill(); }
  } else if (kind === 'sparkles') {
    ctx.font = `${Math.round(eyeDist * 0.5)}px serif`; ctx.textAlign = 'center';
    for (let i = 0; i < 6; i++) {
      const a = t / 600 + (i * Math.PI) / 3, r = eyeDist * 1.4;
      ctx.fillText(i % 2 ? '✨' : '⭐', top.x + Math.cos(a) * r, top.y + eyeDist * 0.5 + Math.sin(a) * r * 0.6);
    }
  }
  ctx.restore();
}

// ---------------------------------------------------------------- gallery (IndexedDB, this browser only)
let dbp = null;
function db() {
  if (!dbp) dbp = new Promise((res, rej) => {
    const r = indexedDB.open('vr-rag-media', 1);
    r.onupgradeneeded = () => r.result.createObjectStore('items', { keyPath: 'id' });
    r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
  });
  return dbp;
}
async function store(mode) { return (await db()).transaction('items', mode).objectStore('items'); }
const req = (r) => new Promise((res, rej) => { r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error); });
export async function saveItem(blob, type, meta = {}) {
  const item = { id: Date.now().toString(36) + Math.random().toString(36).slice(2, 6), type, blob, created: Date.now(), ...meta };
  await req((await store('readwrite')).put(item)); return item;
}
export async function listItems() { return (await req((await store('readonly')).getAll())).sort((a, b) => b.created - a.created); }
export async function deleteItem(id) { await req((await store('readwrite')).delete(id)); }

export const canvasToBlob = (c, q = 0.9) => new Promise((r) => c.toBlob(r, 'image/jpeg', q));
export const blobToDataUrl = (b) => new Promise((r) => { const f = new FileReader(); f.onload = () => r(f.result); f.readAsDataURL(b); });
export function dataUrlToCanvas(url) {
  return new Promise((res, rej) => { const i = new Image(); i.onload = () => { const c = document.createElement('canvas'); c.width = i.naturalWidth; c.height = i.naturalHeight; c.getContext('2d').drawImage(i, 0, 0); res(c); }; i.onerror = rej; i.src = url; });
}

// ---------------------------------------------------------------- UI: filter camera, gallery, recorder
const CSS = `
#md-box{position:fixed;inset:0;z-index:2147483002;background:rgba(5,8,15,.86);display:none;align-items:center;justify-content:center}
#md-card{background:#0f1526;border:1px solid #2a3550;border-radius:14px;padding:14px;width:min(860px,96vw);max-height:94vh;overflow:auto;color:#e8e8e8;font:13px Segoe UI,Arial,sans-serif}
#md-card h2{font-size:15px;margin:0 0 10px;color:#8cc8ff;display:flex;justify-content:space-between;align-items:center}
#md-card button,#md-card select{margin:4px 4px 0 0;padding:7px 11px;border-radius:8px;border:1px solid #3a4766;background:#1b2540;color:#dfe8ff;cursor:pointer;font:inherit;font-size:12px}
#md-card .go{background:#2447c9;border-color:#4d74ff;color:#fff;font-weight:600}
#md-view{width:100%;max-height:60vh;object-fit:contain;background:#000;border-radius:10px;display:block}
.md-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px}
.md-it{background:#141d33;border-radius:8px;overflow:hidden;cursor:pointer;position:relative}
.md-it img,.md-it video{width:100%;height:110px;object-fit:cover;display:block}
.md-it span{position:absolute;left:6px;bottom:4px;font-size:11px;background:rgba(0,0,0,.6);padding:0 5px;border-radius:5px}
.md-note{color:#8a95ab;font-size:11px;margin-top:6px}`;

export function setupMedia({ onAsk, toast, captureView, viewStream }) {
  const st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
  const box = document.createElement('div'); box.id = 'md-box';
  box.innerHTML = '<div id="md-card"></div>';
  document.body.appendChild(box);
  box.addEventListener('keydown', (e) => e.stopPropagation(), true);
  const card = box.querySelector('#md-card');
  let cam = null, loop = 0, rec = null;
  const close = () => { cancelAnimationFrame(loop); if (cam) cam.getTracks().forEach((t) => t.stop()); cam = null; box.style.display = 'none'; };
  box.addEventListener('click', (e) => { if (e.target === box) close(); });

  // ---- live filter camera (webcam) with AR face filters
  async function openCamera() {
    box.style.display = 'flex';
    card.innerHTML = `<h2>🎭 Filter camera <button id="md-x">✕</button></h2>
      <canvas id="md-live" style="width:100%;max-height:60vh;background:#000;border-radius:10px;display:block"></canvas>
      <div><select id="md-f">${FILTERS.map((f) => `<option value="${f.id}">${f.name}</option>`).join('')}</select>
      <select id="md-s">${STICKERS.map((f) => `<option value="${f.id}">${f.name}</option>`).join('')}</select>
      <button class="go" id="md-snap">📸 Capture</button><button id="md-ask">🤖 Capture & ask AI</button><button id="md-rec">🎥 Record</button></div>
      <div class="md-note" id="md-msg">Filters run on this computer. AR face filters load a small model the first time.</div>`;
    card.querySelector('#md-x').onclick = close;
    const v = document.createElement('video'); v.muted = true; v.playsInline = true;
    try { cam = await navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 960 } }, audio: false }); }
    catch (e) { card.querySelector('#md-msg').textContent = 'Camera not available: ' + e.message; return; }
    v.srcObject = cam; await v.play();
    const c = card.querySelector('#md-live'); c.width = v.videoWidth || 640; c.height = v.videoHeight || 480;
    const x = c.getContext('2d', { willReadFrequently: true });
    let lmReady = null;
    const frame = (t) => {
      x.drawImage(v, 0, 0, c.width, c.height);
      const f = card.querySelector('#md-f').value, s = card.querySelector('#md-s').value;
      if (f !== 'none') applyFilter(c, f);
      if (s !== 'none') {
        if (!lmReady) { lmReady = getLandmarker().then((l) => (lmReady = l)).catch(() => { lmReady = 'x'; card.querySelector('#md-msg').textContent = 'AR face filters could not load (no internet?)'; }); }
        else if (lmReady !== 'x' && lmReady.detectForVideo) {
          const r = lmReady.detectForVideo(v, performance.now());
          for (const lm of r.faceLandmarks || []) drawSticker(x, lm, s, c.width, c.height, t);
        }
      }
      loop = requestAnimationFrame(frame);
    };
    loop = requestAnimationFrame(frame);
    const snap = async (ask) => {
      const out = document.createElement('canvas'); out.width = c.width; out.height = c.height; out.getContext('2d').drawImage(c, 0, 0);
      await saveItem(await canvasToBlob(out), 'photo', { source: 'webcam', filter: card.querySelector('#md-f').value });
      toast('📸 Photo saved to the gallery', 'ok');
      if (ask) { close(); onAsk(out); }
    };
    card.querySelector('#md-snap').onclick = () => snap(false);
    card.querySelector('#md-ask').onclick = () => snap(true);
    card.querySelector('#md-rec').onclick = (e) => {
      if (rec) { rec.stop(); return; }
      rec = recorder(c.captureStream(24), 'webcam', () => { e.target.textContent = '🎥 Record'; rec = null; });
      e.target.textContent = '■ Stop recording';
    };
  }

  // ---- recorder (VR view or a canvas stream)
  function recorder(stream, source, done) {
    const mime = ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm'].find((m) => MediaRecorder.isTypeSupported(m)) || '';
    const mr = new MediaRecorder(stream, mime ? { mimeType: mime, videoBitsPerSecond: 3e6 } : {});
    const parts = [], tag = 'rec-' + Math.random().toString(36).slice(2, 7);
    mr.ondataavailable = (e) => e.data.size && parts.push(e.data);
    mr.onstop = async () => {
      privacy.set(tag, null);
      const blob = new Blob(parts, { type: 'video/webm' });
      if (blob.size) { await saveItem(blob, 'video', { source }); toast(`🎥 Video saved (${(blob.size / 1048576).toFixed(1)} MB)`, 'ok'); }
      done && done();
    };
    mr.start(1000);
    privacy.set(tag, 'recording');                       // red blinking REC on the lens while it records
    return { stop: () => mr.state !== 'inactive' && mr.stop() };
  }
  let viewRec = null;
  function toggleViewRecording() {
    if (viewRec) { viewRec.stop(); return false; }
    const vs = viewStream(24);
    const r = recorder(vs.stream, 'vr-view', () => { vs.stop(); viewRec = null; });
    viewRec = { stop: () => r.stop() };
    toast('🔴 Recording your view... say "stop recording"', 'bad', 3500);
    return true;
  }

  // ---- photo of the VR view
  async function capturePhoto(filter = 'none') {
    const url = await captureView(false);
    const c = await dataUrlToCanvas(url);
    if (filter !== 'none') applyFilter(c, filter);
    const item = await saveItem(await canvasToBlob(c), 'photo', { source: 'vr-view', filter });
    toast('📸 Photo saved to the gallery', 'ok');
    return { item, canvas: c };
  }

  // ---- gallery
  async function openGallery() {
    box.style.display = 'flex';
    const items = await listItems();
    card.innerHTML = `<h2>🖼 Gallery <span><button id="md-cam">🎭 Filter camera</button><button id="md-x">✕</button></span></h2>
      ${items.length ? `<div class="md-grid">${items.map((it) => `<div class="md-it" data-id="${it.id}">${it.type === 'video' ? '<video muted></video>' : '<img alt="">'}
        <span>${it.type === 'video' ? '🎥' : '📸'} ${new Date(it.created).toLocaleTimeString()}${it.filter && it.filter !== 'none' ? ' · ' + it.filter : ''}</span></div>`).join('')}</div>`
        : '<div class="md-note">No photos or videos yet. Say "take a photo" or "start recording".</div>'}
      <div id="md-detail"></div><div class="md-note">Saved only in this browser. Download to keep a copy.</div>`;
    card.querySelector('#md-x').onclick = close;
    card.querySelector('#md-cam').onclick = openCamera;
    const urls = [];
    card.querySelectorAll('.md-it').forEach((el) => {
      const it = items.find((i) => i.id === el.dataset.id), u = URL.createObjectURL(it.blob); urls.push(u);
      el.firstElementChild.src = u;
      el.onclick = () => detail(it, u);
    });
  }
  function detail(it, url) {
    const d = card.querySelector('#md-detail');
    d.innerHTML = `<div style="margin-top:10px">${it.type === 'video' ? `<video id="md-view" src="${url}" controls></video>` : `<img id="md-view" src="${url}" alt="">`}
      <a href="${url}" download="vr_rag_${it.id}.${it.type === 'video' ? 'webm' : 'jpg'}"><button>⬇ Download</button></a>
      ${it.type === 'photo' ? `<select id="md-f2">${FILTERS.map((f) => `<option value="${f.id}">${f.name}</option>`).join('')}</select><button id="md-apply">Apply filter (copy)</button><button class="go" id="md-ask2">🤖 Ask AI about it</button>` : ''}
      <button id="md-del">🗑 Delete</button></div>`;
    d.querySelector('#md-del').onclick = async () => { await deleteItem(it.id); openGallery(); };
    if (it.type === 'photo') {
      d.querySelector('#md-ask2').onclick = async () => { const c = await dataUrlToCanvas(await blobToDataUrl(it.blob)); close(); onAsk(c); };
      d.querySelector('#md-apply').onclick = async () => {
        const c = await dataUrlToCanvas(await blobToDataUrl(it.blob));
        applyFilter(c, d.querySelector('#md-f2').value);
        await saveItem(await canvasToBlob(c), 'photo', { source: it.source, filter: d.querySelector('#md-f2').value });
        openGallery();
      };
    }
  }

  return { openCamera, openGallery, capturePhoto, toggleViewRecording, get recording() { return !!viewRec; } };
}
