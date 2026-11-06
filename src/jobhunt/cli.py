import asyncio
import json
import sys
from pathlib import Path

import typer
import yaml
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from . import cloud, config, db, digest, pipeline, schedule, scoring
from .llm import LLM, LLMError
from .models import ScoreOutput
from .net import Http
from .sources import MODULES, detect_ats

app = typer.Typer(no_args_is_help=True, help="Personal job search pipeline.")
company_app = typer.Typer(no_args_is_help=True, help="Manage companies.yaml.")
resume_app = typer.Typer(no_args_is_help=True, help="Resume helpers.")
llm_app = typer.Typer(no_args_is_help=True, help="LLM setup checks.")
schedule_app = typer.Typer(no_args_is_help=True, help="macOS launchd schedule.")
dev_app = typer.Typer(no_args_is_help=True, help="Developer helpers.")
cloud_app = typer.Typer(no_args_is_help=True, help="Run on GitHub Actions (docs/CLOUD.md).")
apply_app = typer.Typer(
    invoke_without_command=True,
    help="Fill applications in a visible browser; you press Submit (docs/APPLY.md).",
)
for sub, name in [
    (apply_app, "apply"),
    (cloud_app, "cloud"),
    (company_app, "company"),
    (resume_app, "resume"),
    (llm_app, "llm"),
    (schedule_app, "schedule"),
    (dev_app, "dev"),
]:
    app.add_typer(sub, name=name)

console = Console()


@app.callback()
def _setup():
    pipeline.setup_logging()


@app.command()
def fetch():
    """Fetch jobs from every company in companies.yaml."""
    conn = db.connect()
    companies = config.load_companies()
    fetched, new, errors = pipeline.fetch(conn, companies)
    console.print(
        f"Fetched {fetched} jobs ({new} new) from {len(companies) - len(errors)}"
        f"/{len(companies)} companies."
    )
    for name, err in errors.items():
        console.print(f"[red]✗ {name}[/red]: {err}")


@app.command()
def score(
    dry_run: bool = typer.Option(False, help="Show what would be scored, call no model."),
    rescore: bool = typer.Option(
        False, help="Also rescore jobs scored under an older profile.yaml / prompt."
    ),
):
    """Hard-filter and LLM-score new or changed jobs."""
    conn = db.connect()
    profile = config.load_profile()
    companies = config.load_companies()
    llm = LLM(profile.llm)
    if dry_run:
        p = scoring.plan(conn, profile, companies, rescore=rescore, write=False)
        table = Table("Company", "Title", "Location")
        for j in p.to_score:
            table.add_row(j["company"], j["title"], j["location"] or "")
        console.print(table)
        console.print(
            f"Would score {len(p.to_score)} jobs: ~{len(p.to_score)} LLM calls "
            f"(up to {2 * len(p.to_score)} with retries). Backlog {p.backlog}, "
            f"stale {p.stale}, newly filtered out {p.filtered_now}."
        )
        problem = llm.health_check()
        console.print(f"[yellow]{problem}[/yellow]" if problem else "Ollama: ready")
        return
    counts, problem = pipeline.score(conn, profile, companies, llm, rescore=rescore)
    if problem:
        console.print(f"[red]Scoring skipped:[/red] {problem}")
    console.print(
        f"Filtered out {counts.filtered_out}, scored {counts.scored_local} locally, "
        f"{counts.escalated} escalated, {counts.score_failed} failed."
    )


@app.command("digest")
def digest_cmd(preview: bool = typer.Option(False, help="Print instead of sending.")):
    """Send the Telegram digest of unsent top matches."""
    conn = db.connect()
    profile = config.load_profile()
    try:
        n = digest.send(conn, profile, preview=preview)
    except digest.DigestError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None
    if not preview:
        console.print(f"Sent {n} jobs.")


