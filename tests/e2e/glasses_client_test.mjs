// Runs the REAL glasses client code (glasses_app/src/rag.js) against the real backend + fake Groq.
// Started by tests/test_glasses_client.py, which sets E2E_API, E2E_OUT and E2E_RAG.
globalThis.window = globalThis;
globalThis.__ENV = { VITE_API_URL: process.env.E2E_API, BASE_URL: "/" };
const rag = await import(process.env.E2E_RAG);
const ok = (c, m) => { if (!c) { console.error("FAIL:", m); process.exitCode = 1; } else console.log("ok  -", m); };
const fs = await import("node:fs");
const last = () => JSON.parse(fs.readFileSync(process.env.E2E_OUT + "/last.json", "utf-8"));

const ix = await rag.RagIndex.load();
const local = ix.search("penalty insider trading", 4);
ok(local.length === 4 && local.every((h) => Number.isInteger(h.i) && ix.data.chunks[h.i] === h.chunk), "local hits carry their chunk position i");
const srv = await rag.serverSearch(ix, "penalty", 4);
ok(srv && srv.hits.every((h) => ix.data.chunks[h.i] === h.chunk), "server-search hits carry i too");

let deltas = 0;
const res = await rag.answerSmart("What penalty was imposed?", local, { provider: "groq" }, () => deltas++);
ok(res.mode === "pdf" && JSON.stringify(res.cited) === "[1]", `cited points at OUR 2nd hit (got ${JSON.stringify(res.cited)})`);
ok(res.text.startsWith("The answer is on page two") && !res.text.includes("SOURCE"), "answer text is clean");
ok(deltas >= 3, `streamed in pieces (${deltas} updates)`);
const [sys, user] = last();
ok(sys.role === "system" && !/sebi|reliance/i.test(sys.content), "system prompt came from the server, no SEBI wording");
ok(user.content.includes(local[1].chunk.text) && user.content.includes(local[0].chunk.text), "server used the exact passages we found");

rag.setAnswerStyle("Hindi", "spoken");
await rag.answerSmart("short answer please", local.slice(0, 1), { provider: "groq" });
ok(/25 words/.test(last()[0].content) && /in Hindi/.test(last()[0].content), "answer style + language reach the server prompt");
rag.setAnswerStyle("English", "full");

const vis = await rag.answerVision("what is this?", { object: "a red pump", details: "", text_seen: "P-77", keywords: "pump" },
  local, null, "", { provider: "groq" });
ok(last()[1].content.startsWith("PHOTO DESCRIPTION (from the vision model): a red pump.") && vis.mode === "pdf", "photo description is used");

const bad = await rag.answerSmart("boom", local, { provider: "groq" });
ok(bad.mode === "offline" && bad.cited.length === 3, "AI failure -> offline answer with the first passages");

const p = rag.answerSmart("stop me", local, { provider: "groq" }, () => rag.stopAnswer());
const st = await p;
ok(st.stopped === true, "Stop works while the answer streams");

const none = await rag.answerSmart("x", [], { provider: "none" });
ok(none.mode === "offline" && !/sebi/i.test(none.text), "no-AI message no longer mentions SEBI");
