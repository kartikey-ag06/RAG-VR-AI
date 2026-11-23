// xr-panels.js - the two floating 3D panels (answer + PDF page): canvas drawing, page-image LRU cache,
// page navigation and re-positioning in front of the user. Owns no business logic: it only draws `state`.
import { Mesh, PlaneGeometry, MeshBasicMaterial, CanvasTexture, SRGBColorSpace, DoubleSide, Vector3 } from '@iwsdk/core';
import { pageImageUrl } from './rag.js';

const MAX_CACHED_PAGES = 6;           // bounded so many pages cannot exhaust a headset's memory
export const CONFIDENCE = {           // colour chip on the answer panel (from the reranker score)
  high: ['HIGH CONFIDENCE', '#2e7d4f'],
  medium: ['MEDIUM CONFIDENCE', '#a37b14'],
  low: ['LOW CONFIDENCE', '#a8323f'],
};

export class XRPanelManager {
  constructor(world, state) {
    this.world = world;
    this.state = state;
    // Meta VR Glasses have a narrower field of view than Quest, so panels sit close to the centre of view.
    this.ansP = this.makePanel(1024, 1280, 0.72, [-0.39, 1.5, -1.25], 0.28);
    this.pdfP = this.makePanel(1024, 1400, 0.66, [0.37, 1.5, -1.25], -0.28);
    this.cache = new Map();           // url -> decoded Image, least recently used first
  }

  get meshes() { return [this.ansP.mesh, this.pdfP.mesh]; }

  makePanel(wPx, hPx, wM, pos, yaw) {
    const canvas = document.createElement('canvas');
    canvas.width = wPx; canvas.height = hPx;
    const tex = new CanvasTexture(canvas);
    tex.colorSpace = SRGBColorSpace; tex.anisotropy = 8;
    const mesh = new Mesh(new PlaneGeometry(wM, (wM * hPx) / wPx),
      new MeshBasicMaterial({ map: tex, transparent: true, side: DoubleSide }));
    mesh.position.set(...pos); mesh.rotation.y = yaw;
    this.world.createTransformEntity(mesh);
    return { canvas, ctx: canvas.getContext('2d'), tex, mesh };
  }

  static wrap(ctx, text, x, y, maxW, lh, maxY) {
    for (const para of String(text).split('\n')) {
      let line = '';
      for (const word of para.split(' ')) {
        const t = line ? line + ' ' + word : word;
        if (ctx.measureText(t).width > maxW && line) {
          ctx.fillText(line, x, y); y += lh; line = word;
          if (y > maxY) { ctx.fillText('...', x, y); return y; }
        } else line = t;
      }
      ctx.fillText(line, x, y); y += lh;
      if (y > maxY) return y;
    }
    return y;
  }

  drawAnswer() {
    const state = this.state, wrap = XRPanelManager.wrap;
    const { ctx, canvas, tex } = this.ansP;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = 'rgba(16,22,36,0.96)'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    // border colour = confidence of the retrieval (green / amber / red), neutral blue when nothing was searched yet
    const conf = CONFIDENCE[state.level];
    ctx.strokeStyle = conf ? conf[1] : '#2f4470'; ctx.lineWidth = conf ? 10 : 6; ctx.strokeRect(5, 5, canvas.width - 10, canvas.height - 10);
    ctx.textBaseline = 'top';
    ctx.fillStyle = '#8cc8ff'; ctx.font = 'bold 44px Segoe UI, Arial'; ctx.fillText('VR_RAG  |  Your documents', 40, 30);
    const thumb = state.photoImg;
    if (thumb) {                                        // the scanned photo, top right
      const s = Math.min(230 / thumb.width, 172 / thumb.height), w = thumb.width * s, h = thumb.height * s;
      ctx.drawImage(thumb, 984 - w, 92, w, h); ctx.strokeStyle = '#8cc8ff'; ctx.lineWidth = 3; ctx.strokeRect(984 - w, 92, w, h);
    }
    ctx.fillStyle = '#fff'; ctx.font = 'bold 34px Segoe UI, Arial';
    let y = wrap(ctx, 'Q: ' + state.query + (state.busy ? '' : '_'), 40, 100, thumb ? 690 : 944, 42, 240);
    if (thumb) y = Math.max(y, 280);
    let ay = Math.max(y + 20, 200);
    const badge = { pdf: ['FROM THE SEBI PDFs', '#2e7d4f'], general: ['GENERAL AI ANSWER - not from the PDFs', '#8a5a14'], offline: ['OFFLINE - best matching PDF sentences', '#4a5568'] }[state.mode];
    let bx = 40;
    ctx.font = 'bold 24px Segoe UI, Arial';
    if (badge) {
      const label = (state.usedPhoto ? 'PHOTO + ' : '') + badge[0];
      const w = ctx.measureText(label).width + 30;
      ctx.fillStyle = badge[1]; ctx.fillRect(bx, ay, w, 38); ctx.fillStyle = '#fff'; ctx.fillText(label, bx + 15, ay + 7);
      bx += w + 12;
    }
    if (conf) {
      const label = conf[0] + (state.confidence != null ? ` ${Math.round(state.confidence * 100)}%` : '');
      const w = ctx.measureText(label).width + 30;
      ctx.fillStyle = conf[1]; ctx.fillRect(bx, ay, w, 38); ctx.fillStyle = '#fff'; ctx.fillText(label, bx + 15, ay + 7);
    }
    if (badge || conf) ay += 55;
    ctx.fillStyle = '#e6e6e6'; ctx.font = '29px Segoe UI, Arial';
    wrap(ctx, state.answer, 40, ay, 944, 37, 980);
    ctx.fillStyle = '#ffd98a'; ctx.font = '24px Segoe UI, Arial';
    let sy = 1010;
    if (state.hits.length) {
      ctx.fillText('PDF sources used (squeeze / Tab = next):', 40, sy); sy += 32;
      state.hits.forEach((h, i) => {
        const t = `${i === state.hitIdx ? '>' : '  '} [${i + 1}] ${h.doc.title} - p.${h.chunk.page}`;
        ctx.fillText(t.length > 68 ? t.slice(0, 68) + '...' : t, 40, sy); sy += 30;
      });
    }
    ctx.fillStyle = '#6f7a90'; ctx.font = 'italic 20px Segoe UI, Arial'; ctx.fillText(state.status.slice(0, 90), 40, 1242);
    tex.needsUpdate = true;
  }

