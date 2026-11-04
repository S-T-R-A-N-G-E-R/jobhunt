"""Fill the synthetic Greenhouse / Lever / Ashby forms in headless Chromium, served from a local
file server. Every test also checks that nothing was ever submitted."""

import functools
import http.server
import threading
from pathlib import Path

import pytest

from jobhunt.answers import Answers
from jobhunt.apply import ADAPTERS, form
from jobhunt.materials import Materials

playwright = pytest.importorskip("playwright.sync_api")

FORMS = Path(__file__).parent / "fixtures" / "forms"
A = Answers.model_validate(
    {"identity": {"full_name": "Alex Q Example", "email": "alex@example.com",
                  "phone": "+91 90000 00000", "city": "Bengaluru", "country": "India",
                  "linkedin": "https://linkedin.com/in/example"},
     "work": {"current_company": "Example Corp", "current_title": "ML Engineer",
              "years_total": 1, "notice_period": "30 days", "current_ctc_lpa": 12,
              "expected_ctc_lpa": 20, "authorized_india": True,
              "needs_sponsorship_elsewhere": True},
     "standard": {"eeo": {"gender": "Male", "hispanic_latino": "No", "race_ethnicity": "Asian"}}}
)  # fmt: skip
JOB = {"id": 1, "company": "Fictional Labs", "title": "ML Engineer", "location": "Bengaluru, India"}
RESUME, LETTER = "Alex_Q_Example_Resume.pdf", "Alex_Q_Example_Cover_Letter_Fictional_Labs.pdf"
RED, ORANGE = "rgb(229, 72, 77)", "rgb(245, 165, 36)"


@pytest.fixture(scope="module")
def server():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=FORMS)
    handler.log_message = lambda *a: None
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as pw:
        try:
            b = pw.chromium.launch()
        except Exception as e:  # no browser installed: `uv run playwright install chromium`
            pytest.skip(f"Chromium not available: {e}")
        yield b
        b.close()


@pytest.fixture
def files(home):
    (home / "data" / "resume.pdf").write_bytes(b"%PDF-1.4 resume")
    letter_pdf = home / "cover_letter.pdf"
    letter_pdf.write_bytes(b"%PDF-1.4 letter")
    return letter_pdf


def open_form(browser, server, name):
    page = browser.new_page()
    posts = []
    page.on("request", lambda r: r.method != "GET" and posts.append(r.url))
    page.goto(f"{server}/{name}.html")
    return page, posts


def assert_never_submitted(page, posts):
    assert page.evaluate("window.__submits") == 0
    assert page.evaluate("window.__submitClicks") == 0
    assert posts == []


def outline(page, selector):
    return page.eval_on_selector(
        selector, "e => getComputedStyle(e.closest('[data-jobhunt-idx]')).outlineColor"
    )


def rows(report):
    return {r.label: r for r in report.rows}


def test_greenhouse(browser, server, files):
    page, posts = open_form(browser, server, "greenhouse")
    m = Materials(dir=files.parent, letter="Dear team", letter_pdf=files, drafts={
        "Why Fictional Labs?*": {"answer": "Your document AI work.", "confidence": "high",
                                 "model": "ollama/test"},
        "What is your favourite open-source project and why?": {
            "answer": "Maybe vLLM.", "confidence": "low", "model": "ollama/test"},
    })  # fmt: skip
    report = form.fill(page.main_frame, page, ADAPTERS["greenhouse"], JOB, A, m)

    assert page.input_value("#first_name") == "Alex"
    assert page.input_value("#last_name") == "Q Example"
    assert page.input_value("#phone") == "+91 90000 00000"
    assert page.input_value("#iti-search") == ""  # the country search box is left alone
    assert page.eval_on_selector("#resume", "e => e.files[0].name") == RESUME
    assert page.eval_on_selector("#cover_letter", "e => e.files[0].name") == LETTER
    assert page.eval_on_selector("#resume", "e => e.files[0].size") == len(b"%PDF-1.4 resume")
    single = "(s) => document.querySelector(s).closest('.select').querySelector('.select__single-value').textContent"  # noqa: E501
    assert page.evaluate(single, "#question_3") == "No"  # sponsorship, job in India
    assert page.evaluate(single, "#gender") == "Male"
    assert page.evaluate(single, "#race") == "Asian"  # revealed after "Hispanic: No"
    assert page.input_value("#question_4") == "Your document AI work."

    r = rows(report)
    assert r["website"].status == "empty" and outline(page, "#question_2") == RED
    assert r["privacy notice acknowledgement"].note.startswith("consent")
    assert not page.is_checked("#privacy") and outline(page, "#privacy") == RED
    low = r["what is your favourite open-source project and why"]
    assert low.status == "flagged" and outline(page, "#question_5") == ORANGE
    assert r["first name"].status == "filled"
    assert outline(page, "#first_name") != RED
    assert report.blocked_submits == 0
    assert_never_submitted(page, posts)


