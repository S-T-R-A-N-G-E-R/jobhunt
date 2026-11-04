"""`jobhunt apply`: one sitting of assisted applications. jobhunt fills, you press Submit."""

import logging
import subprocess
import time
from contextlib import contextmanager
from types import SimpleNamespace

from rich.console import Console
from rich.table import Table

from .. import answers, cloud, config, db, decisions, digest, materials
from ..materials import MaterialsError, Writer
from . import ADAPTERS, form, store

log = logging.getLogger(__name__)
console = Console()

STATUS_STYLE = {"filled": "green", "kept": "green", "flagged": "yellow", "empty": "red"}


class Steps:
    """Progress lines: a spinner while a step runs, then ✓ (or ✗) with how long it took. Every
    step is kept for the timing table at the end of the session."""

    def __init__(self):
        self.done: list[tuple[str, float]] = []
        self.prefix = ""  # the company, while working on a job

    def record(self, text: str, took: float) -> None:
        self.done.append((f"{self.prefix}: {text}" if self.prefix else text, took))

    @contextmanager
    def __call__(self, text: str):
        """`with steps("Pulling the DB") as s:` … set `s.note` to add to the ✓ line."""
        info, start = SimpleNamespace(note=""), time.monotonic()
        try:
            with console.status(f"{text}…"):
                yield info
        except BaseException:
            took = time.monotonic() - start
            console.print(f"[red]✗[/] {text} [dim]({took:.1f}s)[/]")
            self.record(f"{text} (failed)", took)
            raise
        took = time.monotonic() - start
        note = f" · {info.note}" if info.note else ""
        console.print(f"[green]✓[/] {text}{note} [dim]({took:.1f}s)[/]")
        self.record(text, took)

    def table(self) -> None:
        if not self.done:
            return
        t = Table(title="Time per step")
        t.add_column("Step", overflow="fold")
        t.add_column("Time", justify="right")
        for text, took in self.done:
            t.add_row(text, f"{took:.1f}s")
        t.add_row("[bold]Total[/]", f"[bold]{sum(x for _, x in self.done):.1f}s[/]")
        console.print(t)


def company_slug(job) -> tuple[str, str | None] | None:
    """(slug, region) for the job's board, from companies.yaml or else from its URL."""
    for c in config.load_companies_raw(create=True)["companies"]:
        if c.get("ats") == job["source"] and c.get("name") == job["company"]:
            return c["slug"], c.get("region")
    from ..sources import detect_ats

    hit = detect_ats(job["url"])
    return (hit[1], hit[2]) if hit and hit[0] == job["source"] else None


def open_form(page, job, timeout_ms: int = 15000):
    """Go to the job's application form and return the frame holding it (None = unrecognised).
    Tries the ATS's own form pages first, then the job's URL, where the form may be embedded
    in an iframe on the company's site."""
    adapter = ADAPTERS.get(job["source"])
    if adapter is None:
        page.goto(job["url"], wait_until="domcontentloaded")
        return None
    slug = company_slug(job)
    urls = adapter.form_urls(slug[0], job["external_id"], slug[1]) if slug else []
    for url in [*urls, job["url"]]:
        page.goto(url, wait_until="domcontentloaded")
        frame = wait_for_form(page, adapter, timeout_ms)
        if frame is not None:
            return frame
    return None


def wait_for_form(page, adapter, timeout_ms: int):
    """The frame (main page or an ATS iframe) that has form fields, polling until timeout."""
    waited = 0
    while waited <= timeout_ms:
        for fr in page.frames:
            host = fr.url.split("/")[2] if "://" in fr.url else ""
            if adapter.HOST.search(host) and fr.locator(adapter.CONTAINER).count():
                return fr
        page.wait_for_timeout(500)
        waited += 500
    return None


