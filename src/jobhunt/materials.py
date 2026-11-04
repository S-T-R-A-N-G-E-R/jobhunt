"""Application materials for one job (Phase 3): a cover letter (.md + .pdf) that passed a claims
check, and drafted answers to the form's free-text questions.

Saved in output/applications/<company>-<title>-<job id>/ and reused on later runs:
  cover_letter.md / cover_letter.pdf, drafts.json, meta.json (which model wrote what, flags).
"""

import hashlib
import html
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, Field

from . import config
from .answers import Answers, llm_context
from .config import FallbackLLM, LLMConfig, LocalLLM, Profile
from .llm import LLM, LLMError, LLMResult, QuotaError

log = logging.getLogger(__name__)

MIN_WORDS, MAX_WORDS = 150, 350


class Letter(BaseModel):
    letter: str = Field(min_length=200)  # paragraphs separated by blank lines


class ClaimCheck(BaseModel):
    unsupported_claims: list[str]


class Draft(BaseModel):
    answer: str
    confidence: Literal["high", "medium", "low"]


class MaterialsError(Exception):
    pass


@dataclass
class Materials:
    dir: Path
    letter: str = ""  # the full letter as plain text, for pasting
    letter_pdf: Path | None = None
    claims: list[str] = field(default_factory=list)  # unsupported claims left after the retry
    drafts: dict[str, dict] = field(default_factory=dict)  # label -> answer, confidence, model
    letter_model: str = ""  # provider/model that wrote the letter

    @property
    def letter_md(self) -> Path:
        return self.dir / "cover_letter.md"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def slugify(s: str, n: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:n].strip("-")


def job_dir(job) -> Path:
    name = f"{slugify(job['company'], 30)}-{slugify(job['title'])}-{job['id']}"
    return config.OUTPUT_DIR / "applications" / name


class Writer:
    """Writing LLM: Ollama Cloud (answers.yaml settings.writing), then the local model; the
    local model escalates to the paid fallback only if profile.yaml enables it."""

    def __init__(self, a: Answers, profile: Profile, transport: httpx.BaseTransport | None = None):
        w = a.settings.writing
        cloud = LLMConfig(
            local=LocalLLM(model=w.model, think=w.think, timeout_seconds=w.timeout_seconds),
            fallback=FallbackLLM(enabled=False),
        )
        self.chain = [LLM(cloud, transport, host=w.host), LLM(profile.llm, transport)]

    def complete(self, template: str, schema: type[BaseModel], **values) -> LLMResult:
        prompt = (config.PROMPTS_DIR / template).read_text()
        for key, value in values.items():
            prompt = prompt.replace("{{" + key + "}}", str(value))
        errors = []
        for llm in self.chain:
            try:
                return llm.complete_json(prompt, schema, task=template.removesuffix(".md"))
            except (LLMError, QuotaError) as e:
                name = f"{llm.cfg.local.model} at {llm.ollama_host}"
                log.warning("writing model %s failed: %s", name, e)
                errors.append(f"{name}: {str(e)[:150]}")
        raise LLMError("; ".join(errors))

    def describe(self) -> str:
        """The models in the order they're tried, e.g. "gpt-oss:20b → qwen3.5:4b"."""
        return " → ".join(llm.cfg.local.model for llm in self.chain)


def _job_text(job) -> str:
    return (job["description_text"] or "")[:6000]


def write_letter(job, a: Answers, resume: str, writer: Writer) -> tuple[str, list[str], dict]:
    """(letter body, claims still unsupported, meta). Regenerates once if the check finds
    unsupported claims or the length is off."""
    candidate = llm_context(a, resume)
    avoid, meta = "", {}
    for attempt in (1, 2):
        r = writer.complete(
            "cover_letter.md", Letter, candidate=candidate, job=_job_text(job), avoid=avoid,
            title=job["title"], company=job["company"], location=job["location"] or "",
        )  # fmt: skip
        paragraphs = re.split(r"\n\s*\n", r.obj.letter.strip())
        body = "\n\n".join(re.sub(r"\s*\n\s*", " ", p).strip() for p in paragraphs if p.strip())
        check = writer.complete("check_claims.md", ClaimCheck, candidate=candidate, letter=body)
        claims = [c for c in check.obj.unsupported_claims if c.strip()]
        words = len(body.split())
        meta = {
            "letter_model": f"{r.provider}/{r.model}",
            "check_model": f"{check.provider}/{check.model}",
            "attempts": attempt,
            "words": words,
        }
        if not claims and MIN_WORDS <= words <= MAX_WORDS:
            return body, [], meta
        notes = []
        if claims:
            notes.append(
                "- Your previous draft made claims CANDIDATE doesn't support. Leave them out: "
                + "; ".join(claims)
            )
        if not MIN_WORDS <= words <= MAX_WORDS:
            notes.append(f"- Your previous draft had {words} words. Stay between 220 and 260.")
        avoid = "\n".join(notes)
    log.warning("cover letter for job %s still flagged after a retry: %s", job["id"], claims)
    return body, claims, meta


