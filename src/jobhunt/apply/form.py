"""Scan and fill an application form; never submit it.

An ATS adapter only says where each question lives (its container and label selectors). The
scanner finds every question container, works out its kind (text, textarea, select, combobox,
radio, checkbox, yesno, file) and options, and tags it with data-jobhunt-idx. Filling uses
fill / check / select_option / set_input_files, plus clicks on combobox options and Ashby's
Yes/No buttons. Nothing ever presses Enter in a field or touches a submit button, and while
jobhunt is filling, a capture-phase guard cancels any submit that happens anyway.
"""

import logging
import re
from dataclasses import dataclass, field

from ..answers import Answer, Answers, as_text, bank_has, clean, lookup, pick_option, sensitive_key
from ..materials import Materials

log = logging.getLogger(__name__)

SCAN_JS = r"""(cfg) => {
  const T = el => (el ? (el.innerText || el.textContent || '') : '').replace(/\s+/g, ' ').trim();
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  const labelOf = e => T(e.closest('label') ||
    (e.id && document.querySelector(`label[for="${CSS.escape(e.id)}"]`)) || e.parentElement)
    || e.value || '';
  window.__jhNext = window.__jhNext || 0;
  const out = [];
  for (const box of document.querySelectorAll(cfg.container)) {
    if (box.querySelector(cfg.container) || !visible(box)) continue;  // innermost, visible
    const all = s => [...box.querySelectorAll(s)];
    const yesno = cfg.yesno ? box.querySelector(cfg.yesno) : null;
    const files = all('input[type=file]');
    const radios = all('input[type=radio]');
    const checks = all('input[type=checkbox]').filter(e => !(yesno && yesno.contains(e)));
    const combo = box.querySelector('input[role=combobox]:not([type=search])');
    const sel = box.querySelector('select');
    const ta = box.querySelector('textarea:not([name^="g-recaptcha"])');
    const inp = box.querySelector(cfg.input);
    let kind, options = [], ctrl;
    if (files.length) { kind = 'file'; ctrl = files[0]; }
    else if (yesno) {
      kind = 'yesno'; ctrl = yesno; options = [...yesno.querySelectorAll('button')].map(T);
    }
    else if (radios.length) { kind = 'radio'; ctrl = radios[0]; options = radios.map(labelOf); }
    else if (checks.length) { kind = 'checkbox'; ctrl = checks[0]; options = checks.map(labelOf); }
    else if (sel) {
      kind = 'select'; ctrl = sel;
      options = [...sel.options].filter(o => o.value !== '').map(T);
    }
    else if (combo) { kind = 'combobox'; ctrl = combo; }
    else if (ta) { kind = 'textarea'; ctrl = ta; }
    else if (inp) { kind = 'text'; ctrl = inp; }
    else continue;
    const labelEl = (cfg.label && box.querySelector(cfg.label)) ||
      box.querySelector('label, legend');
    const label = T(labelEl) || ctrl.getAttribute('aria-label') ||
      ctrl.getAttribute('placeholder') || '';
    const required = /[*✱]/.test(label) || all('input, textarea, select').some(
      e => e.required || e.getAttribute('aria-required') === 'true');
    let value = '';
    if (kind === 'text' || kind === 'textarea' || kind === 'select') value = ctrl.value || '';
    if (kind === 'combobox')
      value = T(box.querySelector('[class*=single-value], [class*=singleValue]'));
    if (!box.dataset.jobhuntIdx) box.dataset.jobhuntIdx = String(window.__jhNext++);
    out.push({idx: +box.dataset.jobhuntIdx, label, kind, options, required,
              input_type: ctrl.type || '', value});
  }
  return out;
}"""

