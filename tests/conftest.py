import json
import socket
from pathlib import Path

import httpx
import pytest

from jobhunt import config, net, pipeline

FIXTURES = Path(__file__).parent / "fixtures"

PROFILE = """
target_titles: ["Senior Backend Engineer"]
title_exclude_keywords: ["intern"]
seniority: senior
years_experience: 6
locations:
  accept: ["Bengaluru", "Remote - India"]
  remote_ok: true
compensation: {min_expected_ctc_lpa: 40}
dealbreakers: ["contract only"]
what_i_want: Backend work.
llm:
  fallback: {enabled: true, max_calls_per_run: 2}
scoring: {min_score_for_digest: 70, max_llm_calls_per_run: 60}
"""

COMPANIES = """
companies:
  - {name: Acme, ats: greenhouse, slug: acme}
  - {name: Lev, ats: lever, slug: lev}
  - {name: Ash, ats: ashby, slug: ash, priority: high}
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def guard(*args, **kwargs):
        raise RuntimeError("tests must not touch the network")

    monkeypatch.setattr(socket.socket, "connect", guard)
    monkeypatch.setattr(net, "MIN_INTERVAL", 0)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway project dir with a profile, companies, resume and empty DB."""
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "ANTHROPIC_API_KEY",
        "OLLAMA_HOST",
        "OLLAMA_API_KEY",
        "JOBHUNT_LLM_MODEL",
        "JOBHUNT_LLM_THINK",
    ):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "data").mkdir()
    paths = {
        "PROFILE_PATH": tmp_path / "config" / "profile.yaml",
        "COMPANIES_PATH": tmp_path / "config" / "companies.yaml",
        "RESUME_PATH": tmp_path / "data" / "resume.md",
        "DB_PATH": tmp_path / "data" / "jobhunt.db",
        "OUTPUT_DIR": tmp_path / "output",
        "LOG_DIR": tmp_path / "logs",
        "ANSWERS_PATH": tmp_path / "config" / "answers.yaml",
        "APPS_DB_PATH": tmp_path / "data" / "applications.db",
        "BROWSER_DIR": tmp_path / ".browser-profile",
    }
    for attr, value in paths.items():
        monkeypatch.setattr(config, attr, value)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(pipeline, "LOG_FILE", tmp_path / "logs" / "jobhunt.log")
    paths["PROFILE_PATH"].write_text(PROFILE)
    paths["COMPANIES_PATH"].write_text(COMPANIES)
    paths["RESUME_PATH"].write_text("Backend engineer, 6 years of Python.")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    return tmp_path


def board_payloads(extra_greenhouse: list | None = None) -> dict:
    gh = [
        {
            "id": 1,
            "title": "Senior Backend Engineer",
            "location": {"name": "Bengaluru"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
            "content": "&lt;p&gt;Build &amp;amp; run APIs&lt;/p&gt;",
        },
        {
            "id": 2,
            "title": "Account Executive",
            "location": {"name": "Bengaluru"},
            "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
            "content": "Sell",
        },
    ] + (extra_greenhouse or [])
    lever = [
        {
            "id": "l1",
            "text": "Backend Engineer <Payments>",
            "hostedUrl": "https://jobs.lever.co/lev/l1",
            "categories": {"location": "Remote"},
            "workplaceType": "remote",
            "descriptionPlain": "Python services",
            "createdAt": 1760000000000,
        },
    ]
    ashby = {
        "jobs": [
            {
                "id": "a1",
                "title": "Backend Engineer",
                "location": "London",
                "jobUrl": "https://jobs.ashbyhq.com/ash/a1",
                "descriptionPlain": "On-site in London",
                "isRemote": False,
            },
        ]
    }
    return {"acme": {"jobs": gh}, "lev": lever, "ash": ashby}


class FakeBoards:
    """Async transport serving the three job boards; `fail` = set of slugs returning 404."""

    def __init__(self, payloads=None, fail=()):
        self.payloads = payloads or board_payloads()
        self.fail = set(fail)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        slug = next(s for s in self.payloads if f"/{s}" in request.url.path)
        if slug in self.fail:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=self.payloads[slug])

    def transport(self):
        return httpx.MockTransport(self)


GOOD_SCORE = {
    "score": 85,
    "verdict": "apply",
    "reasons": ["Python backend"],
    "red_flags": [],
    "seniority_match": "match",
    "extracted_comp": None,
    "one_line_pitch": "Backend role that matches your Python experience.",
}


class FakeLLM:
    """Sync transport for Ollama + Anthropic. `local_outputs` is consumed in order, then repeats
    the last one. `ollama_up=False` makes every Ollama request fail to connect."""

    def __init__(
        self, local_outputs=None, ollama_up=True, models=("qwen3.5:4b",), quota_after=None
    ):
        self.quota_after = quota_after  # answer 429 once this many chats succeeded
        self.requests: list[httpx.Request] = []
        self.local_outputs = list(local_outputs or [json.dumps(GOOD_SCORE)])
        self.ollama_up = ollama_up
        self.models = models
        self.chat_calls = 0
        self.paid_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.anthropic.com":
            self.paid_calls += 1
            return httpx.Response(
                200,
                json={
                    "content": [
                        {"type": "tool_use", "name": "submit", "input": GOOD_SCORE | {"score": 77}}
                    ]
                },
            )
        if not self.ollama_up:
            raise httpx.ConnectError("connection refused", request=request)
        self.requests.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if self.quota_after is not None and self.chat_calls >= self.quota_after:
            return httpx.Response(429, json={"error": "you have reached your usage limit"})
        self.chat_calls += 1
        out = self.local_outputs.pop(0) if len(self.local_outputs) > 1 else self.local_outputs[0]
        return httpx.Response(200, json={"message": {"content": out}})

    def transport(self):
        return httpx.MockTransport(self)


class FakeTelegram:
    """Bot API fake. `messages` holds sendMessage texts; `sent` the full payloads; `calls` every
    (method, payload); `updates` is served by getUpdates until confirmed with an offset."""

    def __init__(self, down=False, updates=None):
        self.messages: list[str] = []
        self.sent: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.updates = list(updates or [])
        self.down = down

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("no network", request=request)
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        self.calls.append((method, payload))
        if method == "getUpdates":
            offset = payload.get("offset", 0)
            self.updates = [u for u in self.updates if u["update_id"] >= offset]
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        if method == "sendMessage":
            self.messages.append(payload["text"])
            self.sent.append(payload)
        return httpx.Response(200, json={"ok": True, "result": True})

    def transport(self):
        return httpx.MockTransport(self)