def print_checklist(report: form.Report, m) -> None:
    t = Table(title="Checklist", show_lines=False)
    for col in ("", "Question", "Value", "From / note"):
        t.add_column(col, overflow="fold")
    for r in report.rows:
        style = STATUS_STYLE.get(r.status, "")
        mark = {"filled": "✓", "kept": "✓", "flagged": "!", "empty": "✗"}[r.status]
        req = "*" if r.required else ""
        t.add_row(f"[{style}]{mark}[/]", f"{r.label}{req}", r.value, r.note or r.source)
    console.print(t)
    counts = {s: sum(r.status == s for r in report.rows) for s in STATUS_STYLE}
    console.print(
        f"[green]{counts['filled'] + counts['kept']} filled[/] · "
        f"[yellow]{counts['flagged']} flagged (orange)[/] · "
        f"[red]{counts['empty']} left empty (red)[/]"
    )
    if m and m.claims:
        console.print(f"[yellow]Cover letter: claims the check couldn't verify: {m.claims}[/]")
    if m and m.letter:
        console.print(f"Cover letter: {m.letter_md}")
    if report.captcha:
        console.print(
            "[yellow]This form has a CAPTCHA. If a challenge appears, solve it yourself.[/]"
        )
    if report.blocked_submits:
        console.print(f"[red]Blocked {report.blocked_submits} submit attempt(s) while filling.[/]")


def print_unrecognised(job, m, a) -> None:
    console.print(
        "[yellow]I don't recognise this page as a Greenhouse, Lever or Ashby form, so nothing "
        "was filled. Everything is ready to copy:[/]"
    )
    i, w = a.identity, a.work
    for k, v in [("Name", i.full_name), ("Email", i.email), ("Phone", i.phone),
                 ("LinkedIn", i.linkedin), ("GitHub", i.github), ("Notice", w.notice_period),
                 ("Resume", a.resume_for(job["title"]))]:  # fmt: skip
        if v:
            console.print(f"  {k}: {v}")
    if m and m.letter:
        console.print(
            f"  Cover letter: {m.letter_md}" + (f" / {m.letter_pdf}" if m.letter_pdf else "")
        )


def materials_steps(job, a, writer: Writer, fields, pw, steps: Steps, letter: bool = True):
    """The cover letter (when `letter`), then drafts for questions not drafted yet, each as its
    own step."""
    if materials.job_dir(job).joinpath("cover_letter.md").exists():
        with steps("Loading the saved cover letter"):
            m = materials.prepare(job, a, writer, (), pw)
    elif not letter:
        m = materials.prepare(job, a, writer, (), pw, letter=False)
        console.print("[dim]– No cover letter: this form doesn't take one.[/]")
    else:
        with steps(f"Writing the cover letter with {writer.describe()}") as s:
            m = materials.prepare(job, a, writer, (), pw)
            s.note = f"written by {m.letter_model.split('/')[-1]}"
            if m.claims:
                s.note += f", {len(m.claims)} claim(s) flagged"
    questions = form.to_draft(fields, a, job)
    new = [q for q in questions if q[0] not in m.drafts]
    if new:
        with steps(f"Drafting {len(new)} answer(s) with {writer.describe()}") as s:
            m = materials.prepare(job, a, writer, questions, pw, letter=False)
            used = {m.drafts[q]["model"].split("/")[-1] for q, _ in new if q in m.drafts}
            s.note = f"by {', '.join(sorted(used))}" if used else "none could be drafted"
    return m


def review_letter(m, job, a, writer, pw):
    """The claims check flagged the letter: open it for you to fix before it's uploaded."""
    console.print("[yellow]The cover letter makes claims your resume doesn't support:[/]")
    for c in m.claims:
        console.print(f"  [yellow]- {c}[/]")
    subprocess.run(["open", "-t", str(m.letter_md)], check=False)  # your default text editor
    ask("Edit and save the letter (or leave it), then press Enter here: ")
    return materials.prepare(job, a, writer, (), pw)  # picks up your edit, re-renders the PDF


def ask(prompt: str) -> str:
    return console.input(prompt).strip().lower()