def test_lever(browser, server, files):
    page, posts = open_form(browser, server, "lever")
    m = Materials(dir=files.parent, letter="Dear Hiring Team,\n\nHello.", letter_pdf=files)
    report = form.fill(page.main_frame, page, ADAPTERS["lever"], JOB, A, m)

    assert page.input_value("input[name=name]") == "Alex Q Example"
    assert page.input_value("input[name=org]") == "Example Corp"
    assert page.input_value("input[name=location]") == "Bengaluru, India"
    assert page.input_value("input[name='urls[LinkedIn]']") == "https://linkedin.com/in/example"
    assert page.input_value("input[name='urls[GitHub]']") == ""  # unknown: never invented
    assert page.eval_on_selector("input[name=resume]", "e => e.files[0].name") == RESUME
    assert page.is_checked("input[value=Male]")  # the paragraph label's last sentence
    assert page.is_checked("input[name='cards[c1][field2]'][value=No]")
    # No cover letter upload on Lever: the letter goes in "Additional information".
    assert page.input_value("textarea[name=comments]").startswith("Dear Hiring Team")
    r = rows(report)
    assert r["total years of experience"].status == "empty"  # "1" vs ranges: not guessed
    assert "no option clearly matches" in r["total years of experience"].note
    assert_never_submitted(page, posts)


def test_ashby(browser, server, files):
    page, posts = open_form(browser, server, "ashby")
    why = {"answer": "Fraud detection.", "confidence": "medium", "model": "ollama/test"}
    drafts = {"Why do you want to join Placeholder Analytics?": why}
    m = Materials(dir=files.parent, letter="x", letter_pdf=files, drafts=drafts)
    report = form.fill(page.main_frame, page, ADAPTERS["ashby"], JOB, A, m)

    assert page.input_value("#_systemfield_name") == "Alex Q Example"
    assert page.input_value("#f-ctc") == "12 LPA"
    assert page.input_value("#f-ectc") == "20 LPA"
    assert page.input_value("#f-notice") == "30 days"
    assert page.eval_on_selector("#_systemfield_resume", "e => e.files[0].name") == RESUME
    assert page.eval_on_selector("#autofill-pane", "e => e.files.length") == 0
    pressed = (
        "(p) => document.querySelector(`[data-field-path=${p}] [aria-pressed=true]`)?.textContent"  # noqa: E501
    )
    assert page.evaluate(pressed, "f-auth") == "Yes"
    assert page.evaluate(pressed, "f-sponsor") == "No"
    assert page.input_value("#loc") == "Bengaluru, Karnataka, India"
    assert page.is_checked("#g0")
    assert not page.is_checked("#f-ack-0")  # consent stays yours
    assert page.input_value("#f-why") == "Fraud detection."
    assert rows(report)["where are you currently located"].status == "filled"
    assert_never_submitted(page, posts)


def test_guard_blocks_an_accidental_submit_while_filling(browser, server):
    page, posts = open_form(browser, server, "greenhouse")
    page.evaluate(form.GUARD_ON_JS)
    page.click("#submit")
    page.locator("#first_name").press("Enter")  # implicit submission
    assert page.evaluate("window.__submits") == 0 and posts == []
    assert page.evaluate(form.GUARD_OFF_JS) >= 1
    page.check("#privacy")  # the form's own validation needs it
    with page.expect_request(lambda r: r.method == "POST"):
        page.click("#submit")  # guard off: your own click submits normally


def test_unrecognised_page_has_no_fields(browser, server):
    page, _ = open_form(browser, server, "detector.js")  # not a form at all
    assert form.scan(page.main_frame, ADAPTERS["greenhouse"]) == []
