# ruff: noqa: F811  (pytest fixtures imported from test_apply_forms are parameters here)
import json
from types import SimpleNamespace

import pytest

from jobhunt import config, db, decisions
from jobhunt.apply import ADAPTERS, form, session, store

from .test_apply_forms import FORMS, browser, files, server  # noqa: F401  (fixtures)

ANSWERS = """
settings: {max_per_session: 5}
identity: {full_name: Alex Example, email: alex@example.com, city: Bengaluru, country: India}
work: {current_company: Example Corp, notice_period: 30 days}
"""


@pytest.fixture
def jobs_db(home):
    (home / "config" / "answers.yaml").write_text(ANSWERS)
    conn = db.connect()
    with conn:
        for i in range(1, 8):
            conn.execute(
                "INSERT INTO jobs (id, source, company, external_id, title, location, url,"
                " description_text, content_hash, first_seen_at, last_seen_at)"
                " VALUES (?, 'greenhouse', 'Fictional Labs', ?, ?, 'Bengaluru', ?, 'Build AI.',"
                " 'h', 'x', 'x')",
                (i, str(i), f"Role {i}", f"https://job-boards.greenhouse.io/fictional/jobs/{i}"),
            )
            conn.execute(
                "INSERT INTO digests (job_id, score, sent_at) VALUES (?, 80, ?)",
                (i, f"2026-11-0{i % 9}T00:00:00+00:00"),
            )
    return conn


@pytest.fixture
def quiet(monkeypatch):
    """No Telegram: record the summary instead."""
    sent = []
    monkeypatch.setattr(decisions, "poll", lambda conn, transport=None: 0)
    monkeypatch.setattr(session.digest, "send_telegram", lambda text, *a, **k: sent.append(text))
    return sent


class FakeCtx:
    def close(self):
        pass


def test_session_stops_at_the_limit(jobs_db, quiet, monkeypatch):
    apps = store.connect()
    for i in range(1, 8):
        decisions.record(apps, i, "approve", i, f"2026-11-04T00:00:0{i}")
    done = []

    def fake_process(job, ctx, pw, writer, a, apps, steps=None, missing=None):
        done.append(job["id"])
        store.record(apps, job, "applied")
        return "applied"

    monkeypatch.setattr(session, "process", fake_process)
    session.run(pull=False, launch=lambda pw: FakeCtx())
    assert done == [1, 2, 3, 4, 5]  # max_per_session, oldest decision first
    assert [j["id"] for j in store.queue(apps, jobs_db)] == [6, 7]
    assert quiet == ["Session done: 5 applied · 0 skipped · 5 this week"]
    session.run(pull=False, limit=1, launch=lambda pw: FakeCtx())
    assert done[-1] == 6


def test_quit_ends_the_session_without_recording(jobs_db, quiet, monkeypatch):
    apps = store.connect()
    decisions.record(apps, 1, "approve", 1, "t")
    decisions.record(apps, 2, "approve", 2, "t2")
    monkeypatch.setattr(session, "process", lambda *a: "quit")
    session.run(pull=False, launch=lambda pw: FakeCtx())
    assert len(store.queue(apps, jobs_db)) == 2
    assert quiet == ["Session done: 0 applied · 0 skipped · 0 this week"]


def test_cloud_decisions_merge_and_skips_leave_the_queue(jobs_db):
    decisions.record(jobs_db, 1, "approve", 10, "t")  # read by the cloud, came with pull-db
    decisions.record(jobs_db, 2, "skip", 11, "t")
    apps = store.connect()
    assert store.merge_cloud(apps, jobs_db) == 2
    assert store.merge_cloud(apps, jobs_db) == 0  # idempotent
    assert [j["id"] for j in store.queue(apps, jobs_db)] == [1]
    undecided = [j["id"] for j in store.undecided(apps, jobs_db)]
    assert 1 not in undecided and 2 not in undecided and len(undecided) == 5
    store.record(apps, store.job(jobs_db, 1), "applied")
    assert store.queue(apps, jobs_db) == []
    statuses = {(r["job_id"], r["status"]) for r in store.history(apps, jobs_db)}
    assert statuses == {(1, "applied"), (2, "skipped")}


def test_failed_jobs_stay_queued(jobs_db):
    apps = store.connect()
    decisions.record(apps, 3, "approve", 1, "t")
    store.record(apps, store.job(jobs_db, 3), "failed", "browser closed")
    assert [j["id"] for j in store.queue(apps, jobs_db)] == [3]


def test_job_option_approves_and_runs_only_that_job(jobs_db, quiet, monkeypatch):
    done = []
    monkeypatch.setattr(session, "process", lambda job, *a: done.append(job["id"]) or "skipped")
    session.run(pull=False, job_id=4, launch=lambda pw: FakeCtx())
    assert done == [4]


def test_submitted_detection(browser, server):
    page = browser.new_page()
    page.goto(f"{server}/greenhouse.html")
    form_url = page.url
    assert not form.submitted(page, form_url)  # still the form
    page.goto(f"{server}/thanks.html")
    assert form.submitted(page, form_url)  # thank-you text, no upload fields left


