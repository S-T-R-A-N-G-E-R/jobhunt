import hashlib
import os
import sys
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

ROOT = Path(os.environ.get("JOBHUNT_HOME", Path(__file__).resolve().parents[2]))
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
OUTPUT_DIR = ROOT / "output"
PROFILE_PATH = CONFIG_DIR / "profile.yaml"
COMPANIES_PATH = CONFIG_DIR / "companies.yaml"
COMPANIES_EXAMPLE_PATH = CONFIG_DIR / "companies.example.yaml"
RESUME_PATH = DATA_DIR / "resume.md"
DB_PATH = DATA_DIR / "jobhunt.db"
PROMPT_PATH = Path(__file__).parent / "prompts" / "score_job.md"
PROMPTS_DIR = PROMPT_PATH.parent
# Applying (laptop only): answer bank, application state, browser profile, materials.
ANSWERS_PATH = CONFIG_DIR / "answers.yaml"
APPS_DB_PATH = DATA_DIR / "applications.db"
BROWSER_DIR = ROOT / ".browser-profile"

load_dotenv(ROOT / ".env")


class Locations(BaseModel):
    accept: list[str] = []
    remote_ok: bool = True
    relocation_ok: bool = False
    relocation_cities: list[str] = []  # where relocation is OK (the LLM judges this)
    relocation_notes: str = ""


class LocalLLM(BaseModel):
    provider: str = "ollama"
    model: str = "qwen3.5:4b"
    think: bool | str = False  # gpt-oss can't turn thinking off: use "low"
    keep_alive: str = "10m"
    timeout_seconds: float = 120
    num_ctx: int = 16384


class FallbackLLM(BaseModel):
    enabled: bool = False
    provider: str = "anthropic"
    model: str = "claude-haiku-5-5"
    max_calls_per_run: int = 10
    heavy_tasks: list[str] = []  # tasks that always use the paid model


class LLMConfig(BaseModel):
    local: LocalLLM = LocalLLM()
    fallback: FallbackLLM = FallbackLLM()


class Scoring(BaseModel):
    min_score_for_digest: int = 70
    max_llm_calls_per_run: int = 60


class Digest(BaseModel):
    max_jobs: int = 10
    stretch_max: int = 2  # roles capped for seniority that would otherwise match; 0 = off


class Profile(BaseModel):
    target_titles: list[str]
    title_exclude_keywords: list[str] = []
    seniority: str = ""
    years_experience: float | None = None
    locations: Locations = Locations()
    compensation: dict = {}
    must_haves: list[str] = []
    nice_to_haves: list[str] = []
    dealbreakers: list[str] = []
    what_i_want: str = ""
    llm: LLMConfig = LLMConfig()
    scoring: Scoring = Scoring()
    digest: Digest = Digest()


class Company(BaseModel):
    name: str
    ats: Literal["greenhouse", "lever", "ashby"]
    slug: str
    priority: Literal["high", "normal"] = "normal"
    region: Literal["eu"] | None = None


def load_profile() -> Profile:
    if not PROFILE_PATH.exists():
        sys.exit(
            f"Missing {PROFILE_PATH}.\n"
            "Copy the example and fill it in:\n"
            f'  cp "{CONFIG_DIR}/profile.example.yaml" "{PROFILE_PATH}"'
        )
    profile = Profile.model_validate(yaml.safe_load(PROFILE_PATH.read_text()) or {})
    # Env overrides, so a cloud run can use another model with the same profile.yaml.
    local = profile.llm.local
    if env("JOBHUNT_LLM_MODEL"):
        local.model = env("JOBHUNT_LLM_MODEL")
    think = env("JOBHUNT_LLM_THINK").lower()
    if think:
        local.think = {"true": True, "false": False}.get(think, think)
    return profile


def load_companies_raw(create: bool = False) -> dict:
    """The parsed companies.yaml. If it's missing: start empty when `create`, else exit."""
    if not COMPANIES_PATH.exists():
        if create:
            return {"companies": [], "manual_check": []}
        sys.exit(
            f"Missing {COMPANIES_PATH}.\n"
            "Copy the example and edit it, or add companies one by one:\n"
            f'  cp "{COMPANIES_EXAMPLE_PATH}" "{COMPANIES_PATH}"\n'
            "  uv run jobhunt company add <careers-url>"
        )
    data = yaml.safe_load(COMPANIES_PATH.read_text()) or {}
    data.setdefault("companies", [])
    data.setdefault("manual_check", [])
    data["companies"] = data["companies"] or []
    data["manual_check"] = data["manual_check"] or []
    return data


def load_companies() -> list[Company]:
    return [Company.model_validate(c) for c in load_companies_raw()["companies"]]


def companies_header() -> str:
    """The comment block at the top of the example file, reused when writing companies.yaml."""
    if not COMPANIES_EXAMPLE_PATH.exists():
        return ""
    lines = []
    for line in COMPANIES_EXAMPLE_PATH.read_text().splitlines():
        if not line.startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines) + "\n" if lines else ""


def load_resume() -> str:
    return RESUME_PATH.read_text() if RESUME_PATH.exists() else ""


def profile_hash() -> str:
    """Changes whenever profile.yaml or the scoring prompt changes."""
    h = hashlib.sha256()
    for p in (PROFILE_PATH, PROMPT_PATH):
        h.update(p.read_bytes() if p.exists() else b"")
    return h.hexdigest()[:16]


def local_tz() -> tzinfo:
    """JOBHUNT_TIMEZONE (an IANA name like "Europe/Berlin") if set, else the machine's zone.

    "Today" for runs and digests is computed in this zone.
    """
    name = os.environ.get("JOBHUNT_TIMEZONE", "").strip()
    return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default
