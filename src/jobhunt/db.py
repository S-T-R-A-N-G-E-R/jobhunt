import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import config
from .models import Job, now_utc

MIGRATIONS = [
    """
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        source TEXT NOT NULL,
        company TEXT NOT NULL,
        external_id TEXT NOT NULL,
        title TEXT NOT NULL,
        location TEXT,
        is_remote INTEGER NOT NULL DEFAULT 0,
        department TEXT,
        url TEXT NOT NULL,
        description_text TEXT,
        compensation_text TEXT,
        posted_at TEXT,
        content_hash TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        UNIQUE (source, company, external_id)
    );
    CREATE TABLE scores (
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL REFERENCES jobs(id),
        content_hash TEXT NOT NULL,
        profile_hash TEXT NOT NULL,
        hard_filter_result TEXT NOT NULL,      -- 'pass' or the rejection reason
        score INTEGER,
        verdict TEXT,
        reasons TEXT,                          -- JSON list
        red_flags TEXT,                        -- JSON list
        seniority_match TEXT,
        extracted_comp TEXT,
        one_line_pitch TEXT,
        provider TEXT,
        model TEXT,
        escalated INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER,
        status TEXT NOT NULL,                  -- filtered | scored | score_failed
        error TEXT,
        scored_at TEXT NOT NULL
    );
    CREATE INDEX scores_job ON scores(job_id, id);
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY,
        run_date TEXT NOT NULL,                -- local date (see config.local_tz)
        started_at TEXT NOT NULL,
        ended_at TEXT,
        status TEXT NOT NULL DEFAULT 'running', -- running | ok | partial | failed
        companies INTEGER DEFAULT 0,
        fetched INTEGER DEFAULT 0,
        new_jobs INTEGER DEFAULT 0,
        filtered_out INTEGER DEFAULT 0,
        scored_local INTEGER DEFAULT 0,
        escalated INTEGER DEFAULT 0,
        score_failed INTEGER DEFAULT 0,
        sent INTEGER DEFAULT 0,
        digest_sent INTEGER DEFAULT 0,         -- any Telegram message sent by this run
        ollama_status TEXT,                    -- NULL = ok, else the problem + fix
        errors TEXT                            -- JSON {company or step: message}
    );
    CREATE TABLE digests (
        id INTEGER PRIMARY KEY,
        job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
        run_id INTEGER REFERENCES runs(id),
        score INTEGER,
        sent_at TEXT NOT NULL
    );
    """,
    "ALTER TABLE runs ADD COLUMN quota_skipped INTEGER DEFAULT 0",
    # Score before the seniority cap, for the digest's stretch section. Older rows: known only
    # where the cap didn't bite (not 'over', or under the cap of 40); capped rows stay NULL.
    """
    ALTER TABLE scores ADD COLUMN uncapped_score INTEGER;
    UPDATE scores SET uncapped_score = score
     WHERE status = 'scored' AND (seniority_match IS NOT 'over' OR score < 40);
    """,
    # Apply / Skip button presses read by cloud runs; the laptop copies them (decisions.py).
    """
    CREATE TABLE decisions (
        job_id INTEGER PRIMARY KEY,
        action TEXT NOT NULL,
        update_id INTEGER NOT NULL,
        decided_at TEXT NOT NULL
    );
    """,
]


def today() -> str:
    return datetime.now(config.local_tz()).date().isoformat()


def local_date(iso_ts: str) -> str:
    return datetime.fromisoformat(iso_ts).astimezone(config.local_tz()).date().isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    row = conn.execute("SELECT max(version) FROM schema_version").fetchone()
    current = row[0] or 0
    for version, sql in enumerate(MIGRATIONS, start=1):
        if version > current:
            conn.executescript(
                f"BEGIN; {sql}; INSERT INTO schema_version VALUES ({version}); COMMIT;"
            )


