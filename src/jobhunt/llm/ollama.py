import re

import httpx

from ..config import LocalLLM, env

# Ollama Cloud answers 402/429 (or a 4xx naming the limit) when the plan's credits run out.
QUOTA_STATUS = {402, 429}
QUOTA_TEXT = re.compile(r"usage limit|quota|credit|upgrade", re.I)


class QuotaError(Exception):
    """Ollama Cloud refused the request because the account is out of credits or rate-limited."""


def headers(host: str) -> dict:
    """Bearer auth for Ollama Cloud. Only sent over https, never to a local http server."""
    key = env("OLLAMA_API_KEY")
    return {"Authorization": f"Bearer {key}"} if key and host.startswith("https://") else {}


def chat(http: httpx.Client, host: str, cfg: LocalLLM, messages: list[dict], schema: dict) -> str:
    """Native Ollama /api/chat with a JSON-schema constrained output. Returns the raw text."""
    r = http.post(
        f"{host}/api/chat",
        headers=headers(host),
        json={
            "model": cfg.model,
            "messages": messages,
            "stream": False,
            "think": cfg.think,
            "format": schema,
            "options": {"temperature": 0, "num_ctx": cfg.num_ctx},
            "keep_alive": cfg.keep_alive,
        },
        timeout=cfg.timeout_seconds,
    )
    if r.status_code in QUOTA_STATUS or (400 <= r.status_code < 500 and QUOTA_TEXT.search(r.text)):
        raise QuotaError(f"HTTP {r.status_code}: {r.text[:200]}")
    r.raise_for_status()
    return r.json()["message"]["content"]
