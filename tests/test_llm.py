import json

import pytest

from jobhunt.config import LLMConfig
from jobhunt.llm import LLM, LLMError, QuotaError
from jobhunt.models import ScoreOutput

from .conftest import GOOD_SCORE, FakeLLM

INVALID = json.dumps({"score": 150, "verdict": "yes"})


def make(fake, enabled=True, cap=10):
    return LLM(
        LLMConfig(fallback={"enabled": enabled, "max_calls_per_run": cap}),
        transport=fake.transport(),
    )


def test_valid_local_output(monkeypatch):
    fake = FakeLLM()
    r = make(fake).complete_json("p", ScoreOutput, "score_job")
    assert (r.provider, r.model, r.escalated) == ("ollama", "qwen3.5:4b", False)
    assert r.obj.score == 85 and fake.chat_calls == 1


def test_invalid_then_retry_succeeds():
    fake = FakeLLM([INVALID, json.dumps(GOOD_SCORE)])
    r = make(fake).complete_json("p", ScoreOutput, "score_job")
    assert fake.chat_calls == 2 and not r.escalated


def test_invalid_twice_escalates_to_paid(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake = FakeLLM([INVALID])
    r = make(fake).complete_json("p", ScoreOutput, "score_job")
    assert fake.chat_calls == 2 and fake.paid_calls == 1
    assert r.escalated and r.provider == "anthropic" and r.obj.score == 77


def test_no_escalation_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    fake = FakeLLM([INVALID])
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY not set"):
        make(fake).complete_json("p", ScoreOutput, "score_job")
    assert fake.paid_calls == 0


def test_paid_cap_respected(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake = FakeLLM([INVALID])
    llm = make(fake, cap=1)
    llm.complete_json("p", ScoreOutput, "score_job")
    with pytest.raises(LLMError, match="cap reached"):
        llm.complete_json("p", ScoreOutput, "score_job")
    assert fake.paid_calls == 1


def test_health_check_ollama_down():
    assert "open the Ollama app" in make(FakeLLM(ollama_up=False)).health_check()


def test_health_check_model_missing():
    msg = make(FakeLLM(models=("llama3:8b",))).health_check()
    assert "ollama pull qwen3.5:4b" in msg


def test_health_check_ok():
    assert make(FakeLLM()).health_check() is None


def test_ollama_cloud_sends_bearer_only_over_https(monkeypatch):
    monkeypatch.setenv("OLLAMA_API_KEY", "ok-test")
    fake = FakeLLM(models=("gpt-oss:20b",))
    monkeypatch.setenv("OLLAMA_HOST", "https://ollama.com")
    llm = LLM(LLMConfig(local={"model": "gpt-oss:20b", "think": "low"}), transport=fake.transport())
    assert llm.health_check() is None
    llm.complete_json("p", ScoreOutput, "score_job")
    assert {r.headers.get("authorization") for r in fake.requests} == {"Bearer ok-test"}
    assert json.loads(fake.requests[-1].content)["think"] == "low"

    monkeypatch.setenv("OLLAMA_HOST", "http://localhost:11434")
    fake = FakeLLM()
    make(fake).complete_json("p", ScoreOutput, "score_job")
    assert all("authorization" not in r.headers for r in fake.requests)


def test_quota_error_is_not_retried_or_escalated(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    fake = FakeLLM(quota_after=0)
    with pytest.raises(QuotaError):
        make(fake).complete_json("p", ScoreOutput, "score_job")
    assert fake.paid_calls == 0 and len(fake.requests) == 1


def test_env_overrides_model_and_think(home, monkeypatch):
    from jobhunt import config

    monkeypatch.setenv("JOBHUNT_LLM_MODEL", "gpt-oss:20b")
    monkeypatch.setenv("JOBHUNT_LLM_THINK", "low")
    local = config.load_profile().llm.local
    assert (local.model, local.think) == ("gpt-oss:20b", "low")
    monkeypatch.setenv("JOBHUNT_LLM_THINK", "false")
    assert config.load_profile().llm.local.think is False