def test_process_end_to_end(jobs_db, quiet, browser, server, files, monkeypatch):
    """One job through the real flow: open, prepare (mocked LLM), fill, hand over, record."""
    job = store.job(jobs_db, 1)
    page_holder = {}

    def fake_open(page, job, timeout_ms=0):
        page.goto(f"{server}/greenhouse.html")
        page_holder["page"] = page
        return page.main_frame

    def fake_prepare(job, a, writer, questions, pw, letter=True):
        d = config.OUTPUT_DIR / "applications" / "x"
        d.mkdir(parents=True, exist_ok=True)
        (d / "cover_letter.md").write_text("Dear team")
        return session.materials.Materials(dir=d, letter="Dear team", letter_pdf=files)

    replies = iter(["", "y"])  # Enter after "submitting", then "yes, I submitted"
    monkeypatch.setattr(session, "open_form", fake_open)
    monkeypatch.setattr(session.materials, "prepare", fake_prepare)
    monkeypatch.setattr(session, "ask", lambda prompt: next(replies))
    ctx = browser.new_context()
    apps = store.connect()
    writer = SimpleNamespace(describe=lambda: "test-model")
    steps = session.Steps()
    assert session.process(job, ctx, None, writer, session.answers.load(), apps, steps) == "applied"
    names = [text for text, _ in steps.done]
    assert names == [
        "Fictional Labs: Opening the application form",
        "Fictional Labs: Writing the cover letter with test-model",
        "Fictional Labs: Drafting 2 answer(s) with test-model",
        "Fictional Labs: Filling the form",
        "Fictional Labs: your review",
    ]
    row = apps.execute("SELECT * FROM applications WHERE job_id = 1").fetchone()
    assert row["status"] == "applied" and row["cover_letter"].endswith("cover_letter.md")
    assert row["screenshot"].endswith("confirmation.png")
    assert (config.OUTPUT_DIR / "applications" / "x" / "confirmation.png").exists()
    page = page_holder["page"]
    assert page.is_closed()
    ctx.close()


def test_skip_in_session_is_recorded(jobs_db, quiet, browser, server, files, monkeypatch):
    monkeypatch.setattr(session, "open_form", lambda page, job, timeout_ms=0: None)

    def no_model(*a):
        raise session.MaterialsError("no model could write the cover letter")

    monkeypatch.setattr(session.materials, "prepare", no_model)  # carries on without a letter
    monkeypatch.setattr(session, "ask", lambda prompt: "s")
    ctx = browser.new_context()
    apps = store.connect()
    job = store.job(jobs_db, 2)
    writer = SimpleNamespace(describe=lambda: "test-model")
    assert session.process(job, ctx, None, writer, session.answers.load(), apps) == "skipped"
    assert apps.execute("SELECT status FROM applications").fetchone()[0] == "skipped"
    ctx.close()


def test_adapters_build_hosted_form_urls():
    gh = ADAPTERS["greenhouse"].form_urls("fictional", "123")
    assert gh[0] == "https://job-boards.greenhouse.io/fictional/jobs/123"
    assert "embed/job_app?for=fictional&token=123" in gh[1]
    assert ADAPTERS["lever"].form_urls("imag", "abc", "eu") == [
        "https://jobs.eu.lever.co/imag/abc/apply"
    ]
    assert ADAPTERS["ashby"].form_urls("ph", "u-1") == [
        "https://jobs.ashbyhq.com/ph/u-1/application"
    ]
    assert json.dumps(sorted(ADAPTERS)) == '["ashby", "greenhouse", "lever"]'


def test_steps_time_each_step_and_mark_failures(capsys):
    steps = session.Steps()
    with steps("Pulling") as s:
        s.note = "3 new"
    with pytest.raises(RuntimeError), steps("Reading"):
        raise RuntimeError("boom")
    steps.prefix = "Fictional Labs"
    steps.record("your review", 12.0)
    assert [t for t, _ in steps.done] == [
        "Pulling",
        "Reading (failed)",
        "Fictional Labs: your review",
    ]
    steps.table()
    out = capsys.readouterr().out
    assert "✓ Pulling · 3 new" in out and "✗ Reading" in out
    assert "Time per step" in out and "12.0s" in out and "Total" in out


