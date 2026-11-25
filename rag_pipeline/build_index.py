"""
VR_RAG - offline RAG index builder.

What it does:
  1. Reads every PDF in ../docs
  2. Extracts text page by page (PyMuPDF)
  3. Splits each page into overlapping chunks (~120 words)
  4. Builds a TF-IDF index (word importance scores) for keyword/semantic-ish search
  5. Renders every PDF page to a JPG so the VR app can show the real page
  6. Writes everything into ../web/rag (served to the WebXR app)

Run:  python build_index.py
"""
import json
import math
import os
import re
import sys
from collections import Counter

import pymupdf  # pip install pymupdf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DOCS = os.path.join(ROOT, "docs")
OUT = os.path.join(ROOT, "web", "rag")
PAGES_OUT = os.path.join(OUT, "pages")

CHUNK_WORDS = 120
OVERLAP = 30
IMG_WIDTH = 1000

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
from core.knowledge.stopwords import load_stopwords  # noqa: E402  same stop-words as the server index
STOP = load_stopwords(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text):
    return [t for t in TOKEN_RE.findall(text.lower()) if len(t) > 1 and t not in STOP]


def pretty_title(fname):
    # "01_2021-04_AO_Reliance_Industries_Ltd.pdf" -> "2021-04 AO Reliance Industries Ltd"
    base = os.path.splitext(fname)[0]
    parts = base.split("_")[1:]
    return " ".join(parts).replace("AO", "Adjudication Order", 1)


def main():
    os.makedirs(PAGES_OUT, exist_ok=True)
    pdfs = sorted(f for f in os.listdir(DOCS) if f.lower().endswith(".pdf"))
    if not pdfs:
        sys.exit("No PDFs found in " + DOCS)

    docs, chunks = [], []
    for di, fname in enumerate(pdfs):
        doc = pymupdf.open(os.path.join(DOCS, fname))
        docs.append({"id": di, "file": fname, "title": pretty_title(fname), "pages": len(doc)})
        for pi, page in enumerate(doc):
            # 1) page image
            img_name = f"d{di:02d}_p{pi + 1:03d}.jpg"
            img_path = os.path.join(PAGES_OUT, img_name)
            if not os.path.exists(img_path):
                zoom = IMG_WIDTH / page.rect.width
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom))
                pix.save(img_path, jpg_quality=70)
            # 2) text chunks
            words = page.get_text().split()
            step = CHUNK_WORDS - OVERLAP
            for start in range(0, max(len(words), 1), step):
                piece = words[start:start + CHUNK_WORDS]
                if len(piece) < 15 and start > 0:
                    break
                text = " ".join(piece)
                if len(tokenize(text)) < 5:
                    continue
                chunks.append({"doc": di, "page": pi + 1, "text": text})
        print(f"[{di + 1}/{len(pdfs)}] {fname}: {len(doc)} pages")

    # 3) TF-IDF
    tokenized = [tokenize(c["text"]) for c in chunks]
    df = Counter()
    for toks in tokenized:
        df.update(set(toks))
    n = len(chunks)
    vocab = sorted(t for t, c in df.items() if c >= 1)
    vidx = {t: i for i, t in enumerate(vocab)}
    idf = [round(math.log((n + 1) / (df[t] + 1)) + 1.0, 4) for t in vocab]

    for c, toks in zip(chunks, tokenized):
        tf = Counter(toks)
        vec = {vidx[t]: (1 + math.log(cnt)) * idf[vidx[t]] for t, cnt in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        items = sorted(vec.items())
        c["terms"] = [k for k, _ in items]
        c["weights"] = [round(v / norm, 4) for _, v in items]

    index = {"docs": docs, "vocab": vocab, "idf": idf, "chunks": chunks}
    with open(os.path.join(OUT, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    cfg = os.path.join(OUT, "llm_config.json")
    if not os.path.exists(cfg):
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({
                "provider": "none",
                "_providers": "none | anthropic | openai | ollama",
                "apiKey": "",
                "model": "",
                "_models": "anthropic: claude-sonnet-5-5 | openai: gpt-4o-mini | ollama: llama3.2",
                "endpoint": "",
                "_endpoint": "only for ollama, e.g. http://localhost:11434/api/chat"
            }, f, indent=2)

    print(f"Done: {len(docs)} docs, {n} chunks, {len(vocab)} terms -> {OUT}")


if __name__ == "__main__":
    main()