def process(job, ctx, pw, writer: Writer, a, apps, steps: Steps | None = None, missing=None) -> str:
    """One job. Returns applied | skipped | failed | quit. `missing` collects answers.yaml keys
    the form needed and didn't find: {key: [company, ...]}."""
    steps = steps or Steps()
    missing = {} if missing is None else missing
    console.rule(f"[bold]{job['company']} · {job['title']}[/] (score {job['score']})")
    steps.prefix = job["company"]
    page = ctx.new_page()
    try:
        with steps("Opening the application form") as s:
            frame = open_form(page, job)
            fields = form.scan(frame, ADAPTERS[job["source"]]) if frame else []
            s.note = f"{len(fields)} fields" if frame else "not a form I recognise"
        # An unrecognised page still gets a letter: you may paste it there yourself.
        wants_letter = not frame or form.uses_letter(fields)
        try:
            m = materials_steps(job, a, writer, fields, pw, steps, letter=wants_letter)
        except MaterialsError as e:
            console.print(f"[yellow]{e}. Continuing without a cover letter.[/]")
            m = None
        if m and m.claims and wants_letter:
            m = review_letter(m, job, a, writer, pw)
        page.bring_to_front()
        if frame:
            with steps("Filling the form"):
                report = form.fill(frame, page, ADAPTERS[job["source"]], job, a, m)
            print_checklist(report, m)
            for key in report.missing:
                missing.setdefault(key, []).append(job["company"])
        else:
            print_unrecognised(job, m, a)
        form_url = page.url
        console.print(
            "\n[bold]Review the form and click Submit yourself.[/] "
            "Press Enter here when done, or s to skip (q stops the session)."
        )
        start = time.monotonic()
        reply = ask("> ")
        steps.record("your review", time.monotonic() - start)
        if reply == "q":
            return "quit"
        if reply == "s":
            store.record(apps, job, "skipped", "skipped in the session")
            return "skipped"
        done = form.submitted(page, form_url)
        if not done:
            done = ask("Did you submit? y/n ") == "y"
        if not done:
            store.record(apps, job, "failed", "not submitted")
            return "failed"
        shot = None
        if not page.is_closed():
            target = m.dir if m else config.OUTPUT_DIR / "applications"
            target.mkdir(parents=True, exist_ok=True)
            shot = target / "confirmation.png"
            page.screenshot(path=str(shot), full_page=True)
        letter_used = m.letter_md if m and m.letter and wants_letter else ""
        store.record(apps, job, "applied", cover_letter=letter_used, screenshot=shot)
        console.print("[green]Recorded as applied.[/]")
        return "applied"
    except Exception as e:
        log.exception("applying to job %s failed", job["id"])
        store.record(apps, job, "failed", f"{type(e).__name__}: {e}"[:300])
        console.print(f"[red]Failed: {type(e).__name__}: {str(e)[:200]}[/]")
        return "failed"
    finally:
        steps.prefix = ""
        if not page.is_closed():
            page.close()


def sync(pull: bool, steps: Steps) -> tuple:
    """Pull the cloud DB, merge its button presses and read new ones. Returns (jobs, apps)."""
    if pull:
        try:
            with steps("Pulling the latest jobs from the cloud"):
                cloud.pull_db()
        except cloud.CloudError as e:
            console.print(f"[yellow]Couldn't pull the cloud DB ({e}); using the local one.[/]")
    jobs, apps = db.connect(), store.connect()
    try:
        with steps("Reading Apply / Skip decisions from Telegram") as s:
            merged = store.merge_cloud(apps, jobs)
            new = decisions.poll(apps)
            s.note = f"{merged} from the cloud, {new} new"
    except digest.DigestError as e:
        console.print(f"[yellow]Couldn't read Telegram buttons: {e}[/]")
    return jobs, apps


def print_missing(missing: dict[str, list[str]]) -> None:
    """Once per session: the answers.yaml keys forms asked for and didn't find."""
    if not missing:
        return
    console.print(
        f"\n[yellow]Add these to {config.ANSWERS_PATH.name} so they're filled next time:[/]"
    )
    for key, companies in sorted(missing.items()):
        hint = "a custom: q/a entry" if key == "custom" else key
        console.print(f"  [yellow]- {hint}[/] (asked by {', '.join(sorted(set(companies)))})")