def full_letter(body: str, job, a: Answers) -> str:
    return f"Dear Hiring Team at {job['company']},\n\n{body}\n\nRegards,\n{a.identity.full_name}\n"


PDF_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 22mm 22mm 20mm; }}
body {{ font-family: Georgia, 'Times New Roman', serif; font-size: 11pt; line-height: 1.5;
       color: #222; }}
h1 {{ font-size: 17pt; font-weight: normal; letter-spacing: .5px; margin: 0 0 2px; }}
.contact {{ font-size: 9.5pt; color: #555; margin-bottom: 26px; }}
.date {{ margin-bottom: 18px; }}
p {{ margin: 0 0 11px; text-align: left; }}
</style></head><body>
<h1>{name}</h1><div class="contact">{contact}</div>
<div class="date">{date}</div>
{paragraphs}
</body></html>"""


def letter_html(letter: str, a: Answers) -> str:
    i = a.identity
    contact = " · ".join(
        html.escape(x) for x in [f"{i.city}, {i.country}" if i.city else "", i.phone, i.email,
                                 i.linkedin.removeprefix("https://").removeprefix("www.")] if x
    )  # fmt: skip
    paragraphs = "".join(
        "<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in letter.strip().split("\n\n")
    )
    return PDF_TEMPLATE.format(
        name=html.escape(i.full_name), contact=contact,
        date=datetime.now(config.local_tz()).strftime("%-d %B %Y"), paragraphs=paragraphs,
    )  # fmt: skip


def render_pdf(pw, page_html: str, path: Path) -> None:
    """Chromium prints PDFs only when headless, so this uses its own short-lived browser."""
    browser = pw.chromium.launch()
    try:
        page = browser.new_page()
        page.set_content(page_html)
        page.pdf(path=str(path), format="A4", print_background=True)
    finally:
        browser.close()


def draft_answer(question: str, kind: str, job, a: Answers, resume: str, writer: Writer) -> dict:
    length = (
        "2 to 5 sentences, under 120 words."
        if kind == "textarea"
        else "One short line, under 25 words."
    )
    r = writer.complete(
        "answer_question.md", Draft, question=question, length=length,
        candidate=llm_context(a, resume), title=job["title"], company=job["company"],
        job=_job_text(job)[:4000],
    )  # fmt: skip
    confidence = r.obj.confidence if r.obj.answer.strip() else "low"
    model = f"{r.provider}/{r.model}"
    return {"answer": r.obj.answer.strip(), "confidence": confidence, "model": model}


def prepare(
    job,
    a: Answers,
    writer: Writer,
    questions: list[tuple[str, str]] = (),
    pw=None,
    letter: bool = True,
) -> Materials:
    """Create whatever is missing. `questions`: (label, kind) of free-text fields to draft.
    `pw`: a Playwright instance for the PDF (skipped without one). `letter=False`: don't
    write a cover letter (one already written is still loaded). Raises MaterialsError if no
    model could write the letter; drafts that fail are just left out."""  # fmt: skip
    d = job_dir(job)
    d.mkdir(parents=True, exist_ok=True)
    meta_path, drafts_path = d / "meta.json", d / "drafts.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    m = Materials(dir=d)
    resume = config.load_resume()

    pdf = d / "cover_letter.pdf"
    if m.letter_md.exists():
        m.letter, m.claims = m.letter_md.read_text(), meta.get("unsupported_claims", [])
        if meta.get("letter_sha") and _sha(m.letter) != meta["letter_sha"]:
            # You edited the letter: that's your review, and the PDF must follow your text.
            m.claims = []
            pdf.unlink(missing_ok=True)
            meta.update(letter_sha=_sha(m.letter), unsupported_claims=[], edited=True)
            meta_path.write_text(json.dumps(meta, indent=2))
    elif letter:
        try:
            body, claims, info = write_letter(job, a, resume, writer)
        except LLMError as e:
            raise MaterialsError(f"no model could write the cover letter: {e}") from None
        m.letter, m.claims = full_letter(body, job, a), claims
        m.letter_md.write_text(m.letter)
        written = datetime.now().isoformat(timespec="seconds")
        meta.update(info, unsupported_claims=claims, letter_sha=_sha(m.letter), job_id=job["id"],
                    company=job["company"],
                    title=job["title"], written_at=written)  # fmt: skip
        meta_path.write_text(json.dumps(meta, indent=2))

    m.letter_model = meta.get("letter_model", "")
    if m.letter and not pdf.exists() and pw is not None:
        render_pdf(pw, letter_html(m.letter, a), pdf)
    m.letter_pdf = pdf if pdf.exists() else None

    m.drafts = json.loads(drafts_path.read_text()) if drafts_path.exists() else {}
    for label, kind in questions:
        if label in m.drafts:
            continue
        try:
            m.drafts[label] = draft_answer(label, kind, job, a, resume, writer)
        except LLMError as e:
            log.warning("couldn't draft an answer to %r: %s", label, e)
            continue
        drafts_path.write_text(json.dumps(m.drafts, indent=2, ensure_ascii=False))
    return m
