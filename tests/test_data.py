"""Tests for core.data (Phase 3 step 4).

The KV contract runs against every backend:
  - LocalKV (local JSON files): always.
  - Postgres (DB): only when VR_RAG_TEST_DATABASE_URL points at a THROW-AWAY database with pgvector, e.g. a Neon
    test branch. Never point it at the app's real database: the tests create tables and write rows.

    PowerShell:  $env:VR_RAG_TEST_DATABASE_URL = "postgresql://...neon-test-branch..."; python -m pytest -q tests/test_data.py
"""
import os
import threading
import time
import uuid
from pathlib import Path

import numpy as np
import pytest

from core.data import DB, SCHEMA, KeyValueStore, LocalKV, database_url, vec_literal

ROOT = Path(__file__).resolve().parent.parent
TEST_DB_URL = os.environ.get("VR_RAG_TEST_DATABASE_URL", "").strip()
needs_db = pytest.mark.skipif(not TEST_DB_URL, reason="set VR_RAG_TEST_DATABASE_URL to a throw-away Postgres to run")


@pytest.fixture(scope="module")
def pg():
    if not TEST_DB_URL:
        pytest.skip("no VR_RAG_TEST_DATABASE_URL")
    real = os.environ.get("DATABASE_URL", "").strip() or database_url(ROOT)
    if real and real == TEST_DB_URL:
        pytest.fail("VR_RAG_TEST_DATABASE_URL is the app's real database - use a throw-away one (e.g. a Neon branch)")
    return DB(TEST_DB_URL)


@pytest.fixture(params=["local", pytest.param("postgres", marks=needs_db)])
def kv(request, tmp_path):
    if request.param == "local":
        return LocalKV(tmp_path)
    return request.getfixturevalue("pg")


def _kind():
    return "test_" + uuid.uuid4().hex[:10]          # unique kind: tests never touch real records


# ---------------------------------------------------------------- KV contract (every backend)
def test_kv_is_a_key_value_store(kv):
    assert isinstance(kv, KeyValueStore)


def test_kv_put_get_overwrite_delete(kv):
    k = _kind()
    assert kv.kv_get(k, "a") is None
    kv.kv_put(k, "a", {"n": 1, "text": "नमस्ते ₹ ’"})
    assert kv.kv_get(k, "a") == {"n": 1, "text": "नमस्ते ₹ ’"}
    kv.kv_put(k, "a", {"n": 2, "nested": {"list": [1, 2, None]}})
    assert kv.kv_get(k, "a") == {"n": 2, "nested": {"list": [1, 2, None]}}
    kv.kv_delete(k, "a")
    kv.kv_delete(k, "a")                              # deleting twice is not an error
    assert kv.kv_get(k, "a") is None and kv.kv_list(k) == []


def test_kv_ids_are_strings(kv):
    k = _kind()
    kv.kv_put(k, 42, {"v": "x"})
    assert kv.kv_get(k, "42") == {"v": "x"} and kv.kv_get(k, 42) == {"v": "x"}
    kv.kv_delete(k, "42")


def test_kv_list_newest_first_and_kinds_separate(kv):
    k, other = _kind(), _kind()
    for i in range(3):
        kv.kv_put(k, f"id{i}", {"i": i})
        time.sleep(0.03)                              # Windows clocks tick every ~15 ms
    kv.kv_put(other, "id0", {"other": True})
    assert [r["i"] for r in kv.kv_list(k)] == [2, 1, 0]
    time.sleep(0.03)
    kv.kv_put(k, "id0", {"i": 0, "again": True})      # an update moves it to the front
    assert kv.kv_list(k)[0] == {"i": 0, "again": True}
    assert kv.kv_list(other) == [{"other": True}]
    for i in range(3):
        kv.kv_delete(k, f"id{i}")
    kv.kv_delete(other, "id0")


# ---------------------------------------------------------------- LocalKV specifics
def test_local_kv_persists_and_survives_bad_file(tmp_path):
    a = LocalKV(tmp_path)
    a.kv_put("note", "1", {"t": "keep me"})
    assert LocalKV(tmp_path).kv_get("note", "1") == {"t": "keep me"}          # new instance reads the file
    assert (tmp_path / "app_data" / "note.json").exists() and not (tmp_path / "app_data" / "note.tmp").exists()
    (tmp_path / "app_data" / "broken.json").write_text("{not json", "utf-8")
    assert a.kv_list("broken") == [] and a.kv_get("broken", "x") is None


