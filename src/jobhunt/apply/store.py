"""data/applications.db: decisions and applications, on the laptop only.

The cloud owns data/jobhunt.db, and `cloud pull-db` replaces it wholesale, so nothing the laptop
writes may live there. Job ids refer to jobhunt.db's jobs table.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

from .. import config, decisions
from ..models import now_utc

SCHEMA = f"""
{decisions.SCHEMA};
CREATE TABLE IF NOT EXISTS applications (
    job_id INTEGER PRIMARY KEY,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    status TEXT NOT NULL,                  -- applied | skipped | failed
    reason TEXT,                           -- why it failed or was skipped
    cover_letter TEXT,                     -- path of the letter used
    screenshot TEXT,                       -- confirmation screenshot
    updated_at TEXT NOT NULL
);
"""


def connect(path=None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.APPS_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def merge_cloud(apps: sqlite3.Connection, jobs: sqlite3.Connection) -> int:
    """Copy button presses the cloud read (they came down with pull-db). Returns # changed."""
    rows = jobs.execute("SELECT job_id, action, update_id, decided_at FROM decisions").fetchall()
    return sum(decisions.record(apps, *row) for row in rows)


def record(apps: sqlite3.Connection, job, status: str, reason="", cover_letter="", screenshot=""):
    with apps:
        apps.execute(
            """INSERT INTO applications (job_id, company, title, url, status, reason,
                   cover_letter, screenshot, updated_at) VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT (job_id) DO UPDATE SET status=excluded.status,
                   reason=excluded.reason, cover_letter=excluded.cover_letter,
                   screenshot=excluded.screenshot, updated_at=excluded.updated_at""",
            (job["id"], job["company"], job["title"], job["url"], status, reason,
             str(cover_letter or ""), str(screenshot or ""), now_utc()),
        )  # fmt: skip


_JOB = """SELECT j.*, s.score FROM jobs j
          LEFT JOIN scores s ON s.id = (SELECT max(id) FROM scores WHERE job_id = j.id)
          WHERE j.id = ?"""


def job(jobs: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return jobs.execute(_JOB, (job_id,)).fetchone()


def queue(apps: sqlite3.Connection, jobs: sqlite3.Connection) -> list[sqlite3.Row]:
    """Approved jobs not yet applied to or skipped (failed ones stay, to retry), oldest
    decision first. Jobs that are no longer active are left out."""
    ids = apps.execute(
        """SELECT d.job_id FROM decisions d
           WHERE d.action = 'approve' AND d.job_id NOT IN
               (SELECT job_id FROM applications WHERE status IN ('applied', 'skipped'))
           ORDER BY d.decided_at"""
    ).fetchall()
    rows = [job(jobs, i) for (i,) in ids]
    return [r for r in rows if r is not None and r["is_active"]]


def undecided(apps: sqlite3.Connection, jobs: sqlite3.Connection) -> list[sqlite3.Row]:
    """Jobs sent in a digest that have no decision and no application yet, newest first."""
    done = {
        r[0]
        for r in apps.execute("SELECT job_id FROM decisions UNION SELECT job_id FROM applications")
    }  # noqa: E501
    ids = jobs.execute(
        "SELECT d.job_id FROM digests d JOIN jobs j ON j.id = d.job_id"
        " WHERE j.is_active = 1 ORDER BY d.sent_at DESC, d.score DESC"
    ).fetchall()
    return [job(jobs, i) for (i,) in ids if i not in done]


def week_start() -> str:
    """Monday 00:00 of this week (local time), as a UTC timestamp comparable with updated_at."""
    now = datetime.now(config.local_tz())
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0)
    return monday.astimezone(UTC).isoformat(timespec="seconds")


def applied_this_week(apps: sqlite3.Connection) -> int:
    return apps.execute(
        "SELECT count(*) FROM applications WHERE status = 'applied' AND updated_at >= ?",
        (week_start(),),
    ).fetchone()[0]


def history(apps: sqlite3.Connection, jobs: sqlite3.Connection) -> list[dict]:
    """Applications plus jobs skipped with a button or in `apply pick`, newest first."""
    rows = [dict(r) for r in apps.execute("SELECT * FROM applications")]
    for d in apps.execute(
        "SELECT * FROM decisions WHERE action = 'skip'"
        " AND job_id NOT IN (SELECT job_id FROM applications)"
    ):
        j = job(jobs, d["job_id"])
        rows.append({"job_id": d["job_id"], "status": "skipped", "reason": "button / pick",
                     "updated_at": d["decided_at"], "company": j["company"] if j else "?",
                     "title": j["title"] if j else "?"})  # fmt: skip
    return sorted(rows, key=lambda r: r["updated_at"], reverse=True)
