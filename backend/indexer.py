"""
indexer.py - the document store behind the upload page.

Uploaded files live in backend/data/ (not in docs/):
  data/files/<uid><ext>      the original upload
  data/pages/<uid>/pNNN.jpg  one image per page (PDF pages are rendered; DOCX/TXT pages are drawn as text)
  data/pages/<uid>/text.json the text of each page
  data/emb/<uid>_<model>.npy embeddings of that document's chunks (computed once per file)
  data/manifest.json         list of documents (name, type, pages, status)
  data/index.json            the combined search index the VR app downloads

Every change (upload / delete) rebuilds the index in a background thread and bumps `version`,
so the VR app knows it must reload. Chunking + TF-IDF match rag_pipeline/build_index.py.
"""
import hashlib
import json
import math
import re
import shutil
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ALLOWED = {".pdf", ".docx", ".txt"}
MAX_BYTES = 50 * 1024 * 1024
CHUNK_WORDS, OVERLAP = 120, 30
IMG_W, IMG_H = 1000, 1414          # A4 ratio, same width as the PDF page renders
WORDS_PER_TEXT_PAGE = 330          # DOCX/TXT are split into "pages" of about this many words

STOP = set("""a an the and or of to in on for by with at from as is are was were be been being this that these those it its
which who whom whose what when where why how not no nor but if then than so such into upon under over above below
shall will would should may might can could must do does did done have has had having he she they them his her their
i we you me us our your said also any all each other same per vide hereinafter thereof therein herein whereas
page sebi noticee noticees order""".split())


def tokenize(text):
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1 and t not in STOP]


def _window_words(text):
    """Old fixed-size chunking: ~120-word windows with 30 words overlap (used for long plain paragraphs)."""
    words, out = text.split(), []
    step = CHUNK_WORDS - OVERLAP
    for start in range(0, max(len(words), 1), step):
        piece = words[start:start + CHUNK_WORDS]
        if len(piece) < 15 and start > 0:
            break
        t = " ".join(piece)
        if len(tokenize(t)) >= 5:
            out.append(t)
    return out


def _is_table_line(line):
    return line.lstrip().startswith("|")


def chunk_page(text):
    """Layout-aware chunking. Paragraphs are kept whole and packed into chunks of up to ~CHUNK_WORDS words.
    A markdown table (lines starting with '|') is never cut in the middle of a row: a big table is split
    between rows, and every part repeats the table header, so a penalty amount stays next to its label."""
    if not any(_is_table_line(l) for l in text.splitlines()) and "\n\n" not in text.strip():
        return _window_words(text)                       # plain text without structure (DOCX/TXT pages, old data)
    blocks, cur, in_table = [], [], False
    for line in text.splitlines():                       # split into paragraph / table blocks
        tbl = _is_table_line(line)
        if (not line.strip()) or tbl != in_table:
            if cur:
                blocks.append((in_table, cur))
            cur = []
        if line.strip():
            cur.append(line)
        in_table = tbl
    if cur:
        blocks.append((in_table, cur))
    pieces = []                                          # (text, is_table) units no bigger than ~CHUNK_WORDS*1.5
    for is_tbl, lines in blocks:
        if is_tbl:
            head, rows, part = lines[:2], lines[2:], []
            if len(lines) < 3 or not set(lines[1].replace("|", "").strip()) <= set("-: "):
                head, rows = [], lines                   # no header separator row
            n = sum(len(l.split()) for l in head)
            for r in rows or [""]:
                w = len(r.split())
                if part and n + w > CHUNK_WORDS * 1.5:
                    pieces.append(("\n".join(head + part), True)); part, n = [], sum(len(l.split()) for l in head)
                part.append(r); n += w
            pieces.append(("\n".join(head + part), True))
        else:
            para = " ".join(lines)
            if len(para.split()) > CHUNK_WORDS * 1.5:
                pieces.extend((w, False) for w in _window_words(para))
            else:
                pieces.append((para, False))
    out, cur_t, n = [], [], 0
    for t, is_tbl in pieces:
        w = len(t.split())
        if cur_t and (n + w > CHUNK_WORDS or is_tbl):
            out.append("\n".join(cur_t)); cur_t, n = [], 0
        cur_t.append(t); n += w
        if is_tbl:
            out.append("\n".join(cur_t)); cur_t, n = [], 0
    if cur_t:
        out.append("\n".join(cur_t))
    return [c for c in out if len(tokenize(c)) >= 5]