# While jobhunt fills, cancel every submit and every click on a submit-like button. Runs in the
# capture phase on window, so it fires before the page's own (React) handlers.
GUARD_ON_JS = r"""() => {
  if (!window.__jhGuard) {
    window.__jhGuard = {on: true, blocked: 0};
    const stop = e => { if (!window.__jhGuard.on) return;
      e.preventDefault(); e.stopImmediatePropagation(); window.__jhGuard.blocked++; };
    window.addEventListener('submit', stop, true);
    window.addEventListener('click', e => {
      const b = e.target.closest && e.target.closest('button, input[type=submit]');
      if (b && (b.type === 'submit' || b.tagName === 'INPUT') &&
          /submit|apply|send/i.test(b.innerText || b.value || '')) stop(e);
    }, true);
  }
  window.__jhGuard.on = true;
}"""
GUARD_OFF_JS = "() => { if (window.__jhGuard) window.__jhGuard.on = false;"
GUARD_OFF_JS += " return window.__jhGuard ? window.__jhGuard.blocked : 0 }"

# A plain text-like input: the same selector for scanning and filling.
TEXT_INPUT = (
    "input:not([type=hidden]):not([type=file]):not([type=radio]):not([type=checkbox])"
    ":not([type=search]):not([role=combobox]):not([type=submit]):not([type=button])"
)
RED, ORANGE = "#e5484d", "#f5a524"
HIGHLIGHT_JS = """(el, color) => { el.style.outline = `3px solid ${color}`;
  el.style.outlineOffset = '4px'; el.style.borderRadius = '4px'; }"""

URLISH = re.compile(r"\burl\b|link|website|profile|portfolio|github|scholar|twitter|\bx\b", re.I)
COVER = re.compile(r"cover letter|covering letter|motivation letter", re.I)
OTHER_INFO = re.compile(r"additional information|anything else|message to the (hiring )?team", re.I)
RESUME = re.compile(r"resume|résumé|\bcv\b|curriculum", re.I)
LOCATION = re.compile(r"location|city|where are you", re.I)
CAPTCHA = re.compile(r"recaptcha|hcaptcha|challenges\.cloudflare|turnstile|arkoselabs|funcaptcha")
THANKS = re.compile(
    r"thank(s| you) for (applying|your application|submitting)|application (was |has been )?"
    r"(successfully )?(submitted|received)|we('ve| have) received your application",
    re.I,
)


@dataclass
class Field:
    idx: int
    label: str
    kind: str
    options: list[str]
    required: bool
    input_type: str = ""
    value: str = ""


@dataclass
class Row:
    """One line of the checklist."""

    label: str
    kind: str
    required: bool
    status: str  # filled | flagged | empty | kept
    value: str = ""
    source: str = ""
    note: str = ""
    missing_key: str = ""  # answers.yaml key that would have answered it


@dataclass
class Report:
    rows: list[Row] = field(default_factory=list)
    blocked_submits: int = 0
    captcha: bool = False

    @property
    def missing(self) -> list[str]:
        return sorted({r.missing_key for r in self.rows if r.missing_key})


def scan(frame, adapter) -> list[Field]:
    cfg = {"container": adapter.CONTAINER, "label": adapter.LABEL, "yesno": adapter.YESNO,
           "input": TEXT_INPUT}  # fmt: skip
    return [Field(**f) for f in frame.evaluate(SCAN_JS, cfg)]


def box(frame, f: Field):
    return frame.locator(f'[data-jobhunt-idx="{f.idx}"]')


def has_captcha(page) -> bool:
    return any(CAPTCHA.search(fr.url or "") for fr in page.frames)


def is_free_text(f: Field) -> bool:
    return f.kind in ("text", "textarea") and f.input_type not in ("email", "tel", "number")


def to_draft(fields: list[Field], a: Answers, job) -> list[tuple[str, str]]:
    """Free-text questions the answer bank doesn't cover: these get an LLM draft."""
    out = []
    for f in fields:
        if not is_free_text(f) or URLISH.search(f.label) or COVER.search(f.label):
            continue
        if OTHER_INFO.search(f.label) and f.kind == "textarea":
            continue  # gets the cover letter
        if sensitive_key(f.label, job["location"]):
            continue  # pay, contact, authorization, EEO, notice: answers.yaml only, never an LLM
        if lookup(f.label, f.kind, a, job["location"]) is None:
            out.append((f.label, f.kind))
    return out