  drawPdf(img, caption) {
    const { ctx, canvas, tex } = this.pdfP;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = 'rgba(16,22,36,0.96)'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    if (img) {
      const s = Math.min(984 / img.width, 1300 / img.height), w = img.width * s, h = img.height * s;
      ctx.drawImage(img, (canvas.width - w) / 2, 20, w, h);
    }
    ctx.fillStyle = '#cfe3ff'; ctx.font = '24px Segoe UI, Arial'; ctx.textBaseline = 'top';
    ctx.fillText(caption.slice(0, 80), 30, 1345);
    tex.needsUpdate = true;
  }

  // ---- LRU cache of decoded page images
  cacheGet(url) { const v = this.cache.get(url); if (v) { this.cache.delete(url); this.cache.set(url, v); } return v; }
  cacheSet(url, img) {
    this.cache.set(url, img);
    while (this.cache.size > MAX_CACHED_PAGES) {
      const oldest = this.cache.keys().next().value, old = this.cache.get(oldest);
      this.cache.delete(oldest);
      if (old && old.close) old.close();                // release decoded bitmap if the browser supports it
    }
  }
  clearCache() { this.cache.clear(); }

  showPage(docId, page) {
    const state = this.state;
    if (!state.index || docId < 0) return;
    const doc = state.index.data.docs[docId];
    page = Math.min(Math.max(page, 1), doc.pages);
    state.doc = docId; state.page = page;
    const url = pageImageUrl(docId, page);
    const ver = doc.rev && doc.rev !== '1.0' ? ` v${doc.rev}` : '';
    const cap = `${doc.title}${ver}  |  page ${page}/${doc.pages}  (pinch = next)`;
    const done = (img) => { if (state.doc === docId && state.page === page) this.drawPdf(img, cap); };
    if (this.cache.has(url)) return done(this.cacheGet(url));
    const img = new Image();
    img.onload = () => { this.cacheSet(url, img); done(img); };
    img.onerror = () => this.drawPdf(null, 'Missing image ' + url);
    img.src = url;
  }

  /** After a scan the user may be facing anywhere: move both panels in front of their current view
   *  (same layout as at start: answer left, PDF page right, slightly turned towards the user). */
  bringToView() {
    const cam = this.world.camera, pos = new Vector3(), f = new Vector3();
    cam.getWorldPosition(pos); cam.getWorldDirection(f);
    f.y = 0; if (f.lengthSq() < 1e-6) return; f.normalize();
    const right = new Vector3(-f.z, 0, f.x), yaw = Math.atan2(-f.x, -f.z);
    const place = (m, side, turn) => {
      m.position.copy(pos).addScaledVector(f, 1.25).addScaledVector(right, side); m.position.y = pos.y;
      m.rotation.set(0, yaw + turn, 0);
    };
    place(this.ansP.mesh, -0.39, 0.28); place(this.pdfP.mesh, 0.37, -0.28);
  }
}
