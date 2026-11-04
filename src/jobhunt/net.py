"""Shared async HTTP client for job-board APIs: polite rate limit, retries, backoff."""

import asyncio
import time
from collections import defaultdict

import httpx

USER_AGENT = (
    "jobhunt/0.1 (personal job search tool; reads public job-board APIs; "
    "+https://github.com/S-T-R-A-N-G-E-R/jobhunt)"
)
RETRIES = 3
MIN_INTERVAL = 1.0  # seconds between requests to the same host


class Http:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, backoff: float = 1.0):
        self.client = httpx.AsyncClient(
            headers={"User-Agent": USER_AGENT},
            timeout=20,
            transport=transport,
            follow_redirects=True,
        )
        self.backoff = backoff
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last: dict[str, float] = {}

    async def get_json(self, url: str):
        host = httpx.URL(url).host
        for attempt in range(RETRIES + 1):
            async with self._locks[host]:
                wait = self._last.get(host, 0) + MIN_INTERVAL - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                self._last[host] = time.monotonic()
                try:
                    r = await self.client.get(url)
                    retryable = r.status_code == 429 or r.status_code >= 500
                    if not retryable or attempt == RETRIES:
                        r.raise_for_status()
                        return r.json()
                except httpx.TransportError:
                    if attempt == RETRIES:
                        raise
            await asyncio.sleep(self.backoff * 2**attempt)

    async def aclose(self):
        await self.client.aclose()
