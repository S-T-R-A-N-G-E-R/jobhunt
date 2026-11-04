"""Stage 1: hard filters in code. Stage 2: LLM scoring for jobs that pass."""

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

import yaml
from rich.progress import Progress

from . import config, db
from .config import Company, Profile
from .llm import LLM, LLMError, QuotaError
from .models import ScoreOutput

log = logging.getLogger(__name__)

DESCRIPTION_LIMIT = 6000
PRIORITY_BOOST = 5
OVER_SENIORITY_CAP = 40  # a role needing more seniority than you have can't score above this
SENIORITY_WORDS = {
    "senior",
    "sr",
    "staff",
    "lead",
    "principal",
    "junior",
    "jr",
    "mid",
    "i",
    "ii",
    "iii",
    "iv",
}
STOPWORDS = {"and", "of", "the", "a", "an", "for", "to", "in", "at", "with", "or"}
PLACE_ALIASES = {"bangalore": "bengaluru", "gurgaon": "gurugram", "bombay": "mumbai"}


def words(text: str) -> list[str]:
    text = re.sub(r"(\w)-(\w)", r"\1\2", text.lower())  # back-end -> backend
    found = re.findall(r"[a-z0-9+#.]+", text)
    return [PLACE_ALIASES.get(w, w) for w in found if w not in STOPWORDS]


def tokens(text: str) -> set[str]:
    return set(words(text))


def reorder_title(title: str) -> str:
    """'Engineer, Machine Learning (India)' -> 'Machine Learning Engineer'.

    Strips trailing qualifiers like '- Remote' or '(India)', then swaps the two halves
    if exactly one comma is left.
    """
    t = title.strip()
    while True:
        stripped = re.sub(r"\s*\([^()]*\)\s*$", "", t)
        stripped = re.sub(r"\s+[-–—]\s+[^,]*$", "", stripped).strip()
        if stripped == t:
            break
        t = stripped
    if t.count(",") == 1:
        a, b = (part.strip() for part in t.split(","))
        t = f"{b} {a}"
    return t


def title_matches(title: str, targets: list[str]) -> bool:
    """A target title (minus seniority words) must appear as a phrase, in order, in either
    the title as written or its reordered form ('Engineer, ML' -> 'ML Engineer')."""
    forms = [
        [w for w in words(t) if w not in SENIORITY_WORDS] for t in (title, reorder_title(title))
    ]
    for target in targets:
        core = [w for w in words(target) if w not in SENIORITY_WORDS]
        n = len(core)
        for have in forms:
            if n and any(have[i : i + n] == core for i in range(len(have) - n + 1)):
                return True
    return False


def hard_filter(job: sqlite3.Row, profile: Profile) -> str | None:
    """Return a rejection reason, or None if the job passes."""
    title = job["title"]
    for kw in profile.title_exclude_keywords:
        if re.search(rf"\b{re.escape(kw.lower())}\b", title.lower()):
            return f"title contains excluded keyword '{kw}'"
    if not title_matches(title, profile.target_titles):
        return "title doesn't match any target title"
    loc = job["location"] or ""
    if not loc:
        return None
    if job["is_remote"] and profile.locations.remote_ok:
        return None
    if profile.locations.relocation_ok:
        return None  # any location could work; the LLM judges city / visa fit
    have = tokens(loc)
    if any(tokens(a) and tokens(a) <= have for a in profile.locations.accept):
        return None
    return f"location '{loc}' not in accepted locations"


def build_prompt(job: sqlite3.Row, profile: Profile, resume: str) -> str:
    about_me = profile.model_dump(exclude={"llm", "scoring", "digest"})
    job_text = "\n".join(
        [
            f"Company: {job['company']}",
            f"Title: {job['title']}",
            f"Location: {job['location'] or 'not stated'}",
            f"Remote: {'yes' if job['is_remote'] else 'not stated'}",
            f"Department: {job['department'] or 'not stated'}",
            f"Compensation: {job['compensation_text'] or 'not stated'}",
            "",
            (job["description_text"] or "")[:DESCRIPTION_LIMIT],
        ]
    )
    template = config.PROMPT_PATH.read_text()
    return (
        template.replace("{{profile}}", yaml.safe_dump(about_me, sort_keys=False))
        .replace("{{resume}}", resume.strip() or "(no resume provided)")
        .replace("{{job}}", job_text)
    )


@dataclass
class Plan:
    to_score: list[sqlite3.Row] = field(default_factory=list)  # in priority order, capped
    backlog: int = 0  # filter-passing jobs never scored (incl. beyond the cap)
    stale: int = 0  # scored with an old profile_hash
    filtered_now: int = 0  # newly rejected by hard filters this pass


