import pytest
from pydantic import ValidationError

from orchestrator.config import AutonomyLevel, OrchestratorConfig, RetryPolicy


def test_retry_backoff_is_bounded_and_grows():
    policy = RetryPolicy(backoff_seconds=1.0, backoff_multiplier=2.0, max_backoff_seconds=5.0)
    assert policy.delay_for(1) == 0.0          # first attempt never waits
    assert policy.delay_for(2) == 1.0
    assert policy.delay_for(3) == 2.0
    assert policy.delay_for(4) == 4.0
    assert policy.delay_for(5) == 5.0          # capped
    assert policy.delay_for(99) == 5.0


def test_max_attempts_of_one_means_no_retry():
    assert RetryPolicy(max_attempts=1).max_attempts == 1
    with pytest.raises(ValidationError):
        RetryPolicy(max_attempts=0)


def test_autonomy_ordering():
    assert AutonomyLevel.SUGGEST.rank < AutonomyLevel.SUPERVISED.rank
    assert AutonomyLevel.BOUNDED.rank < AutonomyLevel.AUTONOMOUS.rank


def test_from_env_prefers_anthropic_when_key_present(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert OrchestratorConfig.from_env().provider == "anthropic"


def test_from_env_defaults_to_deterministic(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert OrchestratorConfig.from_env().provider == "deterministic"


def test_explicit_override_beats_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert OrchestratorConfig.from_env(provider="deterministic").provider == "deterministic"
