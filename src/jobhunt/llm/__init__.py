"""One small interface for every LLM call: complete_json(prompt, schema, task)."""

import logging
import time
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ValidationError

from ..config import LLMConfig, env
from . import anthropic, ollama
from .ollama import QuotaError

__all__ = ["LLM", "LLMError", "LLMResult", "QuotaError"]

log = logging.getLogger(__name__)


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    obj: BaseModel
    provider: str
    model: str
    latency_ms: int
    escalated: bool


def _parse(schema: type[BaseModel], raw: str | dict) -> BaseModel:
    if isinstance(raw, dict):
        return schema.model_validate(raw)
    return schema.model_validate_json(raw)


def _short(err: Exception) -> str:
    if isinstance(err, ValidationError):
        return "; ".join(
            f"{'.'.join(map(str, e['loc'])) or 'output'}: {e['msg']}" for e in err.errors()[:5]
        )
    return f"{type(err).__name__}: {err}"[:300]


class LLM:
    def __init__(
        self, cfg: LLMConfig, transport: httpx.BaseTransport | None = None, host: str = ""
    ):
        self.cfg = cfg
        self.http = httpx.Client(transport=transport)
        self.paid_calls = 0
        self.host = host  # overrides OLLAMA_HOST (the writing model on Ollama Cloud)

    @property
    def ollama_host(self) -> str:
        return (self.host or env("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")

    def fallback_unavailable(self) -> str | None:
        """Why the paid fallback can't be used right now (None = it can)."""
        fb = self.cfg.fallback
        if not fb.enabled:
            return "fallback disabled in profile.yaml"
        if not env("ANTHROPIC_API_KEY"):
            return "ANTHROPIC_API_KEY not set"
        if self.paid_calls >= fb.max_calls_per_run:
            return f"paid call cap reached ({fb.max_calls_per_run}/run)"
        return None

    def health_check(self) -> str | None:
        """None if Ollama is up and the model is pulled, else a message with the fix."""
        model = self.cfg.local.model
        try:
            r = self.http.get(
                f"{self.ollama_host}/api/tags", headers=ollama.headers(self.ollama_host), timeout=5
            )
            r.raise_for_status()
        except httpx.HTTPError:
            return (
                f"Ollama isn't running at {self.ollama_host}: open the Ollama app "
                "(or run `ollama serve`)"
            )
        names = {m.get("name") for m in r.json().get("models", [])}
        if model not in names and f"{model}:latest" not in names:
            return f"Ollama model {model} isn't pulled: run `ollama pull {model}`"
        return None

    def local(self, messages: list[dict], schema: type[BaseModel]) -> str:
        return ollama.chat(
            self.http, self.ollama_host, self.cfg.local, messages, schema.model_json_schema()
        )

    def paid(self, prompt: str, schema: type[BaseModel]) -> dict:
        self.paid_calls += 1
        return anthropic.complete(
            self.http,
            env("ANTHROPIC_API_KEY"),
            self.cfg.fallback.model,
            prompt,
            schema.model_json_schema(),
        )

    def complete_json(self, prompt: str, schema: type[BaseModel], task: str) -> LLMResult:
        """Raises LLMError on failure, or QuotaError (never escalated) when Ollama Cloud is out
        of credits."""
        start = time.monotonic()

        def result(obj, provider, model, escalated):
            ms = int((time.monotonic() - start) * 1000)
            return LLMResult(obj, provider, model, ms, escalated)

        fb = self.cfg.fallback
        if task in fb.heavy_tasks and not self.fallback_unavailable():
            log.info("task %s marked heavy: using %s", task, fb.model)
            return result(_parse(schema, self.paid(prompt, schema)), fb.provider, fb.model, False)

        messages = [{"role": "user", "content": prompt}]
        raw = ""
        try:
            raw = self.local(messages, schema)
            return result(_parse(schema, raw), self.cfg.local.provider, self.cfg.local.model, False)
        except (httpx.HTTPError, ValidationError, ValueError, KeyError) as e:
            err = _short(e)
            log.warning("local %s output invalid (%s); retrying once", task, err)
        messages += [
            {"role": "assistant", "content": raw or "(no output)"},
            {
                "role": "user",
                "content": f"Your previous output was invalid because: {err}. "
                "Reply with only JSON that matches the schema.",
            },
        ]
        try:
            raw = self.local(messages, schema)
            return result(_parse(schema, raw), self.cfg.local.provider, self.cfg.local.model, False)
        except (httpx.HTTPError, ValidationError, ValueError, KeyError) as e:
            err = _short(e)

        why_not = self.fallback_unavailable()
        if why_not:
            raise LLMError(f"local output invalid after retry ({err}); not escalating: {why_not}")
        log.warning("escalating %s to paid model %s after local failure: %s", task, fb.model, err)
        try:
            obj = _parse(schema, self.paid(prompt, schema))
        except (httpx.HTTPError, ValidationError, ValueError, KeyError) as e:
            raise LLMError(f"local failed ({err}); paid fallback failed too ({_short(e)})") from e
        return result(obj, fb.provider, fb.model, True)
