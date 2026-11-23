"""
db.py - optional Postgres (Neon) + pgvector storage.

Why: Render's disk is wiped on every restart/deploy. With DATABASE_URL set, everything that matters lives
in Postgres and the local disk is only a cache:
  docs        original uploaded files (bytea) + metadata (tags = machine ids, groups = who may see it)
  doc_texts   extracted text of every page (so a restart does not need to re-read PDFs before searching)
  chunks      every search chunk with its embedding  -> semantic search runs in Postgres (pgvector)
  revisions   ProcOps approved page versions
  events      ProcOps question log
  app_data    small JSON records: reminders, notes, pins, sessions, workflows, connectors, users

Set it with the DATABASE_URL environment variable, or a database_url.txt file in the project folder
(git-ignored). Without it the app keeps working on local files exactly as before.
"""
import json
import os
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS docs (
  uid text PRIMARY KEY, meta jsonb NOT NULL, data bytea, updated timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS doc_texts (uid text PRIMARY KEY REFERENCES docs(uid) ON DELETE CASCADE, texts jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (
  uid text NOT NULL REFERENCES docs(uid) ON DELETE CASCADE, model text NOT NULL, ord int NOT NULL,
  page int NOT NULL, text text NOT NULL, embedding vector(%(dim)s), PRIMARY KEY (uid, model, ord));
CREATE TABLE IF NOT EXISTS revisions (uid text PRIMARY KEY, data jsonb NOT NULL);
CREATE TABLE IF NOT EXISTS events (id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), data jsonb NOT NULL,
  archived boolean DEFAULT false);
CREATE TABLE IF NOT EXISTS app_data (kind text NOT NULL, id text NOT NULL, data jsonb NOT NULL,
  updated timestamptz DEFAULT now(), PRIMARY KEY (kind, id));
"""


def database_url(root: Path):
    url = os.environ.get("DATABASE_URL", "").strip()
    f = root / "database_url.txt"
    if not url and f.exists():
        lines = [x.strip() for x in f.read_text(encoding="utf-8-sig").splitlines() if x.strip() and not x.startswith("#")]
        url = lines[0] if lines else ""
    return url or None


def vec_literal(v):
    return "[" + ",".join(f"{float(x):.6f}" for x in v) + "]"


class DB:
    def __init__(self, url, dim=384):
        import psycopg                                   # imported only when a database is configured
        self.psycopg = psycopg
        self.url, self.dim = url, dim
        self.lock = threading.RLock()
        self.conn = None
        with self.lock:
            self._exec(SCHEMA.replace("%(dim)s", str(dim)))

    # ---------- connection with one automatic reconnect (Neon suspends idle computes)
    def _connect(self):
        self.conn = self.psycopg.connect(self.url, autocommit=True, connect_timeout=15,
                                         prepare_threshold=None)   # works through Neon's PgBouncer pooler

    def _exec(self, sql, args=None, fetch=False, many=None):
        for attempt in (1, 2):
            try:
                if self.conn is None or self.conn.closed:
                    self._connect()
                with self.conn.cursor() as cur:
                    if many is not None:
                        cur.executemany(sql, many)
                        return None
                    cur.execute(sql, args)
                    return cur.fetchall() if fetch else None
            except (self.psycopg.OperationalError, self.psycopg.InterfaceError):
                self.conn = None
                if attempt == 2:
                    raise
                time.sleep(1)

    def q(self, sql, args=None):
        with self.lock:
            return self._exec(sql, args, fetch=True)

    def x(self, sql, args=None, many=None):
        with self.lock:
            return self._exec(sql, args, many=many)

    # ---------- documents
    def list_docs(self):
        return [r[0] for r in self.q("SELECT meta FROM docs ORDER BY (meta->>'uploaded'), uid")]

    def put_doc(self, meta, data=None):
        if data is None:
            self.x("INSERT INTO docs(uid, meta) VALUES (%s, %s) ON CONFLICT (uid) DO UPDATE SET meta = EXCLUDED.meta, updated = now()",
                   (meta["uid"], json.dumps(meta)))
        else:
            self.x("INSERT INTO docs(uid, meta, data) VALUES (%s, %s, %s) ON CONFLICT (uid) DO UPDATE SET meta = EXCLUDED.meta, "
                   "data = EXCLUDED.data, updated = now()", (meta["uid"], json.dumps(meta), data))

    def doc_data(self, uid):
        r = self.q("SELECT data FROM docs WHERE uid = %s", (uid,))
        return bytes(r[0][0]) if r and r[0][0] is not None else None

    def delete_doc(self, uid):
        self.x("DELETE FROM docs WHERE uid = %s", (uid,))
        self.x("DELETE FROM revisions WHERE uid = %s", (uid,))

    def put_texts(self, uid, texts):
        self.x("INSERT INTO doc_texts(uid, texts) VALUES (%s, %s) ON CONFLICT (uid) DO UPDATE SET texts = EXCLUDED.texts",
               (uid, json.dumps(texts)))

    def texts(self, uid):
        r = self.q("SELECT texts FROM doc_texts WHERE uid = %s", (uid,))
        return r[0][0] if r else None

    # ---------- chunks + embeddings (pgvector)
    def chunk_count(self, uid, model):
        return self.q("SELECT count(*) FROM chunks WHERE uid = %s AND model = %s", (uid, model))[0][0]

    def put_chunks(self, uid, model, chunks, matrix):
        """chunks: [(page, text)], matrix: normalised embeddings, same order."""
        with self.lock:
            self._exec("DELETE FROM chunks WHERE uid = %s AND model = %s", (uid, model))
            rows = [(uid, model, i, p, t, vec_literal(v)) for i, ((p, t), v) in enumerate(zip(chunks, matrix))]
            for s in range(0, len(rows), 200):
                self._exec("INSERT INTO chunks(uid, model, ord, page, text, embedding) VALUES (%s,%s,%s,%s,%s,%s::vector)",
                           many=rows[s:s + 200])

    def get_matrix(self, uid, model, n):
        rows = self.q("SELECT embedding::text FROM chunks WHERE uid = %s AND model = %s ORDER BY ord", (uid, model))
        if len(rows) != n:
            return None
        import numpy as np
        return np.array([json.loads(r[0]) for r in rows], dtype=np.float32)

    def semantic(self, qvec, model, k, uids):
        """Top-k chunks by cosine similarity, only inside the given documents. -> [(uid, ord, score)]"""
        if not uids:
            return []
        rows = self.q("SELECT uid, ord, 1 - (embedding <=> %s::vector) AS s FROM chunks "
                      "WHERE model = %s AND uid = ANY(%s) ORDER BY embedding <=> %s::vector LIMIT %s",
                      (vec_literal(qvec), model, list(uids), vec_literal(qvec), k))
        return [(r[0], r[1], float(r[2])) for r in rows]

    # ---------- revisions
    def revisions(self):
        return {r[0]: r[1] for r in self.q("SELECT uid, data FROM revisions")}

    def put_revision(self, uid, data):
        self.x("INSERT INTO revisions(uid, data) VALUES (%s, %s) ON CONFLICT (uid) DO UPDATE SET data = EXCLUDED.data",
               (uid, json.dumps(data)))

    def delete_revision(self, uid):
        self.x("DELETE FROM revisions WHERE uid = %s", (uid,))

    # ---------- events (ProcOps log)
    def add_event(self, ev):
        self.x("INSERT INTO events(data) VALUES (%s)", (json.dumps(ev),))

    def events(self):
        return [r[0] for r in self.q("SELECT data FROM events WHERE NOT archived ORDER BY id")]

    def archive_events(self):
        self.x("UPDATE events SET archived = true WHERE NOT archived")

    # ---------- small JSON records
    def kv_put(self, kind, id_, data):
        self.x("INSERT INTO app_data(kind, id, data) VALUES (%s, %s, %s) ON CONFLICT (kind, id) DO UPDATE SET "
               "data = EXCLUDED.data, updated = now()", (kind, str(id_), json.dumps(data)))

    def kv_get(self, kind, id_):
        r = self.q("SELECT data FROM app_data WHERE kind = %s AND id = %s", (kind, str(id_)))
        return r[0][0] if r else None

    def kv_list(self, kind):
        return [r[0] for r in self.q("SELECT data FROM app_data WHERE kind = %s ORDER BY updated DESC", (kind,))]

    def kv_delete(self, kind, id_):
        self.x("DELETE FROM app_data WHERE kind = %s AND id = %s", (kind, str(id_)))


class LocalKV:
    """Same small-record API on local JSON files (used when no database is configured)."""

    def __init__(self, root: Path):
        self.root = root / "app_data"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _file(self, kind):
        return self.root / f"{kind}.json"

    def _load(self, kind):
        f = self._file(kind)
        try:
            return json.loads(f.read_text("utf-8")) if f.exists() else {}
        except ValueError:
            return {}

    def _save(self, kind, d):
        tmp = self._file(kind).with_suffix(".tmp")
        tmp.write_text(json.dumps(d, indent=1, ensure_ascii=False), "utf-8")
        tmp.replace(self._file(kind))

    def kv_put(self, kind, id_, data):
        with self.lock:
            d = self._load(kind)
            d[str(id_)] = {"data": data, "updated": time.time()}
            self._save(kind, d)

    def kv_get(self, kind, id_):
        with self.lock:
            r = self._load(kind).get(str(id_))
            return r["data"] if r else None

    def kv_list(self, kind):
        with self.lock:
            return [r["data"] for r in sorted(self._load(kind).values(), key=lambda r: -r["updated"])]

    def kv_delete(self, kind, id_):
        with self.lock:
            d = self._load(kind)
            d.pop(str(id_), None)
            self._save(kind, d)
