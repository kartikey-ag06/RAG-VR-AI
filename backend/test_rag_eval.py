"""
Golden-question benchmark for the 10 SEBI orders. Run it after any prompt, chunking or ranking change.

  1. start the app:   START_VR_RAG.bat   (or: backend\\.venv\\Scripts\\python.exe backend\\app.py --no-browser)
  2. run:             backend\\.venv\\Scripts\\python.exe backend\\test_rag_eval.py            (retrieval only, no API cost)
                      backend\\.venv\\Scripts\\python.exe backend\\test_rag_eval.py --answers  (also asks the LLM, needs the key)
                      add --url http://127.0.0.1:8765 if the app runs elsewhere. Exit code 1 if any case fails.

Every expected value below was checked against the text of the PDFs (not guessed).
  expected_doc    : file name that must appear in the top-K retrieved chunks (None = must be "unconfident")
  expected_text   : text that must appear in the retrieved chunk of that document / in the LLM answer
"""
import argparse
import json
import re
import sys
import time

import requests

GOLDEN = [
    {"query": "What penalty was imposed on Reliance Industries for not disclosing diluted EPS?",
     "expected_doc": "08_2014-08_AO_RIL_Diluted_EPS_NonDisclosure.pdf", "expected_text": ["13,00,00,000", "Thirteen Crore", "13 crore", "130000000", "13.00 crore"]},
    {"query": "Who was the Adjudicating Officer in the 2020 BH Kothari order?",
     "expected_doc": "03_2020-09_AO_BH_Kothari_Reliance_Industries.pdf", "expected_text": ["Piyush Gupta"]},
    {"query": "Were the 2018 adjudication proceedings against RIL and group entities ended with a penalty?",
     "expected_doc": "05_2018-02_AO_RIL_and_Group_Entities.pdf", "expected_text": ["without imposition of monetary penalty", "disposed of"]},
    {"query": "What was the maximum monetary penalty under the unamended Section 15H when the warrants were converted in January 2000?",
     "expected_doc": "01_2021-04_AO_Reliance_Industries_Ltd.pdf", "expected_text": ["5,00,000"]},
    {"query": "What is the capital of France?", "expected_doc": None, "expected_text": []},
    {"query": "How to bake sourdough bread?", "expected_doc": None, "expected_text": []},
]
TOP_K = 5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8765")
    ap.add_argument("--answers", action="store_true", help="also check the LLM answer text")
    a = ap.parse_args()
    B = a.url.rstrip("/")
    try:
        idx = requests.get(B + "/rag/index.json", timeout=20).json()
    except Exception as e:
        sys.exit(f"Cannot reach the app at {B}: {e}\nStart it first (START_VR_RAG.bat).")
    docs = idx["docs"]
    rows, failed = [], 0
    for g in GOLDEN:
        r = requests.post(B + "/api/search", json={"query": g["query"], "k": TOP_K}, timeout=60).json()
        hits = r.get("hits", [])
        files = [docs[idx["chunks"][h["i"]]["doc"]]["file"] for h in hits]
        texts = [idx["chunks"][h["i"]]["text"] for h in hits]
        if g["expected_doc"] is None:
            ok_ret = r.get("mode") == "unconfident"
            detail = f"mode={r.get('mode')} (want unconfident)"
        else:
            pos = files.index(g["expected_doc"]) + 1 if g["expected_doc"] in files else 0
            has_text = any(t.lower() in x.lower() for f, x in zip(files, texts) if f == g["expected_doc"] for t in g["expected_text"])
            ok_ret = bool(pos) and has_text
            detail = f"doc rank={pos or 'MISSING'} text_in_chunk={has_text} conf={r.get('confidence', 0):.2f}"
        ans_ok = None
        if a.answers and g["expected_doc"]:
            prompt = "Excerpts:\n" + "".join(f"[{i + 1}] ({docs[idx['chunks'][h['i']]['doc']]['title']}, page {idx['chunks'][h['i']]['page']})\n{t}\n\n"
                                            for i, (h, t) in enumerate(zip(hits, texts))) + "Question: " + g["query"]
            for attempt in range(5):                       # Groq free tier: wait and retry on HTTP 429 (rate limit)
                j = requests.post(B + "/api/grok", json={"messages": [
                    {"role": "system", "content": "Answer ONLY from the numbered excerpts, in max 80 words, citing [n]. Never invent figures."},
                    {"role": "user", "content": prompt}]}, timeout=90).json()
                if "429" not in json.dumps(j.get("error", "")):
                    break
                time.sleep(20)
            ans = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
            if not ans:                                    # API error (rate limit...): say so instead of failing silently
                ans = json.dumps(j)[:200]
                detail += " [LLM error: " + ans[:120] + "]"
            norm = lambda s: re.sub(r"[\s‑‐-]+", " ", s.lower())
            ans_ok = any(norm(t) in norm(ans) for t in g["expected_text"])
            detail += f" answer_has_fact={ans_ok}"
        ok = ok_ret and (ans_ok is not False)
        failed += not ok
        rows.append((("PASS" if ok else "FAIL"), g["query"][:70], detail))
    print()
    for st, q, d in rows:
        print(f"{st}  {q:<72} {d}")
    print(f"\n{len(rows) - failed}/{len(rows)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