def test_no_letter_is_written_when_the_form_takes_none(
    jobs_db,
    quiet,
    browser,
    server,
    files,
    monkeypatch,
):
    """Ashby fixture: no cover-letter upload, box or "Additional information" field. Only
    free-text questions get drafted: no letter, no PDF, no editor."""
    from jobhunt import materials

    from .conftest import FakeLLM

    llm = FakeLLM([json.dumps({"answer": "Fraud detection.", "confidence": "high"})])
    writer = materials.Writer(session.answers.load(), config.load_profile(), llm.transport())

    def open_ashby(page, job, timeout_ms=0):
        page.goto(f"{server}/ashby.html")
        return page.main_frame

    monkeypatch.setattr(session, "open_form", open_ashby)
    monkeypatch.setattr(session, "review_letter", lambda *a: pytest.fail("no letter to review"))
    replies = iter(["", "y"])
    monkeypatch.setattr(session, "ask", lambda prompt: next(replies))
    ctx = browser.new_context()
    with jobs_db:
        jobs_db.execute("UPDATE jobs SET source = 'ashby' WHERE id = 1")
    apps, steps = store.connect(), session.Steps()
    job = store.job(jobs_db, 1)
    assert session.process(job, ctx, None, writer, session.answers.load(), apps, steps) == "applied"
    ctx.close()
    # Drafts only: the free-text questions the answer bank doesn't cover (no letter calls).
    drafted = json.loads((materials.job_dir(job) / "drafts.json").read_text())
    assert "Why do you want to join Placeholder Analytics?" in drafted
    assert llm.chat_calls == len(drafted)
    d = materials.job_dir(job)
    assert not (d / "cover_letter.md").exists() and not (d / "cover_letter.pdf").exists()
    assert json.loads((d / "drafts.json").read_text())
    assert not any("cover letter" in text for text, _ in steps.done)
    row = apps.execute("SELECT cover_letter FROM applications WHERE job_id = 1").fetchone()
    assert row["cover_letter"] == ""

    # On demand it's still written: `apply prepare <id> --letter`.
    letter = json.dumps({"letter": " ".join(["word"] * 115) + "\n\n" + " ".join(["word"] * 115)})
    llm2 = FakeLLM([letter, json.dumps({"unsupported_claims": []})])
    writer2 = materials.Writer(session.answers.load(), config.load_profile(), llm2.transport())
    m = materials.prepare(job, session.answers.load(), writer2, (), None, letter=True)
    assert m.letter and (d / "cover_letter.md").exists() and llm2.chat_calls == 2


def test_sensitive_questions_never_reach_the_llm(
    jobs_db, quiet, browser, server, files, monkeypatch, capsys
):
    """No CTC, notice period or phone in answers.yaml: those questions are left red with the
    key to add, no LLM call is made for them, and the keys are listed once at the end."""
    from jobhunt import materials

    from .conftest import FakeLLM

    (config.ANSWERS_PATH).write_text(
        "identity: {full_name: Alex Example, email: alex@example.com, city: Bengaluru,"
        " country: India}\n"
    )
    a = session.answers.load()
    llm = FakeLLM([json.dumps({"answer": "Fraud detection.", "confidence": "high"})])
    writer = materials.Writer(a, config.load_profile(), llm.transport())

    def open_ashby(page, job, timeout_ms=0):
        page.goto(f"{server}/ashby.html")
        return page.main_frame

    monkeypatch.setattr(session, "open_form", open_ashby)
    monkeypatch.setattr(session, "ask", lambda prompt: "s")
    monkeypatch.setattr(session.console, "width", 200)  # keep checklist notes on one line
    with jobs_db:
        jobs_db.execute("UPDATE jobs SET source = 'ashby' WHERE id = 1")
    job, missing = store.job(jobs_db, 1), {}
    ctx = browser.new_context()
    assert session.process(job, ctx, None, writer, a, store.connect(), None, missing) == "skipped"
    ctx.close()

    prompts = [json.loads(r.content)["messages"][0]["content"] for r in llm.requests
               if r.url.path == "/api/chat"]  # fmt: skip
    assert len(prompts) == llm.chat_calls == 1  # only "Why do you want to join …?"
    assert "Why do you want to join Placeholder Analytics?" in prompts[0]
    for q in ("Current CTC", "Expected CTC", "Notice Period", "Phone Number"):
        assert all(f"QUESTION: {q}" not in p for p in prompts)
    assert sorted(missing) == [
        "identity.phone", "standard.eeo.gender", "work.authorized_india", "work.current_ctc_lpa",
        "work.expected_ctc_lpa", "work.notice_period",
    ]  # fmt: skip
    out = capsys.readouterr().out
    assert "add work.current_ctc_lpa to answers.yaml" in out
    # Sponsorship isn't missing: for a job in India the answer is always "No".

    session.print_missing(missing)
    listed = capsys.readouterr().out
    assert "Add these to answers.yaml" in listed and listed.count("work.notice_period") == 1


def test_to_draft_skips_sensitive_questions():
    from jobhunt.answers import Answers

    a = Answers()
    job = {"location": "San Francisco, CA"}
    fields = [form.Field(i, label, "text", [], False) for i, label in enumerate([
        "Current CTC", "What are your salary expectations?", "Mobile number",
        "Do you require visa sponsorship?", "What is your notice period?", "Gender identity",
        "Which Python packages have you published?", "Why us?",
    ])]  # fmt: skip
    assert form.to_draft(fields, a, job) == [
        ("Which Python packages have you published?", "text"),
        ("Why us?", "text"),
    ]
    keys = form.missing_keys(fields, a, job)
    assert keys["work.expected_usd"] == "what are your salary expectations"  # job abroad
    assert "standard.eeo.gender" in keys and "work.notice_period" in keys
