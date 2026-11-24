"""The bytes/4 estimate calibrates itself against provider-reported prompt tokens."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from agent_harness import session_with_provider
from model_harness import _MockClientFactory

from wizolt.agent.engine import Agent
from wizolt.base import Billing
from wizolt.config import ProviderConfig
from wizolt.model import ModelClient
from wizolt.session.codec import SessionSnapshotCodec


def record(s, prompt_tokens, billing=Billing.MAIN):
    ModelClient._record_usage(SimpleNamespace(session=s), SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=1), billing)


def test_a_reported_overcount_calibrates_the_parts_but_not_the_anchor(tmp_path):
    s = session_with_provider(tmp_path)
    agent = Agent(s, output_fn=lambda _text: None)
    s.messages = [{"role": "user", "content": "x" * 40_000}]
    raw = agent.context.update_current_tokens(s.system_prompt)
    before = dict(agent.context.breakdown(s.system_prompt))

    record(s, round(raw * 1.2))

    # The anchor stays raw so the next pair measures the tokenizer again; the factor took one
    # EWMA step toward the reported ratio, and every part wears it.
    assert s.state.context_tokens == raw
    assert s.state.context_factor == pytest.approx(1.1, abs=1e-4)
    after = dict(agent.context.breakdown(s.system_prompt))
    assert after == {name: round(tokens * s.state.context_factor) for name, tokens in before.items()}
    assert s.state.context_percent == min(100, round(raw * s.state.context_factor) * 100 // s.request_token_budget())


def test_the_factor_converges_without_cancelling_itself(tmp_path):
    s = session_with_provider(tmp_path)
    agent = Agent(s, output_fn=lambda _text: None)
    s.messages = [{"role": "user", "content": "x" * 40_000}]
    for _ in range(10):
        raw = agent.context.update_current_tokens(s.system_prompt)
        record(s, round(raw * 1.2))
    # A steady 20% overcount converges to 1.2. Anchoring on the calibrated value instead would
    # feed the correction back into the ratio and decay it toward 1.
    assert 1.19 < s.state.context_factor <= 1.2


def test_one_odd_report_cannot_run_the_factor_away(tmp_path):
    s = session_with_provider(tmp_path)
    s.state.context_tokens = 100_000
    s.calibrate_context_estimate(1_000_000)  # 10x: clamped to 2 before the EWMA
    assert s.state.context_factor == 1.5
    s.calibrate_context_estimate(1_000)  # 0.01x: clamped to 0.8
    assert s.state.context_factor == 1.15
    assert 0.8 <= s.state.context_factor <= 2.0


def test_summary_and_vision_reports_do_not_teach_the_factor(tmp_path):
    s = session_with_provider(tmp_path)
    s.state.context_tokens = 100_000
    record(s, 500_000, billing=Billing.COMPACTION)
    record(s, 500_000, billing=Billing.VISION)
    assert s.state.context_factor == 1.0
    assert s.compaction_usage.last_prompt_tokens == 500_000
    assert s.usage.last_prompt_tokens == 0  # vision joins totals, not the main snapshot


def test_the_fill_before_any_report_wears_the_factor(tmp_path):
    s = session_with_provider(tmp_path)
    s.state.context_tokens = 100_000
    s.state.context_factor = 1.2
    s.state.context_factor_for = f"{s.config.provider.api}/{s.config.provider.model}"
    assert s.context_fill()["used"] == 120_000


def test_the_factor_survives_a_resume_on_the_same_model(tmp_path):
    s = session_with_provider(tmp_path)
    s.state.context_tokens = 100_000
    s.calibrate_context_estimate(120_000)
    data = SessionSnapshotCodec.state(s.state)
    assert data["context_factor"] == s.state.context_factor
    s.state = SessionSnapshotCodec.agent_state(data)
    assert s.estimate_factor() == s.state.context_factor > 1.0


def test_a_different_model_reads_the_stored_factor_as_uncalibrated(tmp_path):
    s = session_with_provider(tmp_path)
    s.state.context_tokens = 100_000
    s.calibrate_context_estimate(120_000)
    assert s.estimate_factor() > 1.0
    s.config.provider.model = "another-model"
    assert s.estimate_factor() == 1.0
    # The next report on the new route learns from 1, not from the old model's ratio.
    s.state.context_tokens = 100_000
    s.calibrate_context_estimate(120_000)
    assert s.state.context_factor == pytest.approx(1.1, abs=1e-4)


async def test_compaction_fires_on_the_calibrated_reading(tmp_path):
    """Raw under the threshold, calibrated over it: the pass runs because the provider's tokenizer
    counts more than bytes/4 -- the late-compaction rejection this calibration exists to prevent."""
    s = session_with_provider(tmp_path)
    s.settings.max_context_tokens = 158_000  # budget = limit = 137_520 at the default threshold 1.0
    s.messages = [
        *({"role": role, "content": f"{role} {index} " + "detail " * 400} for index in range(12) for role in ("user", "assistant")),
        {"role": "user", "content": "now"},
    ]
    s.transcript_messages = list(s.messages)

    class StubModel:
        def __init__(self, session):
            self.session = session

        @staticmethod
        def estimated_request_tokens(messages, tools=None):
            return 80_000 if len(messages) > 10 else 5_000

        @staticmethod
        async def api_request(_messages, _tools, *, allow_stream, response_timeout, provider, json_object, billing=Billing.MAIN):
            return None, [], '{"summary": "the spent bulk summarized"}'

        @staticmethod
        def parse_json_object(text):
            return json.loads(text)

    agent = Agent(s, output_fn=lambda _text: None)
    agent.model = agent.context.model = StubModel(s)

    # Uncalibrated: 80k sits under the 110k background-start band and the 137.5k limit alike.
    await agent.prepare_request([])
    assert s.state.compaction_count == 0

    # Three steady 2x reports teach factor 1.875: the same payload reads 150k, over the limit.
    for _ in range(3):
        s.state.context_tokens = 80_000
        s.calibrate_context_estimate(160_000)
    await agent.prepare_request([])
    assert s.state.compaction_count == 1


def test_a_rerouted_request_does_not_teach_the_factor(tmp_path):
    """A `model.request` interceptor may send another provider; the anchor and the key were
    estimated for the configured route, so that report counts a different tokenizer."""
    s = session_with_provider(tmp_path)
    s.state.context_tokens = 100_000
    other = ProviderConfig(model="another-model", url="http://test", key="sk-test")
    ModelClient._record_usage(SimpleNamespace(session=s), SimpleNamespace(prompt_tokens=120_000, completion_tokens=1), provider=other)
    assert s.state.context_factor == 1.0
    # The configured route still calibrates normally.
    ModelClient._record_usage(SimpleNamespace(session=s), SimpleNamespace(prompt_tokens=120_000, completion_tokens=1), provider=s.config.provider)
    assert s.state.context_factor == pytest.approx(1.1, abs=1e-4)


async def test_the_wire_forwards_the_sent_route_to_calibration(tmp_path, monkeypatch):
    """Locks the wire -> _record_usage forwarding the guard depends on: a report from a rerouted
    request must not teach the factor, while the configured route still calibrates."""
    s = session_with_provider(tmp_path)
    s.config.provider.api = "chat"
    alt = replace(s.config.provider, model="gpt-4o")
    body = {"choices": [{"message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 120_000, "completion_tokens": 1}}
    model = ModelClient(s)
    monkeypatch.setattr(model, "client", _MockClientFactory([(200, body), (200, body)]))
    s.state.context_tokens = 100_000
    await model.api_request([{"role": "user", "content": "hi"}], [], allow_stream=False, provider=alt)
    assert s.state.context_factor == 1.0
    assert s.state.context_factor_for == ""
    await model.api_request([{"role": "user", "content": "hi"}], [], allow_stream=False, provider=s.config.provider)
    assert s.state.context_factor == pytest.approx(1.1, abs=1e-4)
