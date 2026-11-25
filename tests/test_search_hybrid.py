"""Hybrid search (TF-IDF + embeddings + RRF) with a deterministic fake embedding model.

Before Phase 3 step 3 no test reached the hybrid path (the real model is never loaded in tests). The golden file
tests/golden/search_hybrid.json was generated with the OLD app.py code and is now checked against core/knowledge.
"""
import hashlib
import re
import time

import numpy as np
import pytest

import app as backend
from indexer import tokenize
from test_characterization import QUERIES, golden

DIM = 64


class FakeEmbedder:
    """Hashing-trick vectors: same words -> similar vectors. Deterministic, no download."""

    @staticmethod
    def _vec(text):
        v = np.zeros(DIM, dtype=np.float32)
        for w in re.findall(r"[a-z0-9]+", text.lower()):
            h = int(hashlib.md5(w.encode()).hexdigest(), 16)
            v[h % DIM] += 1.0 if (h >> 8) % 2 else -1.0
        n = np.linalg.norm(v)
        return v / n if n else v + 1.0 / np.sqrt(DIM)

    def passage_embed(self, texts, batch_size=32):
        return (self._vec(t) for t in texts)

    def query_embed(self, texts):
        return (self._vec(t) for t in texts)

    def __init__(self, name="fake-embed"):
        self.name = name


def _wait(cond, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError("index did not rebuild in time")


@pytest.fixture(scope="module")
def hybrid():
    store, emb = backend.STORE, backend.EMB
    saved = (store.embedder, store.model_name, dict(emb))
    model = FakeEmbedder()
    emb.update(state="ready", model=model, error=None)
    store.model_name, store.embedder = "fake-embed", model
    store.request_rebuild()
    _wait(lambda: store.snapshot["matrix"] is not None
          and store.snapshot["matrix"].shape[0] == len(store.snapshot["index"]["chunks"]) and store.state == "idle")
    yield store
    store.embedder, store.model_name = saved[0], saved[1]
    emb.clear()
    emb.update(saved[2])


def test_hybrid_results_golden(hybrid):
    out = {}
    for q in QUERIES:
        r = backend.run_search(q, 5)
        assert r["mode"] == "hybrid" and r["engine"] == "memory"
        out[q] = {"conf": r["conf"], "low": r["lowConfidence"],
                  "hits": [{"title": h["title"], "page": h["page"], "score": round(h["score"], 5),
                            "kw": round(h["kw"], 4), "sem": round(h["sem"], 4)} for h in r["hits"]]}
    golden("search_hybrid", out)


def test_hybrid_shape(hybrid):
    r = backend.run_search("penalty", 3)
    assert set(r) == {"version", "hits", "machine", "machineMatched", "conf", "mode", "engine", "model", "lowConfidence"}
    assert list(r)[:5] == ["version", "hits", "machine", "machineMatched", "conf"]        # same field order as before
    assert len(r["hits"]) == 3 and all(h["sem"] is not None for h in r["hits"])


def test_hybrid_respects_permissions(hybrid):
    store = backend.STORE
    assert backend.run_search("penalty", 5)["hits"]                        # normally visible
    blocked = lambda groups, user: False                                   # noqa: E731  nobody sees anything
    from core.knowledge import SearchEngine
    eng = SearchEngine(store, tokenize, blocked, "fake-embed")
    eng.emb.update(state="ready", model=FakeEmbedder())
    assert eng.search("penalty", 5)["hits"] == []


def test_hybrid_pgvector_path(hybrid):
    """With a database, semantic scores come from pgvector (here: a fake that scores one passage)."""
    from core.knowledge import SearchEngine
    snap = backend.STORE.snapshot
    target = snap["index"]["chunks"][7]
    uid = snap["index"]["docs"][target["doc"]]["uid"]
    key = next(k for k, i in snap["keyidx"].items() if i == 7)

    class FakeDb:
        def __init__(self):
            self.calls = []

        def semantic(self, q, model, limit, uids):
            self.calls.append((model, limit, len(uids)))
            return [(key[0], key[1], 0.99)]

    db = FakeDb()
    eng = SearchEngine(backend.STORE, tokenize, lambda g, u: True, "fake-embed", db=db)
    eng.emb.update(state="ready", model=FakeEmbedder())
    r = eng.search("zzqq xxyyw", 4)                          # no indexed words: only pgvector scores count
    assert r["engine"] == "pgvector" and db.calls == [("fake-embed", 60, len(snap["index"]["docs"]))]
    assert r["hits"][0]["i"] == 7 and r["hits"][0]["uid"] == uid and r["conf"] == 0.99 and not r["lowConfidence"]

    class BrokenDb:
        def semantic(self, *a):
            raise RuntimeError("db down")
    eng.db = BrokenDb()
    assert eng.search("penalty", 3)["engine"] == "memory"                  # falls back to in-memory vectors


def test_low_confidence_threshold(hybrid):
    from core.knowledge import SearchEngine
    eng = SearchEngine(backend.STORE, tokenize, lambda g, u: True, "fake-embed", low_confidence=1.01)
    eng.emb.update(state="ready", model=FakeEmbedder())
    assert eng.search("penalty", 3)["lowConfidence"] is True


def test_load_embeddings_success_and_failure():
    from core.knowledge import SearchEngine

    class FakeStore:
        snapshot = backend.STORE.snapshot
        model_name, embedder, rebuilds = "", None, 0

        def request_rebuild(self):
            self.rebuilds += 1
    st = FakeStore()
    eng = SearchEngine(st, tokenize, lambda g, u: True, "m1")
    eng.load_embeddings(factory=lambda name: FakeEmbedder(name))
    assert eng.emb["state"] == "ready" and st.model_name == "m1" and st.rebuilds == 1
    eng.load_embeddings(factory=lambda name: 1 / 0)                         # already ready: no reload
    assert st.rebuilds == 1
    bad = SearchEngine(FakeStore(), tokenize, lambda g, u: True, "m2")

    def boom(name):
        raise OSError("no internet")
    bad.load_embeddings(factory=boom)
    assert bad.emb["state"] == "failed" and "no internet" in bad.emb["error"]
