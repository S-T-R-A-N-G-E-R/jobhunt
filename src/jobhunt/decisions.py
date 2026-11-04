"""Apply / Skip button presses on digest messages (Telegram callback queries).

Nothing listens to Telegram live: presses are read with getUpdates at the start of every cloud
run and every `jobhunt apply`. Telegram keeps unread updates for 24 hours, and the cloud runs
twice a day, so none are lost. Whoever reads an update stores it and confirms it, so each
press is read once; storing is keyed by job, and the newest press (highest update_id) wins,
so replays and double taps are harmless.
"""

import logging
import sqlite3

import httpx

from .config import env
from .digest import DigestError, keyboard, telegram
from .models import now_utc

log = logging.getLogger(__name__)

ACTIONS = {"a": "approve", "s": "skip"}

SCHEMA = """CREATE TABLE IF NOT EXISTS decisions (
    job_id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,                  -- approve | skip
    update_id INTEGER NOT NULL,            -- Telegram update id; terminal picks reuse the last one
    decided_at TEXT NOT NULL
)"""


def record(
    conn: sqlite3.Connection, job_id: int, action: str, update_id: int, at: str, same=False
) -> bool:
    """Store a decision if it's newer than the stored one (`same`: also on an equal id).
    Replays of an already-stored press change nothing. Returns True if stored."""
    op = ">=" if same else ">"
    with conn:
        cur = conn.execute(
            f"""INSERT INTO decisions (job_id, action, update_id, decided_at) VALUES (?,?,?,?)
               ON CONFLICT (job_id) DO UPDATE SET action=excluded.action,
                   update_id=excluded.update_id, decided_at=excluded.decided_at
               WHERE excluded.update_id {op} decisions.update_id""",
            (job_id, action, update_id, at),
        )
    return cur.rowcount > 0


def record_terminal(conn: sqlite3.Connection, job_id: int, action: str) -> None:
    """A decision made in `jobhunt apply pick`: replaces the last press, and a later press
    still wins (it has a higher update id)."""
    row = conn.execute("SELECT update_id FROM decisions WHERE job_id=?", (job_id,)).fetchone()
    record(conn, job_id, action, row[0] if row else 0, now_utc(), same=True)


def parse(update: dict, chat_id: str) -> tuple[int, str, dict] | None:
    """(job_id, action, callback_query) for a button press in our chat, else None."""
    cq = update.get("callback_query")
    if not cq:
        return None
    chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
    if chat != str(chat_id):
        return None  # someone else's chat: ignore
    kind, _, job = (cq.get("data") or "").partition(":")
    if kind not in ACTIONS or not job.isdigit():
        return None
    return int(job), ACTIONS[kind], cq


def poll(conn: sqlite3.Connection, transport: httpx.BaseTransport | None = None) -> int:
    """Read pending button presses into `conn`'s decisions table. Returns how many were new."""
    chat_id = env("TELEGRAM_CHAT_ID")
    if not chat_id or not env("TELEGRAM_BOT_TOKEN"):
        return 0
    updates = (
        telegram("getUpdates", {"timeout": 0, "allowed_updates": ["callback_query"]}, transport)
        or []
    )
    stored = 0
    for u in updates:
        parsed = parse(u, chat_id)
        if not parsed:
            continue
        job_id, action, cq = parsed
        if record(conn, job_id, action, u["update_id"], now_utc()):
            stored += 1
        _acknowledge(cq, job_id, action, transport)
    if updates:
        # An update is confirmed once getUpdates is called with a higher offset.
        telegram(
            "getUpdates", {"offset": updates[-1]["update_id"] + 1, "timeout": 0, "limit": 1},
            transport,
        )  # fmt: skip
    if stored:
        log.info("stored %d button decisions", stored)
    return stored


def _acknowledge(cq: dict, job_id: int, action: str, transport) -> None:
    """Best effort: stop the button's spinner (fails once the query is old, which is fine) and
    mark the chosen button on the message."""
    try:
        telegram("answerCallbackQuery", {"callback_query_id": cq["id"]}, transport)
    except DigestError:
        pass
    msg = cq.get("message") or {}
    try:
        telegram(
            "editMessageReplyMarkup",
            {
                "chat_id": msg["chat"]["id"],
                "message_id": msg["message_id"],
                "reply_markup": keyboard(job_id, chosen=action),
            },
            transport,
        )
    except (DigestError, KeyError):
        pass  # e.g. "message is not modified" when the same button is pressed again
