// app.js - WebXR scene: question -> RAG -> answer panel + real PDF page panel, floating in VR.
import * as THREE from "three";
import { VRButton } from "three/addons/webxr/VRButton.js";
import { RagIndex, pageImageUrl, loadLlmConfig, generate } from "./rag.js";

const SAMPLES = [
  "What penalty was imposed on Reliance Industries?",
  "Why was Reliance penalised for diluted earnings per share disclosure?",
  "What did Reliance Petroleum do in the futures and cash market in 2007?",
  "What did the Bombay High Court order say?"
];

const $ = id => document.getElementById(id);
const state = { index: null, llm: null, hits: [], hitIdx: 0, doc: -1, page: 1, busy: false, query: "", answer: "" };

// ---------------- three.js scene ----------------
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setSize(innerWidth, innerHeight);
renderer.xr.enabled = true;
renderer.xr.setReferenceSpaceType("local-floor");
document.body.appendChild(renderer.domElement);
document.body.appendChild(VRButton.createButton(renderer));

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0a0d14);
const camera = new THREE.PerspectiveCamera(70, innerWidth / innerHeight, 0.05, 100);
camera.position.set(0, 1.6, 0.6);
scene.add(new THREE.HemisphereLight(0xbcd2ff, 0x202030, 1.2));
const grid = new THREE.GridHelper(20, 40, 0x2b3a5c, 0x182033);
scene.add(grid);

// canvas-backed panel helper
function makePanel(wPx, hPx, wM) {
  const canvas = document.createElement("canvas");
  canvas.width = wPx; canvas.height = hPx;
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 8;
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(wM, wM * hPx / wPx), new THREE.MeshBasicMaterial({ map: tex, transparent: true }));
  return { canvas, ctx: canvas.getContext("2d"), tex, mesh };
}

const answerPanel = makePanel(1024, 1280, 0.9);
answerPanel.mesh.position.set(-0.5, 1.5, -1.3);
answerPanel.mesh.rotation.y = 0.3;
scene.add(answerPanel.mesh);

const pdfPanel = makePanel(1024, 1400, 0.82);
pdfPanel.mesh.position.set(0.45, 1.5, -1.3);
pdfPanel.mesh.rotation.y = -0.3;
scene.add(pdfPanel.mesh);

camera.lookAt(0, 1.5, -1.3);

function wrap(ctx, text, x, y, maxW, lineH, maxY) {
  for (const para of String(text).split("\n")) {
    let line = "";
    for (const word of para.split(" ")) {
      const test = line ? line + " " + word : word;
      if (ctx.measureText(test).width > maxW && line) {
        ctx.fillText(line, x, y); y += lineH; line = word;
        if (y > maxY) { ctx.fillText("...", x, y); return y; }
      } else line = test;
    }
    ctx.fillText(line, x, y); y += lineH;
    if (y > maxY) return y;
  }
  return y;
}

function drawAnswerPanel() {
  const { ctx, canvas, tex } = answerPanel;
  ctx.fillStyle = "rgba(16,22,36,0.95)"; ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.strokeStyle = "#2f4470"; ctx.lineWidth = 6; ctx.strokeRect(3, 3, canvas.width - 6, canvas.height - 6);
  ctx.textBaseline = "top";
  ctx.fillStyle = "#8cc8ff"; ctx.font = "bold 44px Segoe UI, Arial";
  ctx.fillText("VR_RAG  |  SEBI x Reliance", 40, 30);
  ctx.fillStyle = "#ffffff"; ctx.font = "bold 34px Segoe UI, Arial";
  let y = wrap(ctx, "Q: " + (state.query || "") + (state.busy ? "" : "_"), 40, 100, 944, 42, 240);
  ctx.fillStyle = "#e6e6e6"; ctx.font = "28px Segoe UI, Arial";
  y = wrap(ctx, state.answer, 40, Math.max(y + 20, 200), 944, 36, 980);
  ctx.fillStyle = "#ffd98a"; ctx.font = "24px Segoe UI, Arial";
  let sy = 1010;
  if (state.hits.length) {
    ctx.fillText("Sources (grip/Tab = next):", 40, sy); sy += 32;
    state.hits.forEach((h, i) => {
      const t = `${i === state.hitIdx ? ">" : "  "} [${i + 1}] ${h.doc.title} - p.${h.chunk.page} (${h.score.toFixed(2)})`;
      ctx.fillText(t.length > 70 ? t.slice(0, 70) + "..." : t, 40, sy); sy += 30;
    });
  }
  ctx.fillStyle = "#6f7a90"; ctx.font = "italic 20px Segoe UI, Arial";
  ctx.fillText($("status").textContent.slice(0, 90), 40, 1240);
  tex.needsUpdate = true;
}

