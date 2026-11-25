"""Versioned database migrations (Phase 4 part 2b) - replaces "CREATE TABLE IF NOT EXISTS for everything".

Each migration runs ONCE, inside a transaction (all of it or nothing), and is recorded in schema_migrations.
A Postgres advisory lock makes sure two servers starting at the same moment never run one twice.
Rules: never edit a migration that has shipped - add a new one. Prefer additive changes (new columns with
defaults) so the running old version keeps working during a deploy.
"""
MIGRATIONS = [
    (1, "tenant_id on app_data and events", [
        "ALTER TABLE app_data ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default'",
        "ALTER TABLE app_data DROP CONSTRAINT IF EXISTS app_data_pkey",
        "ALTER TABLE app_data ADD PRIMARY KEY (tenant_id, kind, id)",
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS tenant_id text NOT NULL DEFAULT 'default'",
        "CREATE INDEX IF NOT EXISTS events_tenant_id ON events (tenant_id, id)",
    ]),
    # Phase 6: `events` becomes the generic module journal (core/data/journal.py). Every row written before this
    # migration was the question log of the old built-in SOP-feedback feature, now the pack "industrial_field_ops" -
    # so existing rows get that stream name once (data history only; the Core does not use the pack otherwise).
    (2, "journal streams on events", [
        "ALTER TABLE events ADD COLUMN IF NOT EXISTS stream text NOT NULL DEFAULT 'industrial_field_ops.questions'",
        "ALTER TABLE events ALTER COLUMN stream SET DEFAULT ''",
        "CREATE INDEX IF NOT EXISTS events_tenant_stream ON events (tenant_id, stream, id)",
    ]),
]

TRACKING = ("CREATE TABLE IF NOT EXISTS schema_migrations (version int PRIMARY KEY, name text NOT NULL, "
            "applied timestamptz NOT NULL DEFAULT now())")
LOCK_ID = 727021                    # any constant: the advisory lock "name" for VR_RAG migrations


def pending(applied: set) -> list:
    return [m for m in MIGRATIONS if m[0] not in applied]


def run(conn, log=print) -> list:
    """Apply every pending migration on a psycopg connection (autocommit). Returns the versions applied."""
    done = []
    with conn.cursor() as cur:
        cur.execute(TRACKING)
        cur.execute("SELECT pg_advisory_lock(%s)", (LOCK_ID,))
        try:
            cur.execute("SELECT version FROM schema_migrations")
            applied = {r[0] for r in cur.fetchall()}
            for version, name, statements in pending(applied):
                with conn.transaction():
                    with conn.cursor() as tx:
                        for sql in statements:
                            tx.execute(sql)
                        tx.execute("INSERT INTO schema_migrations(version, name) VALUES (%s, %s)", (version, name))
                log(f"[db] migration {version} applied: {name}")
                done.append(version)
        finally:
            cur.execute("SELECT pg_advisory_unlock(%s)", (LOCK_ID,))
    return done
