"""SearchEngine - hybrid document search: TF-IDF (exact words) + embeddings (meaning), merged with RRF.

RRF = Reciprocal Rank Fusion: each method ranks the passages; a passage's score is the sum of 1 / (60 + rank) from
each ranking, so a passage that both methods like rises to the top, without comparing their raw scores.

Phase 3 moves this code out of app.py unchanged in behaviour. The sector-specific "machine" filter becomes a
generic TAG filter here; app.py maps it back to the old response fields.

Dependencies are passed in (ADR 0001 - core/ imports no old backend modules):
    store       has .snapshot (the current index), .model_name, .embedder, .request_rebuild()
    tokenize    text -> list of index terms (the same function the index was built with)
    can_see     (doc_groups, user) -> bool  (document permissions)
    db          optional, has .semantic(query_vector, model, limit, uids) -> [(uid, ordinal, score)]  (pgvector)
"""
import math
import re
import time
from collections import Counter
from typing import Callable, Optional

import numpy as np

RRF_K = 60              # standard RRF constant
W_SEMANTIC = 1.0        # weight of the embedding ranking
W_KEYWORD = 0.8         # weight of the TF-IDF ranking (exact terms and numbers)
LOW_CONFIDENCE = 0.62   # best semantic match below this = "low confidence" (bge-small cosine similarity)


def norm_code(x) -> str:
    """'PUMP-7731 ' -> 'pump7731': tags compare without case, spaces or punctuation."""
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def ranks(scores):
    order = np.argsort(-scores)
    r = np.empty(len(scores), dtype=np.int32)
    r[order] = np.arange(1, len(scores) + 1)
    return r


def tfidf_scores(snap, query, tokenize):
    vocab, idf, chunks = snap["vocab"], snap["index"]["idf"], snap["index"]["chunks"]
    counts = Counter(t for t in tokenize(query) if t in vocab)
    out = np.zeros(len(chunks), dtype=np.float32)
    if not counts:
        return out
    q = {vocab[t]: (1 + math.log(c)) * idf[vocab[t]] for t, c in counts.items()}
    norm = math.sqrt(sum(v * v for v in q.values()))
    for i, ch in enumerate(chunks):
        s = 0.0
        for t, w in zip(ch["terms"], ch["weights"]):
            qw = q.get(t)
            if qw:
                s += qw * w
        out[i] = s / norm
    return out


def tag_positions(snap, tag) -> list:
    """Documents whose tags match `tag`: exact match, or (4+ characters) one contains the other."""
    m = norm_code(tag)
    if not m:
        return []
    return [d["id"] for d in snap["index"]["docs"]
            if any(norm_code(t) and (norm_code(t) == m or (len(m) >= 4 and (m in norm_code(t) or norm_code(t) in m)))
                   for t in d.get("tags", []))]


def _hit(snap, i, score, kwv, semv):
    c = snap["index"]["chunks"][i]
    d = snap["index"]["docs"][c["doc"]]
    return {"i": i, "score": score, "kw": kwv, "sem": semv,          # the passage itself, from the SAME snapshot
            "doc": c["doc"], "page": c["page"], "uid": d["uid"], "title": d["title"], "text": c["text"]}


