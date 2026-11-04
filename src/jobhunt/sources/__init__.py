import asyncio
import logging
import re

import httpx

from ..config import Company
from ..models import Job
from ..net import Http
from . import ashby, greenhouse, lever

log = logging.getLogger(__name__)

MODULES = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}

_URL_PATTERNS = [
    (r"^(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io$", "greenhouse"),
    (r"^boards-api\.greenhouse\.io$", "greenhouse"),
    (r"^jobs\.lever\.co$", "lever"),
    (r"^jobs\.eu\.lever\.co$", "lever-eu"),
    (r"^jobs\.ashbyhq\.com$", "ashby"),
]


def detect_ats(url: str) -> tuple[str, str, str | None] | None:
    """Return (ats, slug, region) for a supported careers URL, else None."""
    u = httpx.URL(url if "://" in url else f"https://{url}")
    parts = [p for p in u.path.split("/") if p]
    for pattern, ats in _URL_PATTERNS:
        if re.match(pattern, u.host or "") and parts:
            slug = parts[2] if parts[0] == "v1" and len(parts) > 2 else parts[0]
            if ats == "lever-eu":
                return "lever", slug, "eu"
            return ats, slug, None
    return None


async def fetch_company(http: Http, company: Company) -> list[Job]:
    mod = MODULES[company.ats]
    data = await http.get_json(mod.api_url(company.slug, company.region))
    return mod.parse(data, company.name)


async def fetch_all(
    companies: list[Company], transport: httpx.AsyncBaseTransport | None = None
) -> tuple[dict[str, list[Job]], dict[str, str]]:
    """Fetch every company concurrently (rate limited per host).

    Returns ({company: jobs} for successes, {company: error} for failures).
    """
    http = Http(transport=transport)
    try:
        results = await asyncio.gather(
            *(fetch_company(http, c) for c in companies), return_exceptions=True
        )
    finally:
        await http.aclose()
    ok, errors = {}, {}
    for company, res in zip(companies, results, strict=True):
        if isinstance(res, BaseException):
            msg = f"{type(res).__name__}: {str(res).splitlines()[0] if str(res) else ''}"[:300]
            log.error("fetch failed for %s: %s", company.name, msg)
            errors[company.name] = msg
        else:
            log.info("fetched %d jobs from %s", len(res), company.name)
            ok[company.name] = res
    return ok, errors
