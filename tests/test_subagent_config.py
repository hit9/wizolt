"""Creation defaults are detached from live agents; legacy syntax ends at config loading."""

import pytest

from wizolt.base import ConfigError
from wizolt.config import Config


def config_data(**sections):
    return {"provider": {"active": "main", "main": {"model": "gpt-4"}, "deepseek": {"model": "deepseek-chat", "reasoning": "high"}}, **sections}


@pytest.mark.parametrize("section", ["worker", "subagent"])
def test_creation_defaults_select_provider_without_mutating_parent(section):
    config = Config.from_dict(config_data(**{section: {"provider": "deepseek"}}))
    child = config.for_subagent()
    assert child.active_provider == "deepseek"
    assert child.provider.model == "deepseek-chat" and child.provider.reasoning == "high"
    assert config.active_provider == "main"
    child.provider.model = "approval-model"
    assert config.providers["deepseek"].model == "deepseek-chat"


def test_new_section_overrides_legacy_per_field_and_empty_means_inherit():
    config = Config.from_dict(config_data(worker={"provider": "deepseek", "model": "legacy-model", "reasoning": "high"}, subagent={"provider": "", "model": "new-model", "api": "responses"}))
    child = config.for_subagent()
    assert child.active_provider == "main"
    assert (child.provider.model, child.provider.reasoning, child.provider.api) == ("new-model", "high", "responses")


def test_no_defaults_inherit_live_parent_choices():
    config = Config.from_dict(config_data())
    config.active_provider = "deepseek"
    config.provider.model = "runtime-model"
    assert config.for_subagent().provider.model == "runtime-model"


@pytest.mark.parametrize("section", ["worker", "subagent"])
@pytest.mark.parametrize("field,value", [("provider", "missing"), ("reasoning", "turbo"), ("api", "bogus")])
def test_invalid_creation_defaults_fail_at_config_load(section, field, value):
    with pytest.raises(ConfigError, match=f"subagent.{field}"):
        Config.from_dict(config_data(**{section: {field: value}}))
