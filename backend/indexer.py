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

from core.data.tenancy import DEFAULT_TENANT, current_tenant, doc_tenant
from core.knowledge.stopwords import load_stopwords   # generic words + corpus word files (backend/config/stopwords/)
STOP = load_stopwords(Path(__file__).resolve().parent.parent)


def tokenize(text):
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1 and t not in STOP]


def chunk_page(text):
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


def _page_figures(page, pno, page_dir):
    """Cuts tables, pictures and diagrams (vector drawings) out of one PDF page as their own small images,
    so small lens displays can show just the diagram / table instead of a whole A4 page."""
    import pymupdf
    area = page.rect.width * page.rect.height
    boxes = []
    # (page.find_tables() is accurate but ~0.2 s per page; ruled tables already show up as drawing clusters)
    try:
        for im in page.get_image_info():
            boxes.append(("image", pymupdf.Rect(im["bbox"])))
    except Exception:
        pass
    try:
        for r in page.cluster_drawings():
            boxes.append(("diagram", pymupdf.Rect(r)))
    except Exception:
        pass
    out, kept = [], []
    for kind, r in sorted(boxes, key=lambda b: -b[1].get_area()):
        r = r & page.rect
        a = r.get_area()
        if a < area * 0.03 or a > area * 0.92 or r.width < 60 or r.height < 40:
            continue                                            # too small (lines, logos) or the whole page
        if any((r & k).get_area() > 0.6 * a for k in kept):
            continue                                            # mostly inside a figure we already have
        kept.append(r)
        if kind == "diagram" and len(page.get_textbox(r).split()) > 25:
            kind = "table"                                      # ruled lines full of text = a table
        k = len(out) + 1
        clip = pymupdf.Rect(r.x0 - 6, r.y0 - 6, r.x1 + 6, r.y1 + 6) & page.rect
        zoom = min(3.0, 1100 / max(clip.width, 1))
        f = f"fig_p{pno:03d}_{k}.jpg"
        page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip).save(str(page_dir / f), jpg_quality=80)
        above = pymupdf.Rect(page.rect.x0, max(page.rect.y0, r.y0 - 40), page.rect.x1, r.y0)
        caption = " ".join(page.get_textbox(above).split())[:160] or " ".join(page.get_textbox(r).split())[:160]
        out.append({"page": pno, "k": k, "kind": kind, "file": f, "w": round(r.width), "h": round(r.height),
                    "caption": caption})
        if k >= 4:
            break
    return out


def extract(path, ext, page_dir, title):
    """Returns the list of page texts and writes pNNN.jpg for every page (+ figures.json for PDFs)."""
    page_dir.mkdir(parents=True, exist_ok=True)
    if ext == ".pdf":
        import pymupdf
        texts, figs = [], []
        with pymupdf.open(path) as doc:
            for i, page in enumerate(doc):
                texts.append(page.get_text())
                zoom = IMG_W / page.rect.width
                page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom)).save(str(page_dir / f"p{i + 1:03d}.jpg"), jpg_quality=70)
                try:
                    figs += _page_figures(page, i + 1, page_dir)
                except Exception as ex:
                    print(f"[figures] page {i + 1}: {ex}", flush=True)
        (page_dir / "figures.json").write_text(json.dumps(figs), "utf-8")
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


def _decodes(raw, enc):
    try:
        raw.decode(enc)
        return True
    except UnicodeDecodeError:
        return False