def upsert_jobs(conn: sqlite3.Connection, source: str, company: str, jobs: list[Job]) -> int:
    """Insert/update a company's jobs, deactivate ones that vanished. Returns # new jobs."""
    now = now_utc()
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT external_id FROM jobs WHERE source=? AND company=?", (source, company)
        )
    }
    with conn:
        for j in jobs:
            conn.execute(
                """INSERT INTO jobs (source, company, external_id, title, location, is_remote,
                       department, url, description_text, compensation_text, posted_at,
                       content_hash, first_seen_at, last_seen_at, is_active)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                   ON CONFLICT (source, company, external_id) DO UPDATE SET
                       title=excluded.title, location=excluded.location,
                       is_remote=excluded.is_remote, department=excluded.department,
                       url=excluded.url, description_text=excluded.description_text,
                       compensation_text=excluded.compensation_text,
                       posted_at=excluded.posted_at, content_hash=excluded.content_hash,
                       last_seen_at=excluded.last_seen_at, is_active=1""",
                (
                    j.source,
                    j.company,
                    j.external_id,
                    j.title,
                    j.location,
                    int(j.is_remote),
                    j.department,
                    j.url,
                    j.description_text,
                    j.compensation_text,
                    j.posted_at,
                    j.content_hash,
                    now,
                    now,
                ),
            )
        conn.execute(
            "UPDATE jobs SET is_active=0 WHERE source=? AND company=?"
            " AND external_id NOT IN (SELECT value FROM json_each(?))",
            (source, company, json.dumps([j.external_id for j in jobs])),
        )
    return len({j.external_id for j in jobs} - existing)


def deactivate_unlisted(conn: sqlite3.Connection, companies: list) -> int:
    """Mark jobs inactive when their company is no longer in companies.yaml (it's never fetched
    again, so nothing else would). Returns the number of jobs deactivated."""
    listed = json.dumps([[c.ats, c.name] for c in companies])
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET is_active=0 WHERE is_active=1 AND json_array(source, company)"
            " NOT IN (SELECT value FROM json_each(?))",
            (listed,),
        )
    return cur.rowcount


def prune_descriptions(conn: sqlite3.Connection, days: int = 30) -> int:
    """Drop the description of jobs inactive for `days`+ days; scores and content_hash stay.

    If such a job comes back, the next upsert refills it with the same hash, so it isn't rescored.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    with conn:
        cur = conn.execute(
            "UPDATE jobs SET description_text=NULL"
            " WHERE is_active=0 AND last_seen_at < ? AND description_text IS NOT NULL",
            (cutoff,),
        )
    return cur.rowcount


def latest_scores(conn: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    rows = conn.execute(
        "SELECT s.* FROM scores s JOIN (SELECT job_id, max(id) id FROM scores GROUP BY job_id) l"
        " ON s.id = l.id"
    )
    return {r["job_id"]: r for r in rows}


def insert_score(conn: sqlite3.Connection, **fields) -> None:
    fields.setdefault("scored_at", now_utc())
    for key in ("reasons", "red_flags"):
        if isinstance(fields.get(key), list):
            fields[key] = json.dumps(fields[key])
    cols = ", ".join(fields)
    with conn:
        conn.execute(
            f"INSERT INTO scores ({cols}) VALUES ({', '.join('?' * len(fields))})",
            list(fields.values()),
        )


def start_run(conn: sqlite3.Connection) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO runs (run_date, started_at) VALUES (?, ?)", (today(), now_utc())
        )
    return cur.lastrowid


def update_run(conn: sqlite3.Connection, run_id: int, **fields) -> None:
    if "errors" in fields and not isinstance(fields["errors"], str):
        fields["errors"] = json.dumps(fields["errors"])
    sets = ", ".join(f"{k}=?" for k in fields)
    with conn:
        conn.execute(f"UPDATE runs SET {sets} WHERE id=?", [*fields.values(), run_id])


def get_run(conn: sqlite3.Connection, run_id: int | None = None) -> sqlite3.Row | None:
    if run_id is None:
        return conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()


def ran_ok_today(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM runs WHERE run_date=? AND status='ok' LIMIT 1", (today(),)
        ).fetchone()
    )


def digest_sent_today(conn: sqlite3.Connection) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM runs WHERE run_date=? AND digest_sent=1 LIMIT 1", (today(),)
        ).fetchone()
    )