const imgCache = new Map();
function drawPdfPanel(img, caption) {
  const { ctx, canvas, tex } = pdfPanel;
  ctx.fillStyle = "rgba(16,22,36,0.95)"; ctx.fillRect(0, 0, canvas.width, canvas.height);
  if (img) {
    const maxW = 984, maxH = 1300, s = Math.min(maxW / img.width, maxH / img.height);
    const w = img.width * s, h = img.height * s;
    ctx.drawImage(img, (canvas.width - w) / 2, 20, w, h);
  }
  ctx.fillStyle = "#cfe3ff"; ctx.font = "24px Segoe UI, Arial"; ctx.textBaseline = "top";
  ctx.fillText(caption.slice(0, 80), 30, 1345);
  tex.needsUpdate = true;
}

function showPage(docId, page) {
  if (!state.index || docId < 0) return;
  const doc = state.index.data.docs[docId];
  page = Math.min(Math.max(page, 1), doc.pages);
  state.doc = docId; state.page = page;
  const url = pageImageUrl(docId, page);
  const caption = `${doc.title}  |  page ${page}/${doc.pages}  (trigger / arrows)`;
  const done = img => { if (state.doc === docId && state.page === page) drawPdfPanel(img, caption); };
  if (imgCache.has(url)) return done(imgCache.get(url));
  const img = new Image();
  img.onload = () => { imgCache.set(url, img); done(img); };
  img.onerror = () => drawPdfPanel(null, "Missing image " + url);
  img.src = url;
}

// ---------------- RAG flow ----------------
function renderSourcesDom() {
  $("sources").textContent = state.hits.length
    ? "Sources:\n" + state.hits.map((h, i) => `${i === state.hitIdx ? "▶" : "  "} [${i + 1}] ${h.doc.title} - p.${h.chunk.page}`).join("\n")
    : "";
}

async function ask(q) {
  q = (q || "").trim();
  if (!q || state.busy || !state.index) return;
  state.busy = true; state.query = q; $("q").value = q;
  state.hits = state.index.search(q, 5); state.hitIdx = 0;
  if (state.hits.length) showPage(state.hits[0].doc.id, state.hits[0].chunk.page);
  state.answer = state.llm.provider === "none" ? "Searching..." : `Thinking (${state.llm.provider}${state.llm.model ? ": " + state.llm.model : ""})...`;
  $("answer").textContent = state.answer; renderSourcesDom(); drawAnswerPanel();
  state.answer = await generate(q, state.hits, state.llm);
  state.busy = false;
  $("answer").textContent = state.answer; drawAnswerPanel();
}

function nextHit() {
  if (!state.hits.length) return;
  state.hitIdx = (state.hitIdx + 1) % state.hits.length;
  const h = state.hits[state.hitIdx];
  showPage(h.doc.id, h.chunk.page); renderSourcesDom(); drawAnswerPanel();
}

// ---------------- input ----------------
$("q").addEventListener("input", e => { state.query = e.target.value; drawAnswerPanel(); });
window.addEventListener("keydown", e => {
  const inBox = document.activeElement === $("q");
  if (e.key === "Enter") { e.preventDefault(); ask(inBox ? $("q").value : state.query); return; }
  if (e.key === "Tab") { e.preventDefault(); nextHit(); return; }
  if (e.key === "ArrowRight" && !inBox) return showPage(state.doc, state.page + 1);
  if (e.key === "ArrowLeft" && !inBox) return showPage(state.doc, state.page - 1);
  if (inBox || state.busy) return;
  // typing while the text box is not focused (e.g. inside VR)
  if (e.key === "Backspace") state.query = state.query.slice(0, -1);
  else if (e.key === "Escape") state.query = "";
  else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) state.query += e.key;
  else return;
  $("q").value = state.query; drawAnswerPanel();
});

SAMPLES.forEach((s, i) => {
  const b = document.createElement("button");
  b.textContent = `Sample ${i + 1}`; b.title = s;
  b.onclick = () => ask(s);
  $("samples").appendChild(b);
});

// VR controllers: trigger = next PDF page, grip = next source, with a pointer line
for (let i = 0; i < 2; i++) {
  const c = renderer.xr.getController(i);
  c.addEventListener("select", () => showPage(state.doc, state.page + 1));
  c.addEventListener("squeeze", nextHit);
  c.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, 0, -1)]),
    new THREE.LineBasicMaterial({ color: 0x8cc8ff })));
  scene.add(c);
}

addEventListener("resize", () => {
  camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});
renderer.setAnimationLoop(() => renderer.render(scene, camera));

// ---------------- boot ----------------
(async () => {
  try {
    state.index = await RagIndex.load();
    state.llm = await loadLlmConfig();
    const d = state.index.data;
    $("status").textContent = `${d.docs.length} SEBI orders · ${d.chunks.length} chunks · LLM: ${state.llm.provider}${state.llm.model ? " (" + state.llm.model + ")" : ""}`;
    state.answer = "Type a question and press Enter, or use a Sample button.\n\n" + SAMPLES.map((s, i) => `${i + 1}. ${s}`).join("\n");
  } catch (e) {
    $("status").textContent = "ERROR: " + e.message + " (open via START_VR_RAG.bat, not by double-clicking index.html)";
    state.answer = $("status").textContent;
  }
  $("answer").textContent = state.answer;
  drawAnswerPanel(); drawPdfPanel(null, "PDF page appears here");
})();