def missing_keys(fields: list[Field], a: Answers, job) -> dict[str, str]:
    """Sensitive questions on the form that answers.yaml can't answer: {key: question}."""
    out = {}
    for f in fields:
        key = sensitive_key(f.label, job["location"])
        if key and not bank_has(a, key) and lookup(f.label, f.kind, a, job["location"]) is None:
            out.setdefault(key, clean(f.label))
    return out


def upload_name(a: Answers, what: str, company: str = "") -> str:
    """e.g. Alex_Example_Resume.pdf: what the recruiter sees instead of resume.pdf."""
    parts = [a.identity.full_name or "Applicant", what, company]
    return "_".join(re.sub(r"[^A-Za-z0-9]+", "_", p).strip("_") for p in parts if p) + ".pdf"


def uses_letter(fields: list[Field]) -> bool:
    """Whether the form takes a cover letter (upload, text box, or Lever-style extra info)."""
    cover_file = any(f.kind == "file" and COVER.search(f.label) for f in fields)
    return (
        cover_file
        or any(f.kind in ("text", "textarea") and COVER.search(f.label) for f in fields)
        or any(f.kind == "textarea" and OTHER_INFO.search(f.label) for f in fields)
    )


def resolve(f: Field, job, a: Answers, m: Materials | None, cover_file: bool) -> Answer | None:
    if f.kind == "file":
        if COVER.search(f.label):
            if m and m.letter_pdf:
                name = upload_name(a, "Cover_Letter", job["company"])
                return Answer(
                    str(m.letter_pdf), "cover letter", "low" if m.claims else "high", name
                )
            return None
        if RESUME.search(f.label):
            name = upload_name(a, "Resume")
            return Answer(str(a.resume_for(job["title"])), "files.resume", "high", name)
        return None
    if m and m.letter and f.kind in ("text", "textarea"):
        if COVER.search(f.label):
            return Answer(m.letter, "cover letter", "low" if m.claims else "high")
        if OTHER_INFO.search(f.label) and f.kind == "textarea" and not cover_file:
            return Answer(m.letter, "cover letter (no upload field)", "medium")
    if f.kind == "checkbox" and len(f.options) <= 1:
        return None  # a consent / acknowledgement: yours to tick
    hit = lookup(f.label, f.kind, a, job["location"])
    if hit:
        return hit
    if (
        is_free_text(f)
        and m
        and f.label in m.drafts
        and not sensitive_key(f.label, job["location"])
    ):
        d = m.drafts[f.label]
        if d["answer"]:
            return Answer(d["answer"], f"draft ({d['model']})", d["confidence"])
    return None


def fill(frame, page, adapter, job, a: Answers, m: Materials | None) -> Report:
    """Fill what's known, highlight the rest, and return the checklist. Never submits."""
    report = Report(captcha=has_captcha(page))
    frame.evaluate(GUARD_ON_JS)
    seen: set[int] = set()
    try:
        # A few passes: some forms reveal questions after an answer (race after "Hispanic: No").
        for _ in range(3):
            fields = [f for f in scan(frame, adapter) if f.idx not in seen]
            if not fields:
                break
            cover_file = any(
                f.kind == "file" and COVER.search(f.label) for f in scan(frame, adapter)
            )
            for f in fields:
                seen.add(f.idx)
                report.rows.append(fill_one(frame, adapter, f, job, a, m, cover_file))
    finally:
        report.blocked_submits = frame.evaluate(GUARD_OFF_JS)
    return report