def pretty_title(name):
    stem = Path(name).stem
    m = re.match(r"^\d+_(.*)$", stem)          # "01_2021-04_AO_Reliance..." (the original 10 SEBI files)
    if m:
        return m.group(1).replace("_", " ").replace("AO", "Adjudication Order", 1)
    return re.sub(r"[_\-]+", " ", stem).strip()


def safe_name(name):
    name = Path(name).name
    return re.sub(r"[^A-Za-z0-9 ._()\-]", "_", name)[:120] or "file"


# ------------------------------------------------------------------ text extraction + page images
def _font(size):
    from PIL import ImageFont
    for f in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def render_text_page(text, title, page_no, total, out_path, banner=None):
    """Draws a page of plain text as a white 'paper' image, so DOCX/TXT show in VR like a PDF page.
    banner: optional line shown in a green band (used by ProcOps for an approved revision)."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (IMG_W, IMG_H), "white")
    d = ImageDraw.Draw(img)
    head, body, foot = _font(26), _font(22), _font(18)
    margin, y, max_w = 70, 60, IMG_W - 140
    d.text((margin, y), title[:70], fill=(20, 40, 90), font=head)
    y += 46
    d.line((margin, y, IMG_W - margin, y), fill=(200, 200, 200), width=2)
    y += 24
    if banner:
        d.rectangle((margin, y, IMG_W - margin, y + 44), fill=(222, 245, 230), outline=(46, 125, 79), width=2)
        d.text((margin + 14, y + 10), banner[:80], fill=(30, 100, 60), font=_font(20))
        y += 64
    for para in text.split("\n"):
        line = ""
        for word in para.split():
            test = (line + " " + word).strip()
            if d.textlength(test, font=body) > max_w and line:
                d.text((margin, y), line, fill=(30, 30, 30), font=body)
                y += 31
                line = word
            else:
                line = test
            if y > IMG_H - 110:
                break
        if line and y <= IMG_H - 110:
            d.text((margin, y), line, fill=(30, 30, 30), font=body)
            y += 31
        y += 8
        if y > IMG_H - 110:
            break
    d.text((margin, IMG_H - 60), f"page {page_no} / {total}", fill=(120, 120, 120), font=foot)
    img.save(out_path, "JPEG", quality=75)


def split_words_into_pages(text):
    paras = [p.strip() for p in re.split(r"\n\s*\n|\r\n\s*\r\n", text) if p.strip()] or [text]
    pages, cur, n = [], [], 0
    for p in paras:
        w = len(p.split())
        if cur and n + w > WORDS_PER_TEXT_PAGE:
            pages.append("\n".join(cur)); cur, n = [], 0
        while w > WORDS_PER_TEXT_PAGE:                       # one very long paragraph
            words = p.split()
            pages.append(" ".join(words[:WORDS_PER_TEXT_PAGE]))
            p = " ".join(words[WORDS_PER_TEXT_PAGE:]); w = len(words) - WORDS_PER_TEXT_PAGE
        cur.append(p); n += w
    if cur:
        pages.append("\n".join(cur))
    return pages or [""]


def extract(path, ext, page_dir, title):
    """Returns the list of page texts and writes pNNN.jpg for every page."""
    page_dir.mkdir(parents=True, exist_ok=True)
    if ext == ".pdf":
        try:
            import pymupdf
        except ImportError as ex:        # e.g. Windows Application Control blocks PyMuPDF's DLL
            print(f"[index] PyMuPDF unavailable ({str(ex)[:80]}), using pypdfium2 (plain text, no tables)", flush=True)
            return _extract_pdf_pdfium(path, page_dir)
        texts = []
        with pymupdf.open(path) as doc:
            md_ok = True
            try:
                import pymupdf4llm                       # layout + table aware text (markdown tables)
            except Exception:
                md_ok = False
            for i, page in enumerate(doc):
                txt = None
                if md_ok:
                    try:
                        txt = pymupdf4llm.to_markdown(doc, pages=[i], show_progress=False, use_ocr=False) \
                            if _md_accepts_ocr() else pymupdf4llm.to_markdown(doc, pages=[i], show_progress=False)
                    except Exception as ex:
                        print(f"[index] markdown extraction failed on page {i + 1}, using plain text: {ex}", flush=True)
                if not txt or not txt.strip():
                    txt = page.get_text()
                texts.append(txt)
                zoom = IMG_W / page.rect.width
                page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(str(page_dir / f"p{i + 1:03d}.jpg"), jpg_quality=70)
        return texts
    if ext == ".docx":
        import docx
        d = docx.Document(str(path))
        parts = [p.text for p in d.paragraphs]
        for t in d.tables:                                   # include table text too
            for row in t.rows:
                parts.append(" | ".join(c.text.strip() for c in row.cells))
        text = "\n\n".join(p for p in parts if p.strip())
    else:  # .txt
        raw = path.read_bytes()
        text = next((raw.decode(enc) for enc in ("utf-8-sig", "utf-16", "cp1252") if _decodes(raw, enc)), raw.decode("latin-1"))
    pages = split_words_into_pages(text)
    for i, t in enumerate(pages):
        render_text_page(t, title, i + 1, len(pages), page_dir / f"p{i + 1:03d}.jpg")
    return pages


def _extract_pdf_pdfium(path, page_dir):
    """Fallback PDF reader (pypdfium2) for machines where PyMuPDF cannot load. Plain text, no table layout."""
    import pypdfium2 as pdfium
    texts = []
    pdf = pdfium.PdfDocument(str(path))
    try:
        for i in range(len(pdf)):
            page = pdf[i]
            tp = page.get_textpage()
            texts.append(tp.get_text_range().replace("\r\n", "\n"))
            tp.close()
            scale = IMG_W / page.get_width()
            page.render(scale=scale).to_pil().convert("RGB").save(page_dir / f"p{i + 1:03d}.jpg", "JPEG", quality=70)
            page.close()
    finally:
        pdf.close()
    return texts


_MD_OCR = {}


def _md_accepts_ocr():
    if "v" not in _MD_OCR:
        import inspect
        import pymupdf4llm
        _MD_OCR["v"] = "use_ocr" in inspect.signature(pymupdf4llm.to_markdown).parameters
    return _MD_OCR["v"]


def _decodes(raw, enc):
    try:
        raw.decode(enc)
        return True
    except UnicodeDecodeError:
        return False


# ------------------------------------------------------------------ the store
class Store:
    def __init__(self, root: Path, seed_dir: Path = None):
        self.root = root
        self.files, self.pages, self.emb = root / "files", root / "pages", root / "emb"
        for p in (self.files, self.pages, self.emb):
            p.mkdir(parents=True, exist_ok=True)
        self.manifest_path = root / "manifest.json"
        self.revs_dir = root / "revisions"          # ProcOps: approved page revisions, <uid>.json
        self.revs_dir.mkdir(parents=True, exist_ok=True)
        self.revs = {}
        for f in self.revs_dir.glob("*.json"):
            try:
                self.revs[f.stem] = json.loads(f.read_text("utf-8"))
            except Exception as ex:
                print(f"[procops] bad revision file {f.name}: {ex}", flush=True)
        self.lock = threading.RLock()
        self.queue = []
        self.wake = threading.Event()
        self.qdrant = None              # embedded QdrantClient, created lazily by _sync_qdrant
        self.embedder = None            # set by app.py once the embedding model is loaded
        self.model_name = ""
        self.snapshot = {"version": 0, "index": {"docs": [], "vocab": [], "idf": [], "chunks": []},
                         "vocab": {}, "matrix": None, "uids": []}
        self.state = "idle"
        self.manifest = json.loads(self.manifest_path.read_text("utf-8")) if self.manifest_path.exists() else []
        if not self.manifest and seed_dir and seed_dir.exists():       # first run: import the original docs/
            for f in sorted(seed_dir.iterdir()):
                if f.suffix.lower() in ALLOWED:
                    self._add(f.name, f.read_bytes(), seeded=True)
        for e in self.manifest:                                        # resume unfinished work after a restart
            if e["status"] == "processing":
                self.queue.append(e["uid"])
        self._save_manifest()
        threading.Thread(target=self._worker, daemon=True).start()
        self.request_rebuild()

    # ---------- public API (called by app.py)
    def list(self):
        with self.lock:
            return [dict(e) for e in self.manifest]

    def add(self, filename, data):
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED:
            raise ValueError(f"{filename}: only PDF, DOCX and TXT files are allowed")
        if len(data) > MAX_BYTES:
            raise ValueError(f"{filename}: larger than {MAX_BYTES // (1024 * 1024)} MB")
        if not data:
            raise ValueError(f"{filename}: empty file")
        digest = hashlib.sha256(data).hexdigest()
        with self.lock:
            dup = next((e for e in self.manifest if e.get("sha256") == digest), None)
        if dup:
            raise ValueError(f"{filename}: already uploaded as \"{dup['name']}\"")
        return self._add(filename, data)

    def delete(self, uid):
        with self.lock:
            e = next((x for x in self.manifest if x["uid"] == uid), None)
            if not e:
                return False
            self.manifest.remove(e)
            self._save_manifest()
        (self.files / f"{uid}{e['ext']}").unlink(missing_ok=True)
        shutil.rmtree(self.pages / uid, ignore_errors=True)
        for f in self.emb.glob(f"{uid}_*.npy"):
            f.unlink(missing_ok=True)
        with self.lock:
            self.revs.pop(uid, None)
        (self.revs_dir / f"{uid}.json").unlink(missing_ok=True)
        self.request_rebuild()
        return True

    def page_image(self, doc_pos, page):
        """Maps the VR app's positional URL d{doc}_p{page}.jpg to the stored image."""
        uids = self.snapshot["uids"]
        if 0 <= doc_pos < len(uids):
            return self.page_image_uid(uids[doc_pos], page)
        return None

    # ---------- ProcOps: versioned page revisions
    def page_image_uid(self, uid, page, original=False):
        rev = None if original else self.revs.get(uid, {}).get("pages", {}).get(str(page))
        if rev:
            p = self.pages / uid / rev["image"]
            if p.exists():
                return p
        p = self.pages / uid / f"p{page:03d}.jpg"
        return p if p.exists() else None

    def doc_entry(self, uid):
        with self.lock:
            e = next((x for x in self.manifest if x["uid"] == uid), None)
            return dict(e) if e else None

    def revisions(self, uid):
        with self.lock:
            r = self.revs.get(uid)
            return json.loads(json.dumps(r)) if r else {"version": "1.0", "pages": {}, "history": []}

    def page_text(self, uid, page, original=False):
        """Current text of a page (the approved revision if any)."""
        if not original:
            rev = self.revs.get(uid, {}).get("pages", {}).get(str(page))
            if rev:
                return rev["text"]
        f = self.pages / uid / "text.json"
        if not f.exists():
            return None
        texts = json.loads(f.read_text("utf-8"))
        return texts[page - 1] if 1 <= page <= len(texts) else None

    def add_revision(self, uid, page, text, summary="", approver="", evidence=0):
        e = self.doc_entry(uid)
        if not e or e["status"] != "ready":
            raise ValueError("document not found")
        if not (1 <= page <= e["pages"]):
            raise ValueError("page out of range")
        with self.lock:
            r = self.revisions(uid)
            major, minor = (int(x) for x in r["version"].split("."))
            version = f"{major}.{minor + 1}"
            n = len(r["history"]) + 1
            when = datetime.now(timezone.utc).isoformat(timespec="seconds")
            image = f"p{page:03d}_r{n}.jpg"
            render_text_page(text, e["title"], page, e["pages"], self.pages / uid / image,
                             banner=f"REVISED v{version} - approved {when[:10]}" + (f" by {approver}" if approver else ""))
            rec = {"n": n, "version": version, "page": page, "summary": summary[:500], "approver": approver[:80],
                   "approved": when, "evidence": int(evidence)}
            r["version"] = version
            r["pages"][str(page)] = {"text": text, "version": version, "image": image, "approved": when}
            r["history"].append(rec)
            self.revs[uid] = r
            tmp = self.revs_dir / f"{uid}.tmp"
            tmp.write_text(json.dumps(r, indent=1), "utf-8")
            tmp.replace(self.revs_dir / f"{uid}.json")
        self.request_rebuild()
        return rec

    def clear_revisions(self, uid=None):
        """Back to the original documents (demo reset). Revision files are archived, not deleted."""
        with self.lock:
            uids = [uid] if uid else list(self.revs)
            for u in uids:
                self.revs.pop(u, None)
                f = self.revs_dir / f"{u}.json"
                if f.exists():
                    f.replace(self.revs_dir / f"{u}.{int(time.time())}.bak")
        self.request_rebuild()
        return uids

    def request_rebuild(self):
        self.queue.append("__rebuild__")
        self.wake.set()

    # ---------- internals
    def _add(self, filename, data, seeded=False):
        uid = uuid.uuid4().hex[:12]
        ext = Path(filename).suffix.lower()
        name = safe_name(filename)
        (self.files / f"{uid}{ext}").write_bytes(data)
        entry = {"uid": uid, "name": name, "ext": ext, "type": ext[1:].upper(), "size": len(data),
                 "title": pretty_title(name), "pages": 0, "status": "processing", "error": "",
                 "uploaded": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seed": seeded,
                 "sha256": hashlib.sha256(data).hexdigest()}
        with self.lock:
            self.manifest.append(entry)
            self._save_manifest()
        self.queue.append(uid)
        self.wake.set()
        return entry

    def _save_manifest(self):
        tmp = self.manifest_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.manifest, indent=1), "utf-8")
        tmp.replace(self.manifest_path)

    def _worker(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            while self.queue:
                job = self.queue.pop(0)
                if job != "__rebuild__":
                    self._process(job)
            try:
                self.state = "indexing"
                self._rebuild()
            except Exception as ex:      # keep the old snapshot if a rebuild fails
                print(f"[index] rebuild failed: {ex}", flush=True)
            self.state = "idle"

    def _process(self, uid):
        with self.lock:
            e = next((x for x in self.manifest if x["uid"] == uid), None)
        if not e:
            return
        t0 = time.time()
        try:
            texts = extract(self.files / f"{uid}{e['ext']}", e["ext"], self.pages / uid, e["title"])
            (self.pages / uid / "text.json").write_text(json.dumps(texts), "utf-8")
            words = sum(len(t.split()) for t in texts)
            e.update(pages=len(texts), status="ready" if words else "error",
                     error="" if words else "No text found (scanned PDF? OCR is not supported yet)")
            print(f"[index] {e['name']}: {len(texts)} pages, {words} words in {time.time() - t0:.1f}s", flush=True)
        except Exception as ex:
            e.update(status="error", error=str(ex)[:300])
            print(f"[index] {e['name']} FAILED: {ex}", flush=True)
        with self.lock:
            self._save_manifest()

    def _rebuild(self):
        with self.lock:
            docs = [dict(e) for e in self.manifest if e["status"] == "ready"]
        index_docs, chunks, chunk_doc_uid = [], [], []
        for pos, e in enumerate(docs):
            texts = json.loads((self.pages / e["uid"] / "text.json").read_text("utf-8"))
            rev = self.revs.get(e["uid"], {})
            for pno, pr in rev.get("pages", {}).items():         # ProcOps: approved revisions replace the page
                if 1 <= int(pno) <= len(texts):
                    texts[int(pno) - 1] = pr["text"]
            e["_revn"] = len(rev.get("history", []))
            index_docs.append({"id": pos, "file": e["name"], "title": e["title"], "pages": len(texts),
                               "type": e["type"], "uid": e["uid"], "rev": rev.get("version", "1.0")})
            for pno, t in enumerate(texts, start=1):
                for c in chunk_page(t):
                    chunks.append({"doc": pos, "page": pno, "text": c})
                    chunk_doc_uid.append(e["uid"])
        # TF-IDF (identical maths to build_index.py)
        toks = [tokenize(c["text"]) for c in chunks]
        df = Counter()
        for t in toks:
            df.update(set(t))
        n = len(chunks)
        vocab = sorted(df)
        vidx = {t: i for i, t in enumerate(vocab)}
        idf = [round(math.log((n + 1) / (df[t] + 1)) + 1.0, 4) for t in vocab]
        for c, t in zip(chunks, toks):
            tf = Counter(t)
            vec = {vidx[w]: (1 + math.log(k)) * idf[vidx[w]] for w, k in tf.items()}
            norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
            items = sorted(vec.items())
            c["terms"] = [k for k, _ in items]
            c["weights"] = [round(v / norm, 4) for _, v in items]
        version = int(time.time() * 1000)
        index = {"version": version, "docs": index_docs, "vocab": vocab, "idf": idf, "chunks": chunks}
        ids, col, cname = self._sync_qdrant(docs, chunks, chunk_doc_uid)   # Qdrant vector store (created on first run)
        matrix = None if col is not None else self._embeddings(docs, chunks, chunk_doc_uid)   # .npy fallback
        (self.root / "index.json").write_text(json.dumps(index, ensure_ascii=False), "utf-8")
        with self.lock:
            self.snapshot = {"version": version, "index": index, "vocab": vidx, "matrix": matrix,
                             "uids": [d["uid"] for d in docs], "ids": ids, "idpos": {i: j for j, i in enumerate(ids or [])}, "qdrant": col, "qname": cname}
        print(f"[index] ready: {len(docs)} docs, {n} chunks, embeddings "
              f"{'qdrant' if col is not None else ('npy' if matrix is not None else 'no')}", flush=True)

    def _sync_qdrant(self, docs, chunks, chunk_doc_uid):
        """Keeps the persistent Qdrant collection (embedded mode, data/qdrant) in step with the current chunks.
        First run: the store + collection are created and everything is embedded. Later runs: chunks that are
        already stored are skipped (point ids are deterministic: uuid5 of uid + position + text hash, so nothing is
        duplicated), only new/changed chunks are embedded, and chunks of deleted/revised documents are removed.
        Returns (ids aligned with chunks, client, collection name) or (None, None, None) when unavailable."""
        if self.embedder is None or not chunks:
            return None, None, None
        try:
            from qdrant_client import QdrantClient, models
            name = "chunks_" + re.sub(r"[^A-Za-z0-9]+", "_", self.model_name).strip("_")[:80]
            if self.qdrant is None:
                self.qdrant = QdrantClient(path=str(self.root / "qdrant"))
                existed = self.qdrant.collection_exists(name)
                if not existed:
                    dim = len(next(iter(self.embedder.passage_embed(["dimension probe"]))))
                    self.qdrant.create_collection(name, vectors_config=models.VectorParams(
                        size=dim, distance=models.Distance.COSINE))
                print(f"[qdrant] {'using existing' if existed else 'created new'} collection '{name}' "
                      f"({self.qdrant.count(name).count} vectors) in {self.root / 'qdrant'}", flush=True)
            q = self.qdrant
            pos, ids = {}, []
            for c, u in zip(chunks, chunk_doc_uid):
                k = pos[u] = pos.get(u, -1) + 1
                ids.append(str(uuid.uuid5(uuid.NAMESPACE_URL, f"{u}:{k}:{hashlib.sha1(c['text'].encode('utf-8')).hexdigest()}")))
            have, offset = set(), None
            while True:
                pts, offset = q.scroll(name, limit=2000, offset=offset, with_payload=False, with_vectors=False)
                have.update(str(p.id) for p in pts)
                if offset is None:
                    break
            want = set(ids)
            stale = list(have - want)
            for i in range(0, len(stale), 2000):
                q.delete(name, points_selector=models.PointIdsList(points=stale[i:i + 2000]))
            todo = [(i, c) for i, c in zip(ids, chunks) if i not in have]
            for b in range(0, len(todo), 64):
                part = todo[b:b + 64]
                vecs = np.array(list(self.embedder.passage_embed([c["text"] for _, c in part], batch_size=32)), dtype=np.float32)
                vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
                q.upsert(name, points=[models.PointStruct(id=i, vector=v.tolist(), payload={"doc": c["doc"], "page": c["page"]})
                                       for (i, c), v in zip(part, vecs)])
            print(f"[qdrant] {len(want) - len(todo)} reused, {len(todo)} added, {len(stale)} removed", flush=True)
            return ids, q, name
        except Exception as ex:
            print(f"[qdrant] unavailable, using .npy embedding cache instead: {ex}", flush=True)
            return None, None, None

    def _embeddings(self, docs, chunks, chunk_doc_uid):
        """Per-document embedding cache: only new documents are embedded."""
        if self.embedder is None or not chunks:
            return None
        tag = self.model_name.replace("/", "_")
        parts = []
        for e in docs:
            texts = [c["text"] for c, u in zip(chunks, chunk_doc_uid) if u == e["uid"]]
            f = self.emb / (f"{e['uid']}_{tag}.npy" if not e.get("_revn") else f"{e['uid']}_{tag}_r{e['_revn']}.npy")
            m = np.load(f) if f.exists() else None
            if m is None or m.shape[0] != len(texts):
                m = np.array(list(self.embedder.passage_embed(texts, batch_size=32)), dtype=np.float32) if texts \
                    else np.zeros((0, 384), dtype=np.float32)
                if len(m):
                    m /= np.linalg.norm(m, axis=1, keepdims=True)
                np.save(f, m)
            parts.append(m)
        return np.vstack(parts) if parts else None
