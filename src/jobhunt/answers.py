# ruff: noqa: E501  (the rules table reads best one rule per line)
"""The answer bank (config/answers.yaml) and matching form questions to it.

Every value typed into a form comes from here, the resume, or an LLM draft grounded in them.
Anything not clearly known is left empty for you. Choice fields (dropdowns, radios, yes/no)
only get an option that clearly matches the answer.
"""

import difflib
import re
import sys
from dataclasses import dataclass
from typing import Literal

import yaml
from pydantic import BaseModel

from . import config


class Identity(BaseModel):
    full_name: str = ""
    preferred_name: str = ""
    email: str = ""
    phone: str = ""
    city: str = ""
    country: str = ""
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class Work(BaseModel):
    current_company: str = ""
    current_title: str = ""
    years_total: float | None = None
    years_relevant: float | None = None
    notice_period: str = ""
    current_ctc_lpa: float | None = None
    expected_ctc_lpa: float | None = None
    expected_usd: int | None = None
    earliest_start: str = ""
    authorized_india: bool | None = None
    needs_sponsorship_elsewhere: bool | None = None


class Preferences(BaseModel):
    relocation: str = ""
    work_mode: str = ""
    travel: str = ""


class Education(BaseModel):
    degree: str = ""
    field: str = ""
    institution: str = ""
    location: str = ""
    graduation_year: int | None = None
    gpa: str = ""


class EEO(BaseModel):
    gender: str = ""
    pronouns: str = ""
    hispanic_latino: str = ""
    race_ethnicity: str = ""
    veteran: str = ""
    disability: str = ""


class Standard(BaseModel):
    how_heard: str = "Company careers page"
    eeo: EEO = EEO()


class Files(BaseModel):
    resume: str = "data/resume.pdf"
    variants: dict[str, str] = {}  # title keyword -> PDF


class WritingLLM(BaseModel):
    host: str = "https://ollama.com"
    model: str = "gpt-oss:20b"
    think: bool | str = "low"
    timeout_seconds: float = 180


class Settings(BaseModel):
    max_per_session: int = 5
    writing: WritingLLM = WritingLLM()


class Custom(BaseModel):
    q: str
    a: str


class Answers(BaseModel):
    settings: Settings = Settings()
    identity: Identity = Identity()
    work: Work = Work()
    preferences: Preferences = Preferences()
    education: list[Education] = []
    standard: Standard = Standard()
    files: Files = Files()
    facts: list[str] = []
    custom: list[Custom] = []

    def resume_for(self, title: str):
        """The resume PDF for a job title: the first variant whose keyword is in the title."""
        for keyword, path in self.files.variants.items():
            if keyword.lower() in title.lower():
                return config.ROOT / path
        return config.ROOT / self.files.resume


def load() -> Answers:
    if not config.ANSWERS_PATH.exists():
        sys.exit(
            f"Missing {config.ANSWERS_PATH}.\n"
            "Copy the example and fill it in:\n"
            f'  cp "{config.CONFIG_DIR}/answers.example.yaml" "{config.ANSWERS_PATH}"'
        )
    return Answers.model_validate(yaml.safe_load(config.ANSWERS_PATH.read_text()) or {})


def llm_context(a: Answers, resume: str) -> str:
    """What the writing LLM may know about you. Contact details, pay, work authorization and
    EEO answers are deliberately left out: they're only ever typed into forms."""
    w = a.work
    lines = [
        f"Name: {a.identity.full_name}",
        f"Current role: {w.current_title} at {w.current_company}",
        f"Years of experience: {w.years_total:g}" if w.years_total is not None else "",
        f"Based in: {a.identity.city}, {a.identity.country}",
        f"Relocation: {a.preferences.relocation}" if a.preferences.relocation else "",
        f"Work mode: {a.preferences.work_mode}" if a.preferences.work_mode else "",
    ]
    for e in a.education:
        lines.append(f"Education: {e.degree} {e.field}, {e.institution} ({e.graduation_year})")
    if a.facts:
        lines.append("Facts the candidate is happy to state:")
        lines += [f"- {f}" for f in a.facts]
    return "\n".join(x for x in lines if x) + f"\n\nResume:\n{resume.strip()}"