# ------------------------------------------------------------------ the store
class Store:
    def __init__(self, root: Path, seed_dir: Path = None, db=None):
        self.root = root
        self.db = db                      # db.DB (Postgres/Neon) or None = local files only
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
        self.embedder = None            # set by app.py once the embedding model is loaded
        self.model_name = ""
        self.snapshot = {"version": 0, "index": {"docs": [], "vocab": [], "idf": [], "chunks": []},
                         "vocab": {}, "matrix": None, "uids": []}
        self.state = "idle"
        self.manifest = json.loads(self.manifest_path.read_text("utf-8")) if self.manifest_path.exists() else []
        if self.db:
            self._sync_from_db()
        for e in self.manifest:                                        # older manifests: add the new fields
            e.setdefault("tags", []); e.setdefault("groups", [])
        if not self.manifest and seed_dir and seed_dir.exists():       # first run: import the original docs/
            for f in sorted(seed_dir.iterdir()):
                if f.suffix.lower() in ALLOWED:
                    self._add(f.name, f.read_bytes(), seeded=True)
        for e in self.manifest:                                        # resume unfinished work after a restart
            if e["status"] == "processing":
                if e["uid"] not in self.queue:
                    self.queue.append(e["uid"])
            elif e["status"] == "ready" and not (self.pages / e["uid"] / "p001.jpg").exists():
                self.queue.append("img:" + e["uid"])                   # page images lost (new server): redraw
            elif e["status"] == "ready" and e["ext"] == ".pdf" and not (self.pages / e["uid"] / "figures.json").exists():
                self.queue.append("fig:" + e["uid"])                   # older upload: cut out its diagrams/tables
        self._save_manifest()
        threading.Thread(target=self._worker, daemon=True).start()
        self.request_rebuild()

    # ---------- public API (called by app.py)
    def list(self):
        with self.lock:
            return [dict(e) for e in self.manifest]

    def add(self, filename, data, tags=None, groups=None, tenant_id=None):
        """tenant_id: the owner (default: the tenant of the current request, see core/data/tenancy.py)."""
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED:
            raise ValueError(f"{filename}: only PDF, DOCX and TXT files are allowed")
        if len(data) > MAX_BYTES:
            raise ValueError(f"{filename}: larger than {MAX_BYTES // (1024 * 1024)} MB")
        if not data:
            raise ValueError(f"{filename}: empty file")
        digest = hashlib.sha256(data).hexdigest()
        tenant_id = tenant_id or current_tenant()
        with self.lock:      # duplicates only WITHIN a tenant: never tell one customer about another's files
            dup = next((e for e in self.manifest if e.get("sha256") == digest and doc_tenant(e) == tenant_id), None)
        if dup:
            raise ValueError(f"{filename}: already uploaded as \"{dup['name']}\"")
        return self._add(filename, data, tags=tags, groups=groups, tenant_id=tenant_id)

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
        if self.db:
            self.db.delete_doc(uid)
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
            if self.db:
                self.db.put_revision(uid, r)
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
                if self.db:
                    self.db.delete_revision(u)
        self.request_rebuild()
        return uids

    # ---------- database sync (Neon / Postgres)
    def _sync_from_db(self):
        """The database is the source of truth: bring missing files/texts back onto this (possibly new) disk."""
        try:
            remote = self.db.list_docs()
        except Exception as ex:
            print(f"[db] cannot read documents, using local files: {ex}", flush=True)
            self.db = None
            return
        if not remote and self.manifest:                       # first time with a database: upload local docs
            print(f"[db] copying {len(self.manifest)} local documents into the database...", flush=True)
            for e in self.manifest:
                f = self.files / f"{e['uid']}{e['ext']}"
                if f.exists():
                    self.db.put_doc(e, f.read_bytes())
                    t = self.pages / e["uid"] / "text.json"
                    if t.exists():
                        self.db.put_texts(e["uid"], json.loads(t.read_text("utf-8")))
            for uid, r in self.revs.items():
                self.db.put_revision(uid, r)
            return
        if remote:
            self.manifest = remote
            for e in self.manifest:
                f = self.files / f"{e['uid']}{e['ext']}"
                if not f.exists():
                    data = self.db.doc_data(e["uid"])
                    if data:
                        f.write_bytes(data)
                t = self.pages / e["uid"] / "text.json"
                if e["status"] == "ready" and not t.exists():
                    texts = self.db.texts(e["uid"])
                    if texts is not None:
                        t.parent.mkdir(parents=True, exist_ok=True)
                        t.write_text(json.dumps(texts), "utf-8")
                    else:
                        e["status"] = "processing"
            self.revs = self.db.revisions() or self.revs
            print(f"[db] {len(self.manifest)} documents loaded from the database", flush=True)

    def update_meta(self, uid, **fields):
        """Change title / tags (machine ids) / groups (who may see it) of a document."""
        with self.lock:
            e = next((x for x in self.manifest if x["uid"] == uid), None)
            if not e:
                return None
            for k in ("title", "tags", "groups"):
                if fields.get(k) is not None:
                    e[k] = fields[k]
            self._save_manifest()
            out = dict(e)
        if self.db:
            self.db.put_doc(out)
        self.request_rebuild()
        return out

    def figures(self, uid):
        f = self.pages / uid / "figures.json"
        try:
            return json.loads(f.read_text("utf-8")) if f.exists() else []
        except ValueError:
            return []

    def figure_image(self, uid, file):
        p = self.pages / uid / file
        return p if re.fullmatch(r"fig_p\d{3}_\d+\.jpg", file) and p.exists() else None

    def request_rebuild(self):
        self.queue.append("__rebuild__")
        self.wake.set()

    # ---------- internals
    def _add(self, filename, data, seeded=False, tags=None, groups=None, tenant_id=DEFAULT_TENANT):
        uid = uuid.uuid4().hex[:12]
        ext = Path(filename).suffix.lower()
        name = safe_name(filename)
        (self.files / f"{uid}{ext}").write_bytes(data)
        entry = {"uid": uid, "name": name, "ext": ext, "type": ext[1:].upper(), "size": len(data),
                 "title": pretty_title(name), "pages": 0, "status": "processing", "error": "",
                 "uploaded": datetime.now(timezone.utc).isoformat(timespec="seconds"), "seed": seeded,
                 "sha256": hashlib.sha256(data).hexdigest(), "tenant_id": tenant_id}
        entry.update(tags=list(tags or []), groups=list(groups or []))     # set before indexing: never briefly public
        with self.lock:
            self.manifest.append(entry)
            self._save_manifest()
        if self.db:
            self.db.put_doc(entry, data)
        if uid not in self.queue:
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
                try:                                  # one failing job (e.g. database offline) must not kill the worker
                    if job.startswith("img:"):
                        self._process(job[4:], images_only=True)
                    elif job.startswith("fig:"):
                        self._figures_only(job[4:])
                    elif job != "__rebuild__":
                        self._process(job)
                except Exception as ex:
                    print(f"[index] job {job} failed: {ex}", flush=True)
            try:
                self.state = "indexing"
                self._rebuild()
            except Exception as ex:      # keep the old snapshot if a rebuild fails
                print(f"[index] rebuild failed: {ex}", flush=True)
            self.state = "idle"

    def _figures_only(self, uid):
        e = self.doc_entry(uid)
        if not e:
            return
        try:
            import pymupdf
            figs = []
            with pymupdf.open(self.files / f"{uid}{e['ext']}") as doc:
                for i, page in enumerate(doc):
                    figs += _page_figures(page, i + 1, self.pages / uid)
            (self.pages / uid / "figures.json").write_text(json.dumps(figs), "utf-8")
            print(f"[figures] {e['name']}: {len(figs)} diagrams/tables cut out", flush=True)
        except Exception as ex:
            print(f"[figures] {e['name']}: {ex}", flush=True)

    def _process(self, uid, images_only=False):
        with self.lock:
            e = next((x for x in self.manifest if x["uid"] == uid), None)
        if not e:
            return
        t0 = time.time()
        if images_only:                                   # text already known; only page images were missing
            try:
                extract(self.files / f"{uid}{e['ext']}", e["ext"], self.pages / uid, e["title"])
                print(f"[index] {e['name']}: page images redrawn in {time.time() - t0:.1f}s", flush=True)
            except Exception as ex:
                print(f"[index] {e['name']}: redraw failed: {ex}", flush=True)
            return
        try:
            texts = extract(self.files / f"{uid}{e['ext']}", e["ext"], self.pages / uid, e["title"])
            (self.pages / uid / "text.json").write_text(json.dumps(texts), "utf-8")
            words = sum(len(t.split()) for t in texts)
            e.update(pages=len(texts), status="ready" if words else "error",
                     error="" if words else "No text found (scanned PDF? OCR is not supported yet)")
            print(f"[index] {e['name']}: {len(texts)} pages, {words} words in {time.time() - t0:.1f}s", flush=True)
            if self.db and words:
                self.db.put_texts(uid, texts)
        except Exception as ex:
            e.update(status="error", error=str(ex)[:300])
            print(f"[index] {e['name']} FAILED: {ex}", flush=True)
        with self.lock:
            if e not in self.manifest:                    # deleted while it was being read: do not bring it back
                return
            self._save_manifest()
        if self.db:
            try:
                self.db.put_doc(dict(e))
            except Exception as ex:
                print(f"[db] could not save {e['name']}: {ex}", flush=True)

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
            figs = {}
            for fg in self.figures(e["uid"]):
                figs.setdefault(str(fg["page"]), []).append({"k": fg["k"], "kind": fg["kind"], "file": fg["file"],
                                                             "caption": fg["caption"], "w": fg["w"], "h": fg["h"]})
            index_docs.append({"id": pos, "file": e["name"], "title": e["title"], "pages": len(texts),
                               "type": e["type"], "uid": e["uid"], "rev": rev.get("version", "1.0"),
                               "tags": e.get("tags", []), "groups": e.get("groups", []), "figs": figs,
                               "tenant_id": doc_tenant(e)})
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
        matrix = self._embeddings(docs, chunks, chunk_doc_uid)
        (self.root / "index.json").write_text(json.dumps(index, ensure_ascii=False), "utf-8")
        with self.lock:
            keys, seen = [], {}
            for u in chunk_doc_uid:                       # (uid, n-th chunk of that doc) <-> pgvector rows
                keys.append((u, seen.get(u, 0))); seen[u] = seen.get(u, 0) + 1
            self.snapshot = {"version": version, "index": index, "vocab": vidx, "matrix": matrix,
                             "uids": [d["uid"] for d in docs], "keys": keys,
                             "keyidx": {k: i for i, k in enumerate(keys)}}
        print(f"[index] ready: {len(docs)} docs, {n} chunks, embeddings {'yes' if matrix is not None else 'no'}", flush=True)

    def _embeddings(self, docs, chunks, chunk_doc_uid):
        """Per-document embedding cache: only new documents are embedded."""
        if self.embedder is None or not chunks:
            return None
        tag = self.model_name.replace("/", "_")
        parts = []
        for e in docs:
            mine = [(c["page"], c["text"]) for c, u in zip(chunks, chunk_doc_uid) if u == e["uid"]]
            texts = [t for _, t in mine]
            sig = hashlib.sha1("\x00".join(texts).encode("utf-8")).hexdigest()   # changes when a page is revised
            f = self.emb / (f"{e['uid']}_{tag}.npy" if not e.get("_revn") else f"{e['uid']}_{tag}_r{e['_revn']}.npy")
            m = np.load(f) if f.exists() else None
            if (m is None or m.shape[0] != len(texts)) and self.db:      # another server already embedded it
                try:
                    if self.db.kv_get("embsig", f"{e['uid']}:{tag}") == sig:
                        m = self.db.get_matrix(e["uid"], self.model_name, len(texts))
                except Exception as ex:
                    print(f"[db] embeddings read failed: {ex}", flush=True)
            if m is None or m.shape[0] != len(texts):
                m = np.array(list(self.embedder.passage_embed(texts, batch_size=32)), dtype=np.float32) if texts \
                    else np.zeros((0, 384), dtype=np.float32)
                if len(m):
                    m /= np.linalg.norm(m, axis=1, keepdims=True)
                if self.db:
                    try:
                        self.db.put_chunks(e["uid"], self.model_name, mine, m)
                        self.db.kv_put("embsig", f"{e['uid']}:{tag}", sig)
                    except Exception as ex:
                        print(f"[db] embeddings write failed: {ex}", flush=True)
            elif self.db:
                try:                                      # make sure pgvector has this document's chunks
                    if self.db.kv_get("embsig", f"{e['uid']}:{tag}") != sig:
                        self.db.put_chunks(e["uid"], self.model_name, mine, m)
                        self.db.kv_put("embsig", f"{e['uid']}:{tag}", sig)
                except Exception as ex:
                    print(f"[db] embeddings sync failed: {ex}", flush=True)
            np.save(f, m)
            parts.append(m)
        return np.vstack(parts) if parts else None
