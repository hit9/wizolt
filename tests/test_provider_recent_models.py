"""Regression contracts for model behavior that the generic path cannot express."""

import pytest
from catalog_harness import reasoning_choices, resolve
from test_core_logic import session

from wizolt.config import ProviderConfig
from wizolt.model import ModelClient


@pytest.mark.parametrize("model", ("gpt-6-sol", "gpt-6-luna", "openai/gpt-6-sol"))
@pytest.mark.parametrize("api", ("chat", "responses"))
def test_gpt6_optional_reasoning_can_be_disabled(tmp_path, model, api):
    provider = ProviderConfig(url="https://api.openai.com/v1", model=model, api=api, reasoning="off")
    assert "off" in reasoning_choices(provider)
    params = {}
    ModelClient(session(tmp_path)).apply_provider_params(params, provider)
    expected = {"reasoning_effort": "none"} if api == "chat" else {"reasoning": {"effort": "none"}}
    assert params == expected


@pytest.mark.parametrize(
    ("model", "levels", "requested", "sent"),
    (
        ("grok-4.7", ("low", "medium", "high", "xhigh"), "max", "xhigh"),
        ("gemini-3.8-flash", ("low", "medium", "high"), "minimal", "low"),
        ("gemini-3.7-flash", ("low", "medium", "high"), "max", "high"),
        ("gemini-3.6-flash", ("minimal", "low", "medium", "high"), "minimal", "minimal"),
    ),
)
def test_recent_models_send_only_supported_chat_efforts(tmp_path, model, levels, requested, sent):
    provider = ProviderConfig(url="https://gateway.example/v1", model=model, reasoning=requested)
    assert reasoning_choices(provider) == levels
    params = {}
    ModelClient(session(tmp_path)).apply_provider_params(params, provider)
    assert params == {"reasoning_effort": sent}


def test_deepseek_pro_ga_no_longer_folds_low_into_high(tmp_path):
    provider = ProviderConfig(url="https://api.deepseek.com", model="deepseek-v4-pro", reasoning="low")
    assert reasoning_choices(provider) == ("off", "low", "high", "max")
    params = {}
    ModelClient(session(tmp_path)).apply_provider_params(params, provider)
    assert params == {"reasoning_effort": "low", "extra_body": {"thinking": {"type": "enabled"}}}


@pytest.mark.parametrize("model", ("claude-sonnet-5-5", "anthropic.claude-sonnet-5-5", "claude-opus-5-5"))
@pytest.mark.parametrize("reasoning", ("off", "high", "max"))
def test_claude55_uses_supported_thinking_modes(tmp_path, model, reasoning):
    client = ModelClient(session(tmp_path))
    provider = client.session.config.provider
    provider.url, provider.api = "https://api.anthropic.com", "anthropic"
    provider.model, provider.reasoning, provider.temperature = model, reasoning, 0.3
    params = client.wire(provider).params([{"role": "user", "content": "hello"}], None)
    if reasoning != "off":
        assert params["thinking"] == {"type": "adaptive"}
        assert params["output_config"] == {"effort": reasoning}
    elif "sonnet" in model:
        assert params["thinking"] == {"type": "between_tools"}
        assert params.get("output_config", {}).get("effort", "high") in ("low", "medium", "high")
    else:
        assert params.get("thinking", {}).get("type", "adaptive") == "adaptive"
        assert "off" not in reasoning_choices(provider)
    assert "temperature" not in params
    assert "temperature" not in params.get("extra_body", {})


@pytest.mark.parametrize("model", ("gemini-future", "grok-future", "custom/gpt-6-sol", "production-model", "MiniMax-M3"))
def test_unknown_or_generic_models_do_not_gain_speculative_rules(tmp_path, model):
    provider = ProviderConfig(url="https://gateway.example/v1", model=model, reasoning="high")
    assert resolve(provider).chat_reasoning == "off"
    params = {}
    ModelClient(session(tmp_path)).apply_provider_params(params, provider)
    assert params == {}