@app.command()
def run(
    if_not_run_today: bool = typer.Option(
        False, help="Skip if today's run already succeeded (used by the scheduler)."
    ),
    preview: bool = typer.Option(False, help="Print the digest instead of sending it."),
    quiet: bool = typer.Option(False, help="No progress bar."),
):
    """fetch + score + digest."""
    run_id = pipeline.run(
        if_not_run_today=if_not_run_today, preview=preview, show_progress=not quiet
    )
    if run_id is None:
        console.print("Already ran successfully today.")
        return
    r = db.get_run(db.connect(), run_id)
    _print_run(r)
    # Fail loudly (a red run in Actions) when nothing reached Telegram; the next slot retries.
    if "digest" in json.loads(r["errors"] or "{}"):
        console.print("[red]Telegram send failed.[/red]")
        raise typer.Exit(1)


def _print_run(r) -> None:
    console.print(
        f"Run {r['id']} ({r['run_date']}) [bold]{r['status']}[/bold]: {r['fetched']} fetched "
        f"from {r['companies']} companies, {r['new_jobs']} new, {r['filtered_out']} filtered "
        f"out, {r['scored_local']} scored locally, {r['escalated']} escalated, "
        f"{r['score_failed']} failed, {r['sent']} sent."
    )
    if r["ollama_status"]:
        console.print(f"[yellow]{r['ollama_status']}[/yellow]")
    if r["quota_skipped"]:
        console.print(
            f"[yellow]Ollama Cloud quota hit: {r['quota_skipped']} jobs not scored[/yellow]"
        )
    for k, v in json.loads(r["errors"] or "{}").items():
        console.print(f"[red]✗ {k}[/red]: {v}")


@app.command()
def status():
    """Last run summary and queue counts."""
    conn = db.connect()
    r = db.get_run(conn)
    if r:
        _print_run(r)
    else:
        console.print("No runs yet.")
    profile = config.load_profile()
    p = scoring.plan(conn, profile, config.load_companies(), write=False)
    active = conn.execute("SELECT count(*) FROM jobs WHERE is_active=1").fetchone()[0]
    sent = conn.execute("SELECT count(*) FROM digests").fetchone()[0]
    console.print(f"Active jobs: {active} · sent so far: {sent}")
    console.print(
        f"Unscored backlog: {p.backlog}"
        + (" (run `jobhunt score` to catch up)" if p.backlog else "")
    )
    console.print(
        f"Stale scores (profile/prompt changed): {p.stale}"
        + (" (run `jobhunt score --rescore`)" if p.stale else "")
    )


@app.command()
def top(n: int = typer.Option(20, help="How many to show.")):
    """Best current matches."""
    conn = db.connect()
    rows = conn.execute(
        """SELECT j.company, j.title, j.location, j.url, s.score, s.verdict, s.one_line_pitch
           FROM jobs j JOIN scores s ON s.id = (SELECT max(id) FROM scores WHERE job_id=j.id)
           WHERE j.is_active=1 AND s.status='scored' ORDER BY s.score DESC LIMIT ?""",
        (n,),
    ).fetchall()
    table = Table("Score", "Verdict", "Company", "Title", "Location", "Pitch", show_lines=True)
    for r in rows:
        table.add_row(
            str(r["score"]),
            r["verdict"],
            r["company"],
            f"[link={r['url']}]{r['title']}[/link]",
            r["location"] or "",
            r["one_line_pitch"] or "",
        )
    console.print(table)


@company_app.command("add")
def company_add(
    url: str,
    name: str = typer.Option(None, help="Display name (default: slug)."),
    priority: str = typer.Option("normal", help="high | normal"),
):
    """Detect the ATS from a careers URL, verify it, and add it to companies.yaml."""
    data = config.load_companies_raw(create=True)
    found = detect_ats(url)
    if not found:
        if url not in data["manual_check"]:
            data["manual_check"].append(url)
            _save_companies(data)
        console.print(
            f"[yellow]{url} isn't a Greenhouse, Lever or Ashby board.[/yellow] "
            "Added it to manual_check in companies.yaml."
        )
        raise typer.Exit(1)
    ats, slug, region = found
    if any(c["ats"] == ats and c["slug"] == slug for c in data["companies"]):
        console.print(f"{ats}/{slug} is already in companies.yaml.")
        return
    entry = {
        "name": name or slug.replace("-", " ").title(),
        "ats": ats,
        "slug": slug,
        "priority": priority,
    }
    if region:
        entry["region"] = region
    company = config.Company.model_validate(entry)
    jobs = asyncio.run(_fetch_one(company))
    if isinstance(jobs, Exception):
        console.print(f"[red]Couldn't read the {ats} board '{slug}': {jobs}[/red]")
        raise typer.Exit(1)
    if not jobs:
        console.print(f"[yellow]The {ats} board '{slug}' has 0 open jobs right now.[/yellow]")
    data["companies"].append(entry)
    _save_companies(data)
    console.print(f"Added {entry['name']} ({ats}/{slug}, {len(jobs)} open jobs).")


