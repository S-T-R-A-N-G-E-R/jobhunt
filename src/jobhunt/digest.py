import html
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta

import httpx

from . import config, db
from .config import Profile, env
from .models import now_utc

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
SENIORITY_HINT = re.compile(r"senior|staff|lead|principal|years|yrs|experience|level", re.I)


class DigestError(Exception):
    pass


_UNSENT_SCORED = """SELECT j.*, s.score, s.uncapped_score, s.verdict, s.reasons, s.red_flags,
                  s.extracted_comp, s.one_line_pitch
           FROM jobs j
           JOIN scores s ON s.id = (SELECT max(id) FROM scores WHERE job_id = j.id)
           WHERE j.is_active = 1 AND s.status = 'scored'
             AND j.id NOT IN (SELECT job_id FROM digests)"""


def pending_jobs(conn: sqlite3.Connection, profile: Profile) -> list[sqlite3.Row]:
    """Top unsent, active, scored jobs above the threshold."""
    return conn.execute(
        f"""{_UNSENT_SCORED} AND s.score >= ?
           ORDER BY s.score DESC, j.first_seen_at DESC LIMIT ?""",
        (profile.scoring.min_score_for_digest, profile.digest.max_jobs),
    ).fetchall()


def stretch_jobs(conn: sqlite3.Connection, profile: Profile) -> list[sqlite3.Row]:
    """Unsent jobs held back only by the seniority cap: marked 'over', but scoring at least the
    digest threshold before the cap. Best first, at most digest.stretch_max."""
    if profile.digest.stretch_max <= 0:
        return []
    min_score = profile.scoring.min_score_for_digest
    return conn.execute(
        f"""{_UNSENT_SCORED} AND s.seniority_match = 'over' AND s.uncapped_score >= ?
             AND s.score < ?
           ORDER BY s.uncapped_score DESC, j.first_seen_at DESC LIMIT ?""",
        (min_score, min_score, profile.digest.stretch_max),
    ).fetchall()


def _notes(run: sqlite3.Row | None) -> list[str]:
    """Status lines about the run: Ollama, fetch errors, escalations/failures."""
    if run is None:
        return []
    notes = []
    if run["quota_skipped"]:
        notes.append(
            f"⚠️ Ollama Cloud free credits exhausted: {run['quota_skipped']} jobs not scored. "
            "Options: wait for reset, set vars.JOBHUNT_LLM=runner, or enable the paid fallback."
        )
    if run["ollama_status"]:
        notes.append(f"⚠️ Scoring skipped. {run['ollama_status']}")
    errors = json.loads(run["errors"] or "{}")
    if errors:
        notes.append(f"⚠️ Couldn't fetch: {', '.join(sorted(errors))} (see logs)")
    if run["escalated"] or run["score_failed"]:
        notes.append(
            f"ℹ️ {run['escalated']} escalated to paid model · {run['score_failed']} failed scoring"
        )
    return notes