def test_local_kv_parallel_writes_lose_nothing(tmp_path):
    kv = LocalKV(tmp_path)
    ts = [threading.Thread(target=lambda i=i: kv.kv_put("pin", f"p{i}", {"i": i})) for i in range(20)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(r["i"] for r in kv.kv_list("pin")) == list(range(20))


# ---------------------------------------------------------------- helpers
def test_database_url_env_then_file(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert database_url(tmp_path) is None
    (tmp_path / "database_url.txt").write_text("﻿# my Neon url\n\npostgresql://file/db\nignored\n", "utf-8")
    assert database_url(tmp_path) == "postgresql://file/db"                    # BOM + comment + blank skipped
    monkeypatch.setenv("DATABASE_URL", "  postgresql://env/db ")
    assert database_url(tmp_path) == "postgresql://env/db"


def test_vec_literal_and_schema():
    assert vec_literal([1, 0.5, -0.1234567]) == "[1.000000,0.500000,-0.123457]"
    sql = SCHEMA.replace("%(dim)s", "384")
    assert "vector(384)" in sql and "CREATE EXTENSION IF NOT EXISTS vector" in sql
    for table in ("docs", "doc_texts", "chunks", "revisions", "events", "app_data"):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql


def test_db_shim_still_works():
    import db
    assert db.DB is DB and db.LocalKV is LocalKV and db.database_url is database_url


# ---------------------------------------------------------------- Postgres only (documents, vectors, revisions)
@needs_db
def test_pg_documents_texts_revisions(pg):
    uid = "test_" + uuid.uuid4().hex[:10]
    try:
        pg.put_doc({"uid": uid, "name": "a.txt", "uploaded": "2026-01-01"}, b"\x00bytes")
        assert pg.doc_data(uid) == b"\x00bytes" and any(m["uid"] == uid for m in pg.list_docs())
        pg.put_doc({"uid": uid, "name": "renamed.txt", "uploaded": "2026-01-01"})          # meta only, data kept
        assert pg.doc_data(uid) == b"\x00bytes" and next(m for m in pg.list_docs() if m["uid"] == uid)["name"] == "renamed.txt"
        pg.put_texts(uid, ["page one", "page two"])
        assert pg.texts(uid) == ["page one", "page two"]
        pg.put_revision(uid, {"pages": {"1": "v2"}})
        assert pg.revisions()[uid] == {"pages": {"1": "v2"}}
    finally:
        pg.delete_doc(uid)
    assert pg.doc_data(uid) is None and pg.texts(uid) is None and uid not in pg.revisions()   # cascade + cleanup


@needs_db
def test_pg_chunks_and_semantic_search(pg):
    uid = "test_" + uuid.uuid4().hex[:10]
    rng = np.random.default_rng(7)
    m = rng.normal(size=(5, pg.dim)).astype(np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    try:
        pg.put_doc({"uid": uid, "name": "v.txt", "uploaded": "2026-01-01"})
        pg.put_chunks(uid, "test-model", [(i + 1, f"chunk {i}") for i in range(5)], m)
        assert pg.chunk_count(uid, "test-model") == 5
        back = pg.get_matrix(uid, "test-model", 5)
        assert back.shape == (5, pg.dim) and np.allclose(back, m, atol=1e-5)
        assert pg.get_matrix(uid, "test-model", 6) is None                               # wrong count -> rebuild
        hits = pg.semantic(m[3], "test-model", 2, [uid])
        assert hits[0][:2] == (uid, 3) and hits[0][2] == pytest.approx(1.0, abs=1e-4) and len(hits) == 2
        assert pg.semantic(m[3], "test-model", 2, []) == []
    finally:
        pg.delete_doc(uid)
    assert pg.chunk_count(uid, "test-model") == 0


# ---------------------------------------------------------------- Phase 4 part 2b: migrations + tenants on Postgres
@needs_db
def test_pg_migrations_applied_once(pg):
    from core.data import migrations
    rows = pg.q("SELECT version FROM schema_migrations ORDER BY version")
    assert [r[0] for r in rows] == [m[0] for m in migrations.MIGRATIONS]
    assert migrations.run(pg.conn, log=lambda *_: None) == []                 # second run: nothing to do
    pk = pg.q("SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
              "WHERE i.indrelid = 'app_data'::regclass AND i.indisprimary")
    assert {r[0] for r in pk} == {"tenant_id", "kind", "id"}


@needs_db
def test_pg_kv_and_events_are_per_tenant(pg):
    from core.data.tenancy import use_tenant
    k = _kind()
    try:
        pg.kv_put(k, "same-id", {"who": "default"})
        with use_tenant("acme-test"):
            assert pg.kv_get(k, "same-id") is None and pg.kv_list(k) == []
            pg.kv_put(k, "same-id", {"who": "acme"})                            # same kind + id, other tenant: no clash
            assert pg.kv_get(k, "same-id") == {"who": "acme"}
            marker = "test_" + uuid.uuid4().hex
            pg.add_event({"q": marker})
            assert any(e.get("q") == marker for e in pg.events())
        assert pg.kv_get(k, "same-id") == {"who": "default"}
        assert not any(e.get("q") == marker for e in pg.events())             # default never sees acme's log
    finally:
        pg.kv_delete(k, "same-id")
        with use_tenant("acme-test"):
            pg.kv_delete(k, "same-id")
            pg.x("DELETE FROM events WHERE tenant_id = %s", ("acme-test",))


def test_pg_journal_streams_are_per_tenant(pg):
    """Phase 6: module journals live in `events` with a stream name (migration 2)."""
    from core.data import PgJournal
    from core.data.tenancy import use_tenant
    j, s1, s2 = PgJournal(pg), f"test_{uuid.uuid4().hex[:8]}.questions", f"test_{uuid.uuid4().hex[:8]}.other"
    try:
        j.append(s1, {"q": "default q"})
        j.append(s2, {"q": "other stream"})
        with use_tenant("acme-test"):
            j.append(s1, {"q": "acme q"})
            assert [e["q"] for e in j.list(s1)] == ["acme q"]
            j.archive(s1)
            assert j.list(s1) == []
        assert [e["q"] for e in j.list(s1)] == ["default q"]                   # acme's archive did not touch it
        assert [e["q"] for e in j.list(s2)] == ["other stream"]                 # streams are separate
        with pytest.raises(ValueError):
            j.append("no-dot", {})
    finally:
        pg.x("DELETE FROM events WHERE stream IN (%s, %s)", (s1, s2))