async def _fetch_one(company):
    from .sources import fetch_company

    http = Http()
    try:
        return await fetch_company(http, company)
    except Exception as e:  # noqa: BLE001
        return e
    finally:
        await http.aclose()


def _save_companies(data: dict) -> None:
    config.COMPANIES_PATH.write_text(
        config.companies_header() + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    )


@resume_app.command("import")
def resume_import(pdf: Path):
    """Extract text from your resume PDF into data/resume.md for you to review."""
    from pypdf import PdfReader

    text = "\n\n".join(
        (page.extract_text(extraction_mode="layout") or "").strip() for page in PdfReader(pdf).pages
    )
    if config.RESUME_PATH.exists() and not typer.confirm(f"Overwrite {config.RESUME_PATH}?"):
        raise typer.Exit()
    config.RESUME_PATH.parent.mkdir(exist_ok=True)
    config.RESUME_PATH.write_text(text.strip() + "\n")
    console.print(
        f"Wrote {len(text)} characters to {config.RESUME_PATH}. "
        "Open it and clean up anything the PDF extraction mangled."
    )


class _Ping(BaseModel):
    ok: bool
    capital_of_france: str


@llm_app.command("test")
def llm_test():
    """Check the local and paid providers with a tiny schema-constrained prompt."""
    profile = config.load_profile()
    llm = LLM(profile.llm)
    prompt = "Return ok=true and the capital of France."
    problem = llm.health_check()
    if problem:
        console.print(f"[red]Local:[/red] {problem}")
    else:
        local_only = LLM(
            profile.llm.model_copy(
                update={"fallback": profile.llm.fallback.model_copy(update={"enabled": False})}
            )
        )
        try:
            r = local_only.complete_json(prompt, _Ping, task="llm_test")
            console.print(f"[green]Local OK[/green] {r.model}: {r.obj} in {r.latency_ms} ms")
        except LLMError as e:
            console.print(f"[red]Local failed:[/red] {e}")
    if not config.env("ANTHROPIC_API_KEY"):
        console.print("Paid fallback: ANTHROPIC_API_KEY not set (that's fine; it's optional).")
        return
    import time

    start = time.monotonic()
    try:
        obj = _Ping.model_validate(llm.paid(prompt, _Ping))
        ms = int((time.monotonic() - start) * 1000)
        console.print(
            f"[green]Paid OK[/green] {profile.llm.fallback.model}: {obj} in {ms} ms"
            + (
                ""
                if profile.llm.fallback.enabled
                else " (note: fallback.enabled is false in profile.yaml)"
            )
        )
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]Paid failed:[/red] {type(e).__name__}: {e}")