def late_note(conn: sqlite3.Connection, run: sqlite3.Row | None) -> str | None:
    """JOBHUNT_SCHEDULED_AT ("HH:MM" local, set by the cloud workflow): if nothing has reached
    you yet today (no run sent a message) and this run started over 2 hours after it, say so."""
    at = env("JOBHUNT_SCHEDULED_AT")
    if not at or run is None:
        return None
    earlier = conn.execute(
        "SELECT 1 FROM runs WHERE run_date = ? AND id < ? AND digest_sent = 1",
        (run["run_date"], run["id"]),
    ).fetchone()
    started = datetime.fromisoformat(run["started_at"]).astimezone(config.local_tz())
    h, m = map(int, at.split(":"))
    late = started - started.replace(hour=h, minute=m, second=0, microsecond=0)
    if earlier or late <= timedelta(hours=2):
        return None
    hours, minutes = divmod(int(late.total_seconds()) // 60, 60)
    return (
        f"⏰ Nothing reached you before this: today's run started {hours}h {minutes:02d}m "
        f"after {at}. GitHub delayed or skipped the scheduled runs, or an earlier send failed."
    )


def _header(n: int, run: sqlite3.Row | None) -> str:
    checked = run["fetched"] if run else 0
    companies = run["companies"] if run else 0
    return f"{n} new matches today · {checked} jobs checked across {companies} companies"


def stretch_reason(flags: list[str]) -> str:
    """The red flag that best explains why the role is above your level."""
    fallback = "needs more seniority than you have"
    return next((f for f in flags if SENIORITY_HINT.search(f)), fallback)


def _fields(job: sqlite3.Row, stretch: bool = False) -> dict:
    reasons = json.loads(job["reasons"] or "[]")
    flags = json.loads(job["red_flags"] or "[]")
    if stretch:
        why = stretch_reason(flags)
        flags = [why] + [f for f in flags if f != why]
    return {
        "stretch": stretch,
        "company": job["company"],
        "title": job["title"],
        "location": job["location"] or "location not stated",
        "score": job["uncapped_score"] if stretch else job["score"],
        "pitch": job["one_line_pitch"] or "",
        "reason": reasons[0] if reasons else "",
        "flag": flags[0] if flags else "",
        "comp": job["extracted_comp"] or job["compensation_text"] or "",
        "url": job["url"],
    }


def _html_block(f: dict) -> str:
    e = lambda s: html.escape(str(s)[:300], quote=False)  # noqa: E731
    stretch = f.get("stretch")
    lines = [
        f"<b>{e(f['company'])}</b> · {e(f['title'])}",
        f"📍 {e(f['location'])} · ⭐ {f['score']}" + (" before the cap" if stretch else ""),
    ]
    if stretch:
        lines.append(f"🧗 <b>Stretch</b>, above your level: {e(f['flag'])}")
    if f["pitch"]:
        lines.append(f"<i>{e(f['pitch'])}</i>")
    if f["reason"]:
        lines.append(f"✅ {e(f['reason'])}")
    if f["flag"] and not stretch:
        lines.append(f"🚩 {e(f['flag'])}")
    if f["comp"]:
        lines.append(f"💰 {e(f['comp'])}")
    lines.append(f'<a href="{html.escape(f["url"], quote=True)}">Open job</a>')
    return "\n".join(lines)


def _md_block(f: dict) -> str:
    if f.get("stretch"):
        lines = [
            f"### Stretch: {f['company']} · {f['title']}",
            f"- {f['location']} · score {f['score']} before the cap",
            f"- above your level: {f['flag']}",
        ]
        keys = ("pitch", "reason", "comp")
    else:
        lines = [f"### {f['company']} · {f['title']}", f"- {f['location']} · score {f['score']}"]
        keys = ("pitch", "reason", "flag", "comp")
    for key in keys:
        if f[key]:
            lines.append(f"- {key}: {f[key]}")
    lines.append(f"- {f['url']}")
    return "\n".join(lines)


def split_messages(parts: list[str], limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Pack parts (separated by blank lines) into messages under the limit."""
    messages, current = [], ""
    for part in parts:
        part = part[: limit - 10]
        candidate = f"{current}\n\n{part}" if current else part
        if len(candidate) > limit:
            messages.append(current)
            candidate = part
        current = candidate
    if current:
        messages.append(current)
    return messages


def keyboard(job_id: int, chosen: str | None = None) -> dict:
    """The Apply / Skip buttons under a job. `chosen` ("approve"/"skip") marks the last press;
    both buttons stay, so a later press can change your mind."""
    mark = lambda action: " ✓" if chosen == action else ""  # noqa: E731
    return {
        "inline_keyboard": [
            [
                {"text": f"✅ Apply{mark('approve')}", "callback_data": f"a:{job_id}"},
                {"text": f"⏭ Skip{mark('skip')}", "callback_data": f"s:{job_id}"},
            ]
        ]
    }


def build(conn: sqlite3.Connection, profile: Profile, run: sqlite3.Row | None):
    """Returns (header messages, [(job, html block)] with a stretch-section header row
    (job None) before the first stretch role, markdown text, jobs included)."""
    jobs, stretch = pending_jobs(conn, profile), stretch_jobs(conn, profile)
    late = late_note(conn, run)
    top = ([late] if late else []) + _notes(run) + [_header(len(jobs), run)]
    blocks = [(j, _html_block(_fields(j))) for j in jobs]
    md_parts = ["\n".join(top)] + [_md_block(_fields(j)) for j in jobs]
    if stretch:
        blocks.append((None, f"<b>🧗 Stretch roles</b> ({len(stretch)}, above your level)"))
        md_parts.append(f"## Stretch roles ({len(stretch)}, above your level)")
        for j in stretch:
            f = _fields(j, stretch=True)
            blocks.append((j, _html_block(f)))
            md_parts.append(_md_block(f))
    header = split_messages([html.escape("\n".join(top), quote=False)])
    return header, blocks, "\n\n".join(md_parts), jobs + stretch


def telegram(
    method: str, payload: dict, transport: httpx.BaseTransport | None = None
) -> dict | list | bool:
    """Call a Bot API method; returns its `result`. Raises DigestError (token never shown)."""
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise DigestError("TELEGRAM_BOT_TOKEN not set in .env")
    try:
        with httpx.Client(transport=transport, timeout=20) as client:
            r = client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
        r.raise_for_status()
        return r.json().get("result")
    except httpx.HTTPError as e:
        # Never let the token (it's in the URL) reach logs or tracebacks.
        detail = getattr(getattr(e, "response", None), "text", "") or type(e).__name__
        msg = f"Telegram {method} failed: {detail[:200]}".replace(token, "***")
        raise DigestError(msg) from None


def send_telegram(
    text: str,
    transport: httpx.BaseTransport | None = None,
    reply_markup: dict | None = None,
    silent: bool = False,
) -> None:
    chat_id = env("TELEGRAM_CHAT_ID")
    if not env("TELEGRAM_BOT_TOKEN") or not chat_id:
        raise DigestError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in .env")
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "disable_notification": silent,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    telegram("sendMessage", payload, transport)


def write_file(md: str) -> None:
    config.OUTPUT_DIR.mkdir(exist_ok=True)
    now = datetime.now(config.local_tz())
    path = config.OUTPUT_DIR / f"digest-{now.date().isoformat()}.md"
    with path.open("a") as f:
        f.write(f"## Digest {now:%H:%M}\n\n{md}\n\n")


def send(
    conn: sqlite3.Connection,
    profile: Profile,
    run_id: int | None = None,
    preview: bool = False,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Send the digest. Returns the number of jobs sent."""
    run = db.get_run(conn, run_id)
    header, blocks, md, jobs = build(conn, profile, run)
    if preview:
        print(md)
        return 0
    if not jobs and db.digest_sent_today(conn):
        log.info("nothing new and today's status message already sent")
        return 0
    write_file(md)
    # One ping per digest: the header notifies, every job after it is silent. Each job gets its
    # own message with Apply / Skip buttons, and is marked sent as soon as Telegram accepts it.
    for i, message in enumerate(header):
        send_telegram(message, transport, silent=i > 0)
    sent = 0
    for job, block in blocks:
        if job is None:
            send_telegram(block, transport, silent=True)
            continue
        send_telegram(block, transport, reply_markup=keyboard(job["id"]), silent=True)
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO digests (job_id, run_id, score, sent_at) VALUES (?,?,?,?)",
                (job["id"], run_id, job["score"], now_utc()),
            )
        sent += 1
    if run_id:
        db.update_run(conn, run_id, digest_sent=1, sent=sent)
    log.info("digest sent: %d jobs", sent)
    return sent