# ---- matching -------------------------------------------------------------------------------

Confidence = Literal["high", "medium", "low"]


@dataclass
class Answer:
    value: str | bool  # bool for yes/no questions
    source: str  # answers.yaml key ("work.notice_period", "custom"), "draft (model)", ...
    confidence: Confidence = "high"
    filename: str = ""  # for uploads: the name the recruiter sees


INDIA = re.compile(
    r"\bindia\b|bengaluru|bangalore|mumbai|pune|hyderabad|chennai|delhi|gurgaon|gurugram|noida"
    r"|kolkata|ahmedabad|kochi|jaipur|chandigarh|coimbatore|thiruvananthapuram|indore",
    re.I,
)
ANYWHERE = re.compile(r"remote|anywhere|global|worldwide|distributed", re.I)


def job_country(location: str | None) -> Literal["india", "abroad"] | None:
    """Where the job is, as far as it matters for authorization and pay: None = unclear."""
    loc = location or ""
    if INDIA.search(loc):
        return "india"
    if loc.strip() and not ANYWHERE.search(loc):
        return "abroad"
    return None


def clean(label: str) -> str:
    """Lower case, no asterisks or (parentheticals), and for long labels only the last
    sentence, which is where the actual question is (Lever puts a paragraph before it)."""
    s = re.sub(r"[*✱]", " ", label)
    if len(s) > 120:
        parts = [p for p in re.split(r"(?<=[.!?:])\s+", s.strip()) if len(p.split()) >= 2]
        s = parts[-1] if parts else s
    s = re.sub(r"\([^)]*\)", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip(" :?.")


def _lpa(x: float) -> str:
    return f"{x:g} LPA"


def standard(label: str, kind: str, a: Answers, location: str | None) -> Answer | None:
    """An answer from answers.yaml for a common question, or None."""
    q = clean(label)
    i, w, p, eeo = a.identity, a.work, a.preferences, a.standard.eeo
    edu = a.education[0] if a.education else Education()
    country = job_country(location)
    choice = kind in ("radio", "select", "combobox", "yesno", "checkbox")

    def ans(value, key, confidence: Confidence = "high"):
        if value in ("", None):
            return None
        return Answer(value if isinstance(value, bool) else str(value), key, confidence)

    def yaml_(key, value, confidence: Confidence = "high"):
        return ans(value, key, confidence)

    rules = [
        # identity
        (r"\bpreferred (first )?name\b", lambda: yaml_("identity.preferred_name", i.preferred_name)),
        (r"^(legal )?first name$|given name", lambda: yaml_("identity.full_name", i.full_name.split()[0] if i.full_name else "")),
        (r"^(legal )?last name$|surname|family name", lambda: yaml_("identity.full_name", " ".join(i.full_name.split()[1:]))),
        (r"^(current |present |most recent )?(company|employer|organi[sz]ation)( name)?$", lambda: yaml_("work.current_company", w.current_company)),
        (r"^(your )?(full |legal )?name$", lambda: yaml_("identity.full_name", i.full_name)),
        (r"e-?mail", lambda: yaml_("identity.email", i.email)),
        (r"phone|mobile|contact number", lambda: yaml_("identity.phone", i.phone)),
        (r"linkedin", lambda: yaml_("identity.linkedin", i.linkedin)),
        (r"github", lambda: yaml_("identity.github", i.github)),
        (r"portfolio|personal (web)?site|^website$", lambda: yaml_("identity.portfolio", i.portfolio)),
        # EEO (before "race"/"gender" words can match anything else)
        (r"hispanic|latin[oax]", lambda: yaml_("standard.eeo.hispanic_latino", eeo.hispanic_latino)),
        (r"\brace\b|ethnicit", lambda: yaml_("standard.eeo.race_ethnicity", eeo.race_ethnicity)),
        (r"pronoun", lambda: yaml_("standard.eeo.pronouns", eeo.pronouns)),
        (r"\bgender\b|\bsex\b", lambda: yaml_("standard.eeo.gender", eeo.gender)),
        (r"veteran", lambda: yaml_("standard.eeo.veteran", eeo.veteran)),
        (r"disabilit", lambda: yaml_("standard.eeo.disability", eeo.disability)),
        # work
        (r"current (job )?(title|role|designation|position)|^designation$", lambda: yaml_("work.current_title", w.current_title)),
        (r"how did you (hear|find|learn|come across)|where did you (hear|find|see)|referral source", lambda: yaml_("standard.how_heard", a.standard.how_heard)),
        (r"notice period", lambda: yaml_("work.notice_period", w.notice_period)),
        (r"(current|present|existing).*(ctc|salary|compensation|package)", lambda: yaml_("work.current_ctc_lpa", _lpa(w.current_ctc_lpa) if w.current_ctc_lpa else None)),
        (r"expected.*(ctc|salary|compensation|pay)|(salary|compensation|pay) expectation|desired (salary|compensation|pay)", lambda: _expected(w, country)),
        (r"start date|earliest.*(start|join)|when can you (start|join)|available to (start|join)|date of joining", lambda: yaml_("work.earliest_start", w.earliest_start)),
        (r"relevant.*experience|experience.*relevant", lambda: yaml_("work.years_relevant", f"{w.years_relevant:g}" if w.years_relevant is not None else None)),
        # "years of experience with X" is about one skill: check it.
        (r"years.*experience|experience.*years|total experience", lambda: yaml_("work.years_total", f"{w.years_total:g}" if w.years_total is not None else None, "medium" if re.search(r"\b(with|in|using|of) (?!experience)\w+", q.split("experience", 1)[-1]) else "high")),
        # authorization (depends on where the job is)
        (r"sponsor", lambda: _by_country(country, False, w.needs_sponsorship_elsewhere, "work.needs_sponsorship_elsewhere")),
        (r"(authori[sz]ed|eligible|legally|right|permit(ted)?) to work|work (authori[sz]ation|permit)", lambda: _by_country(country, w.authorized_india, False, "work.authorized_india")),
        # preferences
        (r"relocat", lambda: yaml_("preferences.relocation", True if choice else p.relocation, "medium")),
        (r"travel", lambda: yaml_("preferences.travel", True if choice else p.travel, "medium")),
        (r"in[- ]person|on-?site|in[- ]office|hybrid|from (the|our) office", lambda: yaml_("preferences.work_mode", True if choice else p.work_mode, "medium")),
        # education
        (r"\b(c?gpa|grade point|cumulative grade)\b", lambda: yaml_("education[0].gpa", edu.gpa)),
        (r"graduat.*(year|date)|year of (graduation|passing)", lambda: yaml_("education[0].graduation_year", edu.graduation_year)),
        (r"\b(school|university|college|institution)\b", lambda: yaml_("education[0].institution", edu.institution)),
        (r"^(highest )?degree|qualification", lambda: yaml_("education[0].degree", edu.degree)),
        (r"field of study|discipline|\bmajor\b|speciali[sz]ation", lambda: yaml_("education[0].field", edu.field)),
        # location
        (r"^country$|country of residence", lambda: yaml_("identity.country", i.country)),
        (r"\bcity\b|location|where are you (currently )?(located|based)|address|where do you live", lambda: yaml_("identity.city", f"{i.city}, {i.country}" if i.city else "", "medium" if "address" in q else "high")),
    ]  # fmt: skip
    for pattern, get in rules:
        if re.search(pattern, q):
            return get()
    return None


def _expected(w: Work, country) -> Answer | None:
    if country == "india" and w.expected_ctc_lpa:
        return Answer(_lpa(w.expected_ctc_lpa), "work.expected_ctc_lpa")
    if country == "abroad" and w.expected_usd:
        return Answer(f"USD {w.expected_usd:,}", "work.expected_usd", "medium")
    return None  # unclear currency: you decide


def _by_country(country, india_value, abroad_value, key) -> Answer | None:
    value = {"india": india_value, "abroad": abroad_value}.get(country)
    if value is None:
        return None  # unclear where the job is: you decide
    return Answer(bool(value), f"{key} (job in {country})")


# Questions the LLM must never answer: their answers come only from answers.yaml. Each maps to
# the key to fill in when it's missing. "expected" pay depends on where the job is.
SENSITIVE = [
    (r"hispanic|latin[oax]", "standard.eeo.hispanic_latino"),
    (r"\brace\b|ethnicit", "standard.eeo.race_ethnicity"),
    (r"pronoun", "standard.eeo.pronouns"),
    (r"\bgender\b|\bsex\b", "standard.eeo.gender"),
    (r"veteran", "standard.eeo.veteran"),
    (r"disabilit", "standard.eeo.disability"),
    (r"sexual orientation|religio|marital|date of birth|\bage\b", "custom"),
    (r"e-?mail", "identity.email"),
    (r"phone|mobile|contact number", "identity.phone"),
    (r"notice period", "work.notice_period"),
    (
        r"(current|present|existing|last).*(ctc|salary|compensation|package|pay)",
        "work.current_ctc_lpa",
    ),
    (r"\bctc\b|salary|compensation|\bpay\b|remuneration|\blpa\b", "expected"),
    (r"sponsor|\bvisa\b", "work.needs_sponsorship_elsewhere"),
    (
        r"(authori[sz]ed|eligible|legally|right|permit(ted)?) to work|work (authori[sz]ation|permit)|citizenship",
        "work.authorized_india",
    ),
]


def sensitive_key(label: str, location: str | None) -> str | None:
    """The answers.yaml key a sensitive question needs, or None if it isn't sensitive."""
    q = clean(label)
    for pattern, key in SENSITIVE:
        if re.search(pattern, q):
            if key == "expected":
                abroad = job_country(location) == "abroad"
                return "work.expected_usd" if abroad else "work.expected_ctc_lpa"
            return key
    return None


def bank_has(a: Answers, key: str) -> bool:
    """Whether answers.yaml has a value at a dotted key (False counts; empty doesn't)."""
    if key == "custom":
        return False  # a question only a custom: entry can answer, and none matched
    value = a
    for part in key.split("."):
        value = getattr(value, part, None)
    return value not in (None, "")


def custom(label: str, a: Answers) -> Answer | None:
    """Your own answers: an exact or near match of the question."""
    q = clean(label)
    best, best_ratio = None, 0.0
    for c in a.custom:
        ratio = difflib.SequenceMatcher(None, q, clean(c.q)).ratio()
        if ratio > best_ratio:
            best, best_ratio = c, ratio
    if best and best_ratio >= 0.85:
        return Answer(best.a, "custom")
    return None


def lookup(label: str, kind: str, a: Answers, location: str | None) -> Answer | None:
    return custom(label, a) or standard(label, kind, a, location)


# ---- choosing an option ---------------------------------------------------------------------


def _words(s: str) -> list[str]:
    s = s.lower().replace("’", "'").replace("n't", " not").replace("can not", "cannot")
    return re.sub(r"[^a-z0-9+]+", " ", s).split()


YES_NO = {"yes", "no"}


def pick_option(answer: str | bool, options: list[str]) -> str | None:
    """The one option that clearly matches the answer, else None (the field is left empty).

    Clearly = equal words, or the only option containing all the answer's words, or the only
    option starting with yes/no for a yes/no answer. Never a guess between similar options.
    """
    if not options:
        return None
    if isinstance(answer, bool):
        want = "yes" if answer else "no"
        hits = [o for o in options if _words(o)[:1] == [want]]
        return hits[0] if len(hits) == 1 else None
    a = _words(answer)
    if not a:
        return None
    exact = [o for o in options if _words(o) == a]
    if len(exact) == 1:
        return exact[0]
    containing = [o for o in options if set(a) <= set(_words(o))]
    if a[0] in YES_NO:  # "No, I don't have ..." must land on a "No ..." option
        containing = [o for o in containing if _words(o)[:1] == a[:1]]
    return containing[0] if len(containing) == 1 else None


def as_text(value: str | bool) -> str:
    return ("Yes" if value else "No") if isinstance(value, bool) else value