@app.command()
def calibrate(n: int = typer.Option(15, help="How many jobs to compare.")):
    """Score the same jobs with the local and paid models and compare."""
    if not config.env("ANTHROPIC_API_KEY"):
        console.print("calibrate needs ANTHROPIC_API_KEY in .env (it calls the paid model).")
        raise typer.Exit(1)
    conn = db.connect()
    profile = config.load_profile()
    llm = LLM(profile.llm)
    problem = llm.health_check()
    if problem:
        console.print(f"[red]{problem}[/red]")
        raise typer.Exit(1)
    jobs = conn.execute(
        """SELECT j.* FROM jobs j JOIN scores s ON s.id = (SELECT max(id) FROM scores
           WHERE job_id=j.id) WHERE j.is_active=1 AND s.status != 'filtered'
           ORDER BY j.first_seen_at DESC LIMIT ?""",
        (n,),
    ).fetchall()
    if not jobs:
        console.print("No filter-passing jobs yet: run `jobhunt run` first.")
        raise typer.Exit(1)
    resume = config.load_resume()
    table = Table("Company", "Title", "Local", "Paid", "Δ")
    agree, diffs = 0, []
    for j in jobs:
        prompt = scoring.build_prompt(j, profile, resume)
        try:
            local = ScoreOutput.model_validate_json(
                llm.local([{"role": "user", "content": prompt}], ScoreOutput)
            )
            paid = ScoreOutput.model_validate(llm.paid(prompt, ScoreOutput))
        except Exception as e:  # noqa: BLE001
            table.add_row(j["company"], j["title"], "error", "error", str(e)[:40])
            continue
        agree += local.verdict == paid.verdict
        diffs.append(abs(local.score - paid.score))
        table.add_row(
            j["company"],
            j["title"],
            f"{local.score} {local.verdict}",
            f"{paid.score} {paid.verdict}",
            str(local.score - paid.score),
        )
    console.print(table)
    if diffs:
        console.print(
            f"Verdict agreement: {agree}/{len(diffs)} ({100 * agree // len(diffs)}%) · "
            f"mean |score diff|: {sum(diffs) / len(diffs):.1f}"
        )


@schedule_app.command("install")
def schedule_install(
    cloud_backup: bool = typer.Option(
        False,
        "--cloud",
        help="Instead of running locally, start the cloud workflow at 09:30 and 14:00 "
        "(a backup for GitHub's dropped crons).",
    ),
):
    """Install and load the launchd agent (08:00 daily + hourly catch-up, or --cloud)."""
    console.print(schedule.install(cloud_backup))


@schedule_app.command("uninstall")
def schedule_uninstall():
    """Unload and remove the launchd agent."""
    console.print(schedule.uninstall())


@schedule_app.command("status")
def schedule_status():
    """Show whether the launchd agent is loaded."""
    console.print(schedule.status())