class SearchEngine:
    def __init__(self, store, tokenize: Callable, can_see: Callable, embed_model: str, db=None,
                 rrf_k: int = RRF_K, w_semantic: float = W_SEMANTIC, w_keyword: float = W_KEYWORD,
                 low_confidence: float = LOW_CONFIDENCE):
        self.store, self.tokenize, self.can_see, self.embed_model, self.db = store, tokenize, can_see, embed_model, db
        self.rrf_k, self.w_semantic, self.w_keyword, self.low_confidence = rrf_k, w_semantic, w_keyword, low_confidence
        self.emb = {"state": "warming", "model": None, "error": None}       # embedding model, loaded in background

    # ------------------------------------------------------------ embeddings
    def load_embeddings(self, factory: Optional[Callable] = None) -> None:
        """Load the embedding model (slow: ~70 MB download the first time). Search works on TF-IDF until then.
        `factory(model_name)` builds the model; default fastembed.TextEmbedding."""
        if self.emb.get("state") == "ready":
            return
        t0 = time.time()
        try:
            if factory is None:
                from fastembed import TextEmbedding as factory
            model = factory(self.embed_model)
            self.emb.update(state="ready", model=model)
            self.store.model_name, self.store.embedder = self.embed_model, model
            self.store.request_rebuild()                     # embeds any document not cached yet
            print(f"[embeddings] model ready in {time.time() - t0:.1f}s", flush=True)
        except Exception as e:  # keep serving with TF-IDF only
            self.emb.update(state="failed", error=str(e))
            print(f"[embeddings] FAILED, using keyword search only: {e}", flush=True)

    # ------------------------------------------------------------ search
    def visible_positions(self, snap, user) -> list:
        return [d["id"] for d in snap["index"]["docs"] if self.can_see(d.get("groups", []), user)]

    def search(self, query: str, k: int = 8, tag: str = "", user=None) -> dict:
        """Hybrid search. Respects document permissions; `tag` narrows to tagged documents when any match.
        Returns {version, hits, tag, tagMatched, conf, mode, ...}."""
        snap = self.store.snapshot                             # one consistent version of the index
        chunks = snap["index"]["chunks"]
        n = len(chunks)
        out = {"version": snap["version"], "hits": [], "tag": tag or "", "tagMatched": None, "conf": None}
        if not query or not n:
            return dict(out, mode="none")
        allowed = set(self.visible_positions(snap, user))
        if tag:
            tp = set(tag_positions(snap, tag)) & allowed
            out["tagMatched"] = bool(tp)
            if tp:
                allowed = tp
        doc_of = np.fromiter((c["doc"] for c in chunks), dtype=np.int32, count=n)
        mask = np.isin(doc_of, np.fromiter(allowed, dtype=np.int32)) if allowed else np.zeros(n, bool)
        kw = tfidf_scores(snap, query, self.tokenize)
        kw[~mask] = 0
        matrix = snap["matrix"]
        if self.emb["state"] != "ready" or matrix is None or matrix.shape[0] != n:
            order = [int(i) for i in np.argsort(-kw)[:k] if kw[i] > 0]
            return dict(out, mode="keyword", embeddings=self.emb["state"],
                        hits=[_hit(snap, i, float(kw[i]), float(kw[i]), None) for i in order])
        q = np.array(list(self.emb["model"].query_embed([query]))[0], dtype=np.float32)
        q /= np.linalg.norm(q)
        engine = "memory"
        sem = None
        if self.db is not None:                                # semantic search inside Postgres (pgvector)
            try:
                uids = [snap["uids"][p] for p in allowed]
                sem = np.full(n, -1.0, dtype=np.float32)
                for uid, ordn, sc in self.db.semantic(q, self.embed_model, max(60, k * 6), uids):
                    i = snap["keyidx"].get((uid, ordn))
                    if i is not None:
                        sem[i] = sc
                engine = "pgvector"
            except Exception as e:
                print(f"[db] pgvector search failed, using memory: {e}", flush=True)
                sem = None
        if sem is None:
            sem = matrix @ q
        sem[~mask] = -1.0
        rrf = self.w_semantic / (self.rrf_k + ranks(sem)) + self.w_keyword * (kw > 0) / (self.rrf_k + ranks(kw))
        rrf[~mask] = -1.0
        order = [int(i) for i in np.argsort(-rrf)[:k] if rrf[i] > 0]
        conf = float(max((sem[i] for i in order), default=0.0))
        return dict(out, mode="hybrid", engine=engine, model=self.embed_model, conf=round(conf, 3),
                    lowConfidence=conf < self.low_confidence,
                    hits=[_hit(snap, i, float(rrf[i]), float(kw[i]), float(sem[i])) for i in order])