def plan(
    conn: sqlite3.Connection,
    profile: Profile,
    companies: list[Company],
    rescore: bool = False,
    write: bool = True,
) -> Plan:
    """Apply hard filters (recording rejections) and work out which jobs need the LLM."""
    ph = config.profile_hash()
    today = db.today()
    latest = db.latest_scores(conn)
    high = {c.name for c in companies if c.priority == "high"}
    p = Plan()
    unscored, stale = [], []
    for job in conn.execute("SELECT * FROM jobs WHERE is_active=1"):
        s = latest.get(job["id"])
        fresh = s is not None and s["content_hash"] == job["content_hash"]
        if fresh and s["profile_hash"] == ph:
            if s["status"] == "score_failed" and db.local_date(s["scored_at"]) < today:
                unscored.append(job)  # retry failures on a later day
            continue
        # New, changed, or evaluated under an older profile: filters are cheap, re-run them.
        reason = hard_filter(job, profile)
        if reason:
            p.filtered_now += 1
            if write:
                db.insert_score(
                    conn,
                    job_id=job["id"],
                    content_hash=job["content_hash"],
                    profile_hash=ph,
                    hard_filter_result=reason,
                    status="filtered",
                )
        elif fresh and s["status"] == "scored":
            stale.append(job)  # scored under an older profile: rescore only on request
        else:
            unscored.append(job)

    def order(j):
        return (j["company"] not in high, -_ts(j["posted_at"] or j["first_seen_at"]))

    unscored.sort(key=order)
    stale.sort(key=order)
    queue = stale + unscored if rescore else unscored  # --rescore fixes stale scores first
    p.to_score = queue[: profile.scoring.max_llm_calls_per_run]
    p.backlog = len(unscored)
    p.stale = len(stale)
    return p


def final_score(out: ScoreOutput, high_priority: bool) -> tuple[int, str, int]:
    """Apply code-side guardrails, then derive the verdict from the score.
    Returns (score, verdict, score before the seniority cap)."""
    uncapped = min(out.score + (PRIORITY_BOOST if high_priority else 0), 100)
    score = min(uncapped, OVER_SENIORITY_CAP) if out.seniority_match == "over" else uncapped
    verdict = "apply" if score >= 75 else "maybe" if score >= 50 else "skip"
    return score, verdict, uncapped


def _ts(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


@dataclass
class ScoreCounts:
    filtered_out: int = 0
    scored_local: int = 0
    escalated: int = 0
    score_failed: int = 0
    quota_skipped: int = 0  # left unscored because Ollama Cloud ran out of credits


def score_jobs(
    conn: sqlite3.Connection,
    profile: Profile,
    companies: list[Company],
    llm: LLM,
    rescore: bool = False,
    show_progress: bool = True,
) -> ScoreCounts:
    p = plan(conn, profile, companies, rescore=rescore)
    counts = ScoreCounts(filtered_out=p.filtered_now)
    if not p.to_score:
        return counts
    ph = config.profile_hash()
    resume = config.load_resume()
    high = {c.name for c in companies if c.priority == "high"}
    with Progress(disable=not show_progress) as bar:
        task = bar.add_task("Scoring", total=len(p.to_score))
        for i, job in enumerate(p.to_score):
            row = dict(
                job_id=job["id"],
                content_hash=job["content_hash"],
                profile_hash=ph,
                hard_filter_result="pass",
            )
            try:
                res = llm.complete_json(
                    build_prompt(job, profile, resume), ScoreOutput, task="score_job"
                )
            except QuotaError as e:
                # Not a failure of this job: leave it (and the rest) unscored for a later run.
                counts.quota_skipped = len(p.to_score) - i
                log.error(
                    "Ollama Cloud quota hit, %d jobs left unscored: %s", counts.quota_skipped, e
                )
                break
            except LLMError as e:
                log.error("score failed for %s / %s: %s", job["company"], job["title"], e)
                db.insert_score(conn, **row, status="score_failed", error=str(e)[:1000])
                counts.score_failed += 1
            else:
                out: ScoreOutput = res.obj
                score, verdict, uncapped = final_score(out, job["company"] in high)
                db.insert_score(
                    conn,
                    **row,
                    status="scored",
                    score=score,
                    uncapped_score=uncapped,
                    verdict=verdict,
                    reasons=out.reasons,
                    red_flags=out.red_flags,
                    seniority_match=out.seniority_match,
                    extracted_comp=out.extracted_comp,
                    one_line_pitch=out.one_line_pitch,
                    provider=res.provider,
                    model=res.model,
                    escalated=int(res.escalated),
                    latency_ms=res.latency_ms,
                )
                log.info(
                    "scored %s / %s: %d via %s in %d ms",
                    job["company"],
                    job["title"],
                    score,
                    res.model,
                    res.latency_ms,
                )
                if res.escalated:
                    counts.escalated += 1
                else:
                    counts.scored_local += 1
            bar.advance(task)
    return counts
