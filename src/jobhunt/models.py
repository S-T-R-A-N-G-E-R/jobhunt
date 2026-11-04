import hashlib
import html
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Literal

from pydantic import BaseModel, Field


class Job(BaseModel):
    source: str
    company: str
    external_id: str
    title: str
    location: str = ""
    is_remote: bool = False
    department: str | None = None
    url: str
    description_text: str = ""
    compensation_text: str | None = None
    posted_at: str | None = None
    fetched_at: str = Field(default_factory=lambda: now_utc())
    content_hash: str = ""

    def model_post_init(self, _ctx) -> None:
        if not self.content_hash:
            raw = "\n".join([self.title, self.location, self.description_text])
            self.content_hash = hashlib.sha256(raw.encode()).hexdigest()[:16]


class ScoreOutput(BaseModel):
    """What the LLM must return for one job."""

    score: int = Field(ge=0, le=100)
    verdict: Literal["apply", "maybe", "skip"]
    reasons: list[str] = Field(max_length=3)
    red_flags: list[str]
    seniority_match: Literal["under", "match", "over"]
    extracted_comp: str | None
    one_line_pitch: str


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


_BLOCK_TAGS = {"p", "br", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "li":
            self.parts.append("- ")

    def handle_endtag(self, tag):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(raw: str | None) -> str:
    """Strip HTML (also HTML that arrives entity-escaped, as Greenhouse sends it)."""
    if not raw:
        return ""
    if "&lt;" in raw:
        raw = html.unescape(raw)
    p = _TextExtractor()
    p.feed(raw)
    return normalize_ws("".join(p.parts))


def normalize_ws(text: str) -> str:
    text = text.replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def looks_remote(*texts: str | None) -> bool:
    return any(t and "remote" in t.lower() for t in texts)