def show_queue(queue) -> None:
    t = Table(title=f"Approved, not applied yet ({len(queue)})")
    for col in ("Job", "Company", "Title", "Score", "Location"):
        t.add_column(col)
    for j in queue:
        t.add_row(str(j["id"]), j["company"], j["title"], str(j["score"]), j["location"] or "")
    console.print(t)


def run(limit: int | None = None, pull: bool = True, job_id: int | None = None, launch=None):
    """The whole sitting. `launch(pw)` returns a browser context (tests pass a headless one)."""
    steps = Steps()
    a = answers.load()
    profile = config.load_profile()
    jobs, apps = sync(pull, steps)
    if job_id is not None:
        if store.job(jobs, job_id) is None:
            console.print(f"[red]No job {job_id} in the DB.[/]")
            return
        decisions.record_terminal(apps, job_id, "approve")  # naming it is approving it
    queue = store.queue(apps, jobs)
    if job_id is not None:
        queue = [j for j in queue if j["id"] == job_id]
    if not queue:
        console.print("Nothing approved to apply to. Press ✅ Apply in Telegram, or run "
                      "`jobhunt apply pick`.")  # fmt: skip
        return
    show_queue(queue)
    limit = limit or a.settings.max_per_session
    if len(queue) > limit:
        console.print(f"Doing the first {limit} (apply.max_per_session); the rest stay queued.")
    from playwright.sync_api import sync_playwright

    results, missing = [], {}
    with steps("Opening the browser"):
        pw = sync_playwright().start()
        ctx = (launch or _launch)(pw)
    try:
        writer = Writer(a, profile)
        for job in queue[:limit]:
            outcome = process(job, ctx, pw, writer, a, apps, steps, missing)
            if outcome == "quit":
                break
            results.append(outcome)
    finally:
        ctx.close()
        pw.stop()
    steps.table()
    print_missing(missing)
    summary(apps, results)


def _launch(pw):
    config.BROWSER_DIR.mkdir(exist_ok=True)
    return pw.chromium.launch_persistent_context(
        str(config.BROWSER_DIR), headless=False, viewport=None, accept_downloads=False
    )


def summary(apps, results: list[str]) -> str:
    applied, skipped = results.count("applied"), results.count("skipped")
    text = (
        f"Session done: {applied} applied · {skipped} skipped · "
        f"{store.applied_this_week(apps)} this week"
    )
    console.print(f"\n[bold]{text}[/]")
    try:
        digest.send_telegram(text)
    except digest.DigestError as e:
        console.print(f"[yellow]Couldn't send the summary to Telegram: {e}[/]")
    return text


def prepare_only(job_id: int, letter: bool = False) -> None:
    """`jobhunt apply prepare <id>`: materials without filling, reading questions headless.
    The letter is written only if the form takes one (or isn't recognised), or `letter`."""
    a = answers.load()
    profile = config.load_profile()
    job = store.job(db.connect(), job_id)
    if job is None:
        console.print(f"[red]No job {job_id} in the DB.[/]")
        return
    from playwright.sync_api import sync_playwright

    steps = Steps()
    with sync_playwright() as pw:
        with steps("Reading the application form (headless)") as s:
            browser = pw.chromium.launch()
            try:
                frame = open_form(browser.new_page(), job)
                fields = form.scan(frame, ADAPTERS[job["source"]]) if frame else []
            finally:
                browser.close()
            s.note = f"{len(fields)} fields" if frame else "not a form I recognise"
        wants_letter = letter or not frame or form.uses_letter(fields)
        m = materials_steps(job, a, Writer(a, profile), fields, pw, steps, letter=wants_letter)
    steps.table()
    print_missing({k: [job["company"]] for k in form.missing_keys(fields, a, job)})
    if m.letter:
        pdf = f" and {m.letter_pdf}" if m.letter_pdf else ""
        console.print(f"Cover letter: {m.letter_md}{pdf}")
    else:
        console.print("No cover letter written: add --letter to write one anyway.")
    if m.claims:
        console.print(f"[yellow]Flagged claims: {m.claims}[/]")
    for label, d in m.drafts.items():
        style = "yellow" if d["confidence"] == "low" else ""
        console.print(f"[{style}]{label}[/] ({d['confidence']}): {d['answer'] or '(empty)'}")