def _cloud(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except cloud.CloudError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from None


@cloud_app.command("push-secrets")
def cloud_push_secrets():
    """Upload profile, resume, companies and the Telegram/API keys as runner-repo secrets."""
    names = _cloud(cloud.push_secrets)
    console.print(f"Set {len(names)} secrets in {cloud.repo()}: {', '.join(names)}")


@cloud_app.command("seed-db")
def cloud_seed_db():
    """Upload the local DB as the cloud's starting state and start a run that uses it."""
    size = _cloud(cloud.seed_db)
    console.print(
        f"Uploaded the DB ({size / 1e6:.1f} MB gzipped) and started a seeded run. "
        "Watch it with: gh run watch --repo " + cloud.repo()
    )


@cloud_app.command("pull-db")
def cloud_pull_db():
    """Replace data/jobhunt.db with the newest cloud DB (the current one is backed up first)."""
    backup = _cloud(cloud.pull_db)
    console.print(f"Pulled the cloud DB into {config.DB_PATH}.")
    if backup:
        console.print(f"Previous DB backed up to {backup}")


@cloud_app.command("run")
def cloud_run(force: bool = typer.Option(False, help="Run even if today already succeeded.")):
    """Start the cloud workflow now."""
    _cloud(cloud.trigger, force=force)
    console.print(f"Started. Watch it with: gh run watch --repo {cloud.repo()}")


@cloud_app.command("write-files", hidden=True)
def cloud_write_files():
    """(Runner) Write the personal files from secrets in the environment."""
    console.print(f"Wrote {len(_cloud(cloud.write_files))} files.")


@cloud_app.command("restore-db", hidden=True)
def cloud_restore_db(seed: bool = False, allow_fresh: bool = False):
    """(Runner) Restore the DB: seed > cache > newest artifact > fresh."""
    console.print(f"DB restored from: {_cloud(cloud.restore_db, seed, allow_fresh)}")


@dev_app.command("save-fixture")
def save_fixture(ats: str, slug: str, keep: int = typer.Option(3, help="Jobs to keep.")):
    """Save a trimmed real API response to output/fixtures/<ats>-<slug>.json (git-ignored).

    Use it to inspect a board's structure. Committed test fixtures are synthetic.
    """
    if ats not in MODULES:
        console.print(f"ats must be one of {', '.join(MODULES)}")
        raise typer.Exit(1)

    async def get():
        http = Http()
        try:
            return await http.get_json(MODULES[ats].api_url(slug))
        finally:
            await http.aclose()

    data = asyncio.run(get())
    if isinstance(data, list):
        data = data[:keep]
    else:
        data["jobs"] = data["jobs"][:keep]
    path = config.OUTPUT_DIR / "fixtures" / f"{ats}-{slug}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    console.print(f"Saved {path}")


@dev_app.command("install-hooks")
def install_hooks():
    """Install a pre-commit hook that blocks commits containing values from your personal files."""
    from . import hooks

    n = len(hooks.values())
    console.print(f"Installed {hooks.install()} (checks {n} values from your personal files)")


if __name__ == "__main__":
    sys.exit(app())


# ---- apply (Phase 3 / 4) ----------------------------------------------------------------------


@apply_app.callback()
def apply_cmd(
    ctx: typer.Context,
    limit: int = typer.Option(None, help="Forms to open (default: settings.max_per_session)."),
    no_pull: bool = typer.Option(False, "--no-pull", help="Don't pull the cloud DB first."),
    job: int = typer.Option(None, help="Apply to this job id only (approves it)."),
):
    """Pull jobs, read button presses, then fill each approved job's form for you to submit."""
    if ctx.invoked_subcommand is None:
        from .apply import session

        session.run(limit=limit, pull=not no_pull, job_id=job)


@apply_app.command("queue")
def apply_queue():
    """Approved jobs not applied to yet (uses the local DBs; `jobhunt apply` pulls first)."""
    from .apply import session, store

    session.show_queue(store.queue(store.connect(), db.connect()))


@apply_app.command("pick")
def apply_pick():
    """Go through digest jobs you haven't decided on: a = apply, s = skip, n = later, q = quit."""
    import click

    from . import decisions
    from .apply import store

    apps, jobs = store.connect(), db.connect()
    todo = store.undecided(apps, jobs)
    if not todo:
        console.print("No undecided jobs from the digests.")
        return
    for i, j in enumerate(todo, 1):
        console.print(
            f"\n[bold]{i}/{len(todo)} {j['company']} · {j['title']}[/] "
            f"(score {j['score']}, {j['location'] or 'location not stated'})\n{j['url']}"
        )
        console.print("[a]pply  [s]kip  [n]ext  [q]uit: ", end="")
        key = click.getchar().lower()
        console.print(key)
        if key == "q":
            break
        if key in ("a", "s"):
            decisions.record_terminal(apps, j["id"], "approve" if key == "a" else "skip")
    console.print(f"Queue: {len(store.queue(apps, jobs))} approved. Run `jobhunt apply`.")


@apply_app.command("status")
def apply_status(n: int = typer.Option(30, help="How many rows to show.")):
    """Applications by date (applied, skipped, failed) and this week's counts."""
    from .apply import store

    apps, jobs = store.connect(), db.connect()
    rows = store.history(apps, jobs)
    t = Table(title="Applications")
    for col in ("When", "Status", "Company", "Title", "Note"):
        t.add_column(col, overflow="fold")
    for r in rows[:n]:
        t.add_row(db.local_date(r["updated_at"]), r["status"], r["company"], r["title"],
                  r["reason"] or "")  # fmt: skip
    console.print(t)
    week = [r for r in rows if r["updated_at"] >= store.week_start()]
    counts = {s: sum(r["status"] == s for r in week) for s in ("applied", "skipped", "failed")}
    console.print(
        f"This week: {counts['applied']} applied · {counts['skipped']} skipped · "
        f"{counts['failed']} failed · {len(store.queue(apps, jobs))} still queued"
    )


@apply_app.command("prepare")
def apply_prepare(
    job_id: int,
    letter: bool = typer.Option(
        False, "--letter", help="Write the cover letter even if the form doesn't take one."
    ),
):
    """Draft answers (and the cover letter, if the form takes one) without filling."""
    from .apply import session

    session.prepare_only(job_id, letter=letter)
