"""fetch -> filter/score -> digest, recorded as one row in `runs`."""

import asyncio
import logging
from logging.handlers import RotatingFileHandler

import httpx

from . import config, db, decisions, digest
from .config import Company, Profile
from .llm import LLM
from .scoring import ScoreCounts, plan, score_jobs
from .sources import fetch_all

log = logging.getLogger("jobhunt")
LOG_FILE = config.LOG_DIR / "jobhunt.log"


def setup_logging() -> None:
    if any(getattr(h, "baseFilename", None) == str(LOG_FILE) for h in log.handlers):
        return
    for h in list(log.handlers):
        log.removeHandler(h)
    LOG_FILE.parent.mkdir(exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=5)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    # httpx logs full URLs at INFO, and the Telegram URL contains the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def fetch(conn, companies: list[Company], transport=None) -> tuple[int, int, dict]:
    """Returns (jobs fetched, new jobs, {company: error})."""
    dropped = db.deactivate_unlisted(conn, companies)
    if dropped:
        log.info("deactivated %d jobs of companies no longer in companies.yaml", dropped)
    results, errors = asyncio.run(fetch_all(companies, transport))
    by_name = {c.name: c for c in companies}
    fetched = new = 0
    for name, jobs in results.items():
        fetched += len(jobs)
        new += db.upsert_jobs(conn, by_name[name].ats, name, jobs)
    return fetched, new, errors


def score(
    conn, profile: Profile, companies: list[Company], llm: LLM, rescore=False, show_progress=True
) -> tuple[ScoreCounts, str | None]:
    """Score if Ollama is healthy; otherwise only apply hard filters. Returns (counts, problem)."""
    problem = llm.health_check()
    if problem:
        log.error("skipping LLM scoring: %s", problem)
        return ScoreCounts(filtered_out=plan(conn, profile, companies).filtered_now), problem
    return score_jobs(conn, profile, companies, llm, rescore, show_progress), None


def run(
    if_not_run_today: bool = False,
    preview: bool = False,
    show_progress: bool = True,
    fetch_transport: httpx.AsyncBaseTransport | None = None,
    llm_transport: httpx.BaseTransport | None = None,
    telegram_transport: httpx.BaseTransport | None = None,
) -> int | None:
    """One full run. Returns the run id (None if skipped)."""
    setup_logging()
    conn = db.connect()
    # Before the "already ran" guard: the catch-up run must still collect button presses,
    # because Telegram drops unread updates after 24 hours.
    try:
        decisions.poll(conn, telegram_transport)
    except digest.DigestError as e:
        log.warning("couldn't read button presses: %s", e)
    if if_not_run_today and db.ran_ok_today(conn):
        log.info("already ran successfully today; nothing to do")
        return None
    run_id = db.start_run(conn)
    errors: dict[str, str] = {}
    try:
        profile = config.load_profile()
        companies = config.load_companies()
        fetched, new, errors = fetch(conn, companies, fetch_transport)
        db.update_run(
            conn, run_id, companies=len(companies), fetched=fetched, new_jobs=new, errors=errors
        )
        llm = LLM(profile.llm, transport=llm_transport)
        counts, problem = score(conn, profile, companies, llm, show_progress=show_progress)
        db.update_run(
            conn,
            run_id,
            filtered_out=counts.filtered_out,
            scored_local=counts.scored_local,
            escalated=counts.escalated,
            score_failed=counts.score_failed,
            quota_skipped=counts.quota_skipped,
            ollama_status=problem,
        )
        try:
            digest.send(conn, profile, run_id, preview=preview, transport=telegram_transport)
        except digest.DigestError as e:
            log.error("%s", e)
            errors["digest"] = str(e)
        pruned = db.prune_descriptions(conn)
        if pruned:
            log.info("dropped descriptions of %d jobs inactive for 30+ days", pruned)
        # Retryable (partial): Ollama down, no company fetched at all (e.g. no network), or the
        # digest couldn't be sent. A single failing company is only reported.
        all_failed = bool(companies) and len(errors.keys() - {"digest"}) >= len(companies)
        db.update_run(
            conn,
            run_id,
            status="partial" if problem or all_failed or "digest" in errors else "ok",
            ended_at=db.now_utc(),
            errors=errors,
        )
        log.info("run %d finished: %s", run_id, dict(db.get_run(conn, run_id)))
        return run_id
    except (Exception, SystemExit) as e:
        log.exception("run %d crashed", run_id)
        errors["crash"] = f"{type(e).__name__}: {e}"[:500]
        db.update_run(conn, run_id, status="failed", ended_at=db.now_utc(), errors=errors)
        try:
            digest.send_telegram(
                f"❌ jobhunt run failed: {digest.html.escape(errors['crash'][:300])}\n"
                f"Log: {digest.html.escape(str(LOG_FILE))}",
                telegram_transport,
            )
        except digest.DigestError as te:
            log.error("couldn't send failure notice: %s", te)
        raise
