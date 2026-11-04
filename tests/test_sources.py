import asyncio
import json

import httpx
import pytest

from jobhunt.config import Company
from jobhunt.net import Http
from jobhunt.sources import ashby, detect_ats, fetch_all, greenhouse, lever

from .conftest import FIXTURES, FakeBoards


@pytest.mark.parametrize(
    "mod,name", [(greenhouse, "greenhouse"), (lever, "lever"), (ashby, "ashby")]
)
def test_parse_real_fixture(mod, name):
    jobs = mod.parse(json.loads((FIXTURES / f"{name}.json").read_text()), "Co")
    assert jobs
    for j in jobs:
        assert j.source == name and j.company == "Co"
        assert j.external_id and j.title and j.url.startswith("https://")
        assert len(j.description_text) > 100
        assert "<p" not in j.description_text and "&lt;" not in j.description_text
        assert len(j.content_hash) == 16


def test_synthetic_fixture_details():
    (gh, *_) = greenhouse.parse(json.loads((FIXTURES / "greenhouse.json").read_text()), "Northwind")
    assert gh.title == "Machine Learning Engineer, Search" and gh.location == "Bengaluru, India"
    assert gh.department == "Search Relevance" and gh.posted_at.startswith("2026-01-10")
    assert "health cover" in gh.description_text and "&amp;" not in gh.description_text

    lv = lever.parse(json.loads((FIXTURES / "lever.json").read_text()), "Lumen")
    assert lv[0].compensation_text == "INR 2,500,000–3,500,000 per year salary"
    assert lv[1].is_remote and not lv[2].is_remote
    assert "What you will need" in lv[0].description_text

    ab = ashby.parse(json.loads((FIXTURES / "ashby.json").read_text()), "Quillstone")
    assert ab[0].location == "Bengaluru, India; Remote - India"
    assert ab[0].compensation_text == "₹30L – ₹45L" and ab[1].is_remote


def test_greenhouse_unescapes_html():
    data = {
        "jobs": [
            {
                "id": 1,
                "title": " T ",
                "location": {"name": "Remote, India"},
                "absolute_url": "https://x",
                "content": "&lt;p&gt;A &amp;amp; B&lt;/p&gt;",
            }
        ]
    }
    (job,) = greenhouse.parse(data, "Co")
    assert job.description_text == "A & B"
    assert job.title == "T" and job.is_remote


def test_content_hash_changes_with_content():
    a = greenhouse.parse(
        {"jobs": [{"id": 1, "title": "T", "absolute_url": "u", "content": "x"}]}, "C"
    )
    b = greenhouse.parse(
        {"jobs": [{"id": 1, "title": "T", "absolute_url": "u", "content": "y"}]}, "C"
    )
    assert a[0].content_hash != b[0].content_hash


def test_lever_eu_host():
    assert lever.api_url("zoox", "eu").startswith("https://api.eu.lever.co/")
    assert lever.api_url("zoox").startswith("https://api.lever.co/")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://boards.greenhouse.io/acme", ("greenhouse", "acme", None)),
        ("https://job-boards.greenhouse.io/acme/jobs/123", ("greenhouse", "acme", None)),
        ("https://jobs.lever.co/zoox", ("lever", "zoox", None)),
        ("https://jobs.eu.lever.co/zoox", ("lever", "zoox", "eu")),
        ("https://jobs.ashbyhq.com/orbital", ("ashby", "orbital", None)),
        ("jobs.ashbyhq.com/orbital/abc", ("ashby", "orbital", None)),
        ("https://careers.example.com/jobs", None),
        ("https://www.linkedin.com/jobs/view/1", None),
    ],
)
def test_detect_ats(url, expected):
    assert detect_ats(url) == expected


def test_one_failing_company_does_not_break_fetch():
    companies = [
        Company(name="Acme", ats="greenhouse", slug="acme"),
        Company(name="Lev", ats="lever", slug="lev"),
    ]
    ok, errors = asyncio.run(fetch_all(companies, FakeBoards(fail={"lev"}).transport()))
    assert set(ok) == {"Acme"} and len(ok["Acme"]) == 2
    assert set(errors) == {"Lev"} and "404" in errors["Lev"]


def test_retries_server_errors_then_succeeds():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": 1})

    async def go():
        http = Http(transport=httpx.MockTransport(handler), backoff=0)
        try:
            return await http.get_json("https://api.lever.co/v0/postings/x")
        finally:
            await http.aclose()

    assert asyncio.run(go()) == {"ok": 1}
    assert len(calls) == 3
    assert "jobhunt" in calls[0].headers["user-agent"]