def fill_one(frame, adapter, f: Field, job, a, m, cover_file) -> Row:
    row = Row(
        label=clean(f.label)[:70] or f"({f.kind})", kind=f.kind, required=f.required, status="empty"
    )
    ans = resolve(f, job, a, m, cover_file)
    try:
        if ans is None:
            if f.value:
                row.status, row.value, row.note = "kept", f.value[:40], "already filled"
            elif f.kind == "checkbox" and len(f.options) <= 1:
                row.note = "consent: tick it yourself if you agree"
            elif key := sensitive_key(f.label, job["location"]):
                if bank_has(a, key):
                    row.note = "answer it yourself (unclear for this job)"
                else:
                    row.note, row.missing_key = f"add {key} to answers.yaml", key
            else:
                row.note = "not in the answer bank"
        else:
            row.source = ans.source
            shown = put(frame, adapter, f, ans)
            if shown is None:
                row.note = f"no option clearly matches {as_text(ans.value)!r}"
            else:
                row.value = shown
                row.status = "flagged" if ans.confidence == "low" else "filled"
                if ans.confidence == "low":
                    row.note = "low confidence: check it"
    except Exception as e:  # one awkward field must not stop the rest
        log.warning("couldn't fill %r: %s", f.label, e)
        row.status, row.note = "empty", f"couldn't fill: {type(e).__name__}"
    if row.status in ("empty", "flagged"):
        try:
            box(frame, f).evaluate(HIGHLIGHT_JS, RED if row.status == "empty" else ORANGE)
        except Exception:
            pass
    return row


def put(frame, adapter, f: Field, ans: Answer) -> str | None:
    """Enter one answer. Returns what's shown in the checklist, or None if no option matched."""
    b = box(frame, f)
    value = ans.value
    if f.kind == "file":
        files = value
        if ans.filename:  # same bytes, a name that's yours instead of "resume.pdf"
            files = {"name": ans.filename, "mimeType": "application/pdf",
                     "buffer": open(value, "rb").read()}  # fmt: skip
        b.locator("input[type=file]").first.set_input_files(files)
        return ans.filename or value.rsplit("/", 1)[-1]
    if f.kind in ("text", "textarea"):
        text = as_text(value)
        if f.input_type == "number" and not re.fullmatch(r"[\d.]+", text):
            return None
        b.locator("textarea" if f.kind == "textarea" else TEXT_INPUT).first.fill(text)
        return text[:40] + ("…" if len(text) > 40 else "")
    if f.kind == "combobox":
        return combobox(frame, b, value, LOCATION.search(f.label) is not None)
    option = pick_option(value, f.options)
    if option is None:
        return None
    k = f.options.index(option)
    if f.kind == "select":
        b.locator("select").first.select_option(label=option)
    elif f.kind == "yesno":
        b.locator(f"{adapter.YESNO} button").nth(k).click()
    else:  # radio / checkbox group
        b.locator(f"input[type={f.kind}]").nth(k).check(force=True)
    return option


def _options(frame) -> list[str]:
    opts = frame.locator("[role=option]")
    return [t.strip() for t in opts.all_inner_texts()] if opts.count() else []


def combobox(frame, b, value, is_location: bool) -> str | None:
    """Open the dropdown, pick the matching option. Typed text is cleared if nothing matches.
    Never presses Enter (it could submit the form)."""
    inp = b.locator("input[role=combobox]:not([type=search])").first
    inp.click()
    frame.wait_for_timeout(400)
    choice = pick_option(value, _options(frame))
    if choice is None and isinstance(value, str):
        typed = value.split(",")[0] if is_location else value
        inp.fill(typed)
        frame.wait_for_timeout(1500)  # autocompletes load options as you type
        opts = _options(frame)
        choice = pick_option(value, opts) or pick_option(typed, opts)
        if choice is None and is_location:
            # ponytail: first suggestion starting with the city; shown in the checklist to check
            choice = next((o for o in opts if o.lower().startswith(typed.lower())), None)
        if choice is None:
            inp.fill("")
    if choice is None:
        inp.press("Escape")
        return None
    frame.get_by_role("option", name=choice, exact=True).first.click()
    return choice


def submitted(page, form_url: str) -> bool:
    """Best guess that the application went through: a thank-you page, or the form gone and
    thank-you text shown. Unsure = False (then you're asked)."""
    if page.is_closed():
        return False
    url = page.url
    if url.split("?")[0] != form_url.split("?")[0] and re.search(
        r"confirm|thank|success|submitted", url, re.I
    ):
        return True
    for fr in page.frames:
        try:
            text = fr.inner_text("body", timeout=2000)[:20000]
            if THANKS.search(text) and fr.locator("input[type=file]").count() == 0:
                return True
        except Exception:
            continue
    return False
