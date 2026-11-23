"""compaction auto (split from tests/test_context.py)."""

import asyncio
import json

import pytest
from agent_harness import session, session_with_provider
from test_context import _CountingModel, _huge_history

from wizolt.agent.context import ContextManager


async def test_automatic_compaction_runs_once_until_new_messages_arrive(tmp_path):
    # The loop guard. Compaction shrinks history but cannot always get under budget, so "still over
    # budget" must not by itself justify compacting again: re-deciding on the same messages is the
    # runaway compaction this has regressed into before. One automatic pass per scope, until the
    # message list actually changes.
    s, context = _huge_history(tmp_path, steps=30)
    model = _CountingModel(s)

    for _ in range(5):
        prepared = await context.prepare_messages(model, "system")
        assert s.state.context_tokens == context.request_tokens(prepared)

    assert model.calls == 1
    assert s.state.compaction_count == 1

    # A new message is new information, so one more pass is allowed -- and again only one.
    # Large enough to go back over budget on its own: a pass now frees more than it used to, so a
    # smaller message no longer re-triggers one and the guard under test would never be exercised.
    s.messages.append({"role": "assistant", "content": "another step " + "y" * 700_000})
    for _ in range(5):
        await context.prepare_messages(model, "system")

    assert model.calls == 2


async def test_a_short_tail_of_large_messages_is_still_compactable(tmp_path):
    # The recent window is a message count, not a size. Once a session has been compacted, the only
    # compactable head is what sits before the latest user message -- and that is just the previous
    # summary, which is filtered out. So a handful of large messages after that user message left an
    # empty head, and every following request went out over budget without compacting anything.
    for steps in (2, 6, 8):
        s, context = _huge_history(tmp_path, steps=steps)
        budget = context.request_token_budget()
        before = context.request_tokens(context.model_messages("system"), None)
        model = _CountingModel(s)

        await context.prepare_messages(model, "system")

        after = context.request_tokens(context.model_messages("system"), None)
        if before < budget:
            assert model.calls == 0, f"{steps} steps fit; nothing should have been compacted"
        else:
            assert model.calls == 1, f"{steps} large messages after the user message were left uncompacted"
            assert after < budget, f"{steps} steps: still over budget after compacting"


async def test_a_lowered_threshold_compacts_history_below_the_budget(tmp_path):
    # The default line is the budget itself -- a request that fits compacts nothing. A lowered
    # [compaction].threshold moves the line under the budget for rate limits that count the full
    # prompt; the budget stays the hard line the current turn compacts at.
    s = session_with_provider(tmp_path)
    s.settings.max_context_tokens = 200_000
    s.messages = [
        {"role": "user", "content": "start"},
        {"role": "assistant", "content": "work " + "y" * 480_000},
        {"role": "user", "content": "continue"},
        {"role": "assistant", "content": "later"},
    ]
    context = ContextManager(s)
    assert context.request_tokens(context.model_messages("system"), None) < context.request_token_budget()

    model = _CountingModel(s)
    await context.prepare_messages(model, "system")
    assert model.calls == 0  # fits: the default threshold is the budget

    s.config.compaction_threshold = 0.5
    model = _CountingModel(s)
    await context.prepare_messages(model, "system")
    assert model.calls == 1  # about 120k of a 200k budget is over half: the history pass runs


async def test_over_budget_with_nothing_compactable_is_reported_once(tmp_path):
    # The irreducible case: the latest user message and one enormous tool result. The cut may not
    # land between a tool result and the call that produced it, so there is nothing to compact at
    # any window. Say so once rather than silently sending a request the provider will reject.
    s = session_with_provider(tmp_path)
    s.settings.max_context_tokens = 200_000
    s.messages = [
        {"role": "user", "content": "read the file"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "function": {"name": "Read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "x" * 1_000_000},
    ]
    context = ContextManager(s)
    reports = []
    context.hooks.on_compaction = lambda active, error: reports.append((active, error))
    model = _CountingModel(s)

    for _ in range(4):
        await context.prepare_messages(model, "system")

    assert model.calls == 0  # nothing could be compacted, so nothing was sent to the model
    assert len(reports) == 1, "the dead end must be reported, and only once"
    assert reports[0][0] is False
    assert "nothing is left to compact" in reports[0][1]

    # A new message buys one more attempt. It is no longer a dead end: with something after the
    # enormous result, the call and its result can be lifted out together, which the old cut --
    # confined to what follows the latest user message -- could never reach.
    s.messages.append({"role": "assistant", "content": "still working"})
    await context.prepare_messages(model, "system")
    assert reports[1:] == [(True, ""), (False, "")]
    assert all("read the file" != str(message.get("content") or "") or index == 1 for index, message in enumerate(s.messages))
    assert not any(message.get("tool_calls") for message in s.messages)  # the 1MB pair is gone
    assert model.calls == 1

    # And that pass actually fixed it: the request fits again, so a further message asks for no
    # further compaction. The dead end was reported once and then stopped being one.
    s.messages.append({"role": "user", "content": "carry on"})
    await context.prepare_messages(model, "system")
    assert model.calls == 1
    assert context.request_tokens(context.model_messages("system")) < context.request_token_budget()
    assert reports[-2:] == [(True, ""), (False, "")]


async def test_automatic_turn_compaction_runs_once_until_the_turn_grows(tmp_path):
    # Same guard for the current-turn pass, and it must not carry across turns: a fresh turn is a
    # different (shorter) list, and blocking it because the previous turn was longer would leave the
    # new one uncompactable.
    s, context = _huge_history(tmp_path, steps=2)
    model = _CountingModel(s)
    turn = [{"role": "user", "content": "request"}, *({"role": "assistant", "content": "t " + "y" * 160_000} for _ in range(30))]

    for _ in range(5):
        await context.prepare_messages(model, "system", turn)
    first = model.calls
    assert first >= 1

    for _ in range(5):
        await context.prepare_messages(model, "system", turn)
    assert model.calls == first  # nothing changed, nothing recompacted

    next_turn = [{"role": "user", "content": "next"}, *({"role": "assistant", "content": "n " + "y" * 160_000} for _ in range(30))]
    await context.prepare_messages(model, "system", next_turn)
    assert model.calls > first  # a new turn is not blocked by the previous turn's mark


async def test_prepare_messages_builds_under_budget_context_once(tmp_path, monkeypatch):
    context = ContextManager(session(tmp_path))
    calls = 0
    original = context.model_messages

    def model_messages(base_system, turn_messages=None):
        nonlocal calls
        calls += 1
        return original(base_system, turn_messages)

    monkeypatch.setattr(context, "model_messages", model_messages)
    await context.prepare_messages(object(), "system", [{"role": "user", "content": "request"}])

    assert calls == 1

@pytest.mark.parametrize(
    ("changed", "summary", "applied", "ready"),
    [
        pytest.param(False, "Earlier work is complete.", True, True, id="appended"),
        pytest.param(True, "Earlier work is complete.", False, True, id="edited"),
        pytest.param(False, "摘" * 9000, False, False, id="larger-utf8-summary"),
    ],
)
async def test_background_summary_commits_at_next_request_only(tmp_path, monkeypatch, changed, summary, applied, ready):
    from wizolt.model import ModelClient
    from wizolt.ui.cli.presentation import Presentation

    s, context = _huge_history(tmp_path, steps=16, chars=2048)
    model = _CountingModel(s)
    raw = context.request_tokens(context.model_messages("system"))
    monkeypatch.setattr(context, "auto_compaction_limit", lambda: int(raw / 0.9))
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    output = []
    ui = Presentation(s, output_fn=output.append)
    def notify(phase):
        ui.history_summary_status(phase)
        if phase in ("ready", "finished"):
            finished.set()
    context.hooks.on_history_summary = notify

    async def summarize(self, *args, **kwargs):
        started.set()
        await release.wait()
        return None, [], json.dumps({"summary": summary})

    monkeypatch.setattr(ModelClient, "api_request", summarize)
    await context.prepare_messages(model, "system")
    await asyncio.wait_for(started.wait(), 1)
    assert "compacting" in "".join(text for _, text in ui.status_bar.fragments())
    await context.prepare_messages(model, "system")
    assert s.state.compaction_count == 0
    if changed:
        s.messages[2]["content"] = "edited since preparation"
    appended = {"role": "user", "content": "A new follow-up"}
    s.messages.append(appended)
    release.set()
    await asyncio.wait_for(finished.wait(), 1)
    assert "compacting" not in "".join(text for _, text in ui.status_bar.fragments())
    assert ("compaction ready" in "".join(text for _, text in ui.status_bar.fragments())) == ready
    assert s.state.compaction_count == 0
    prepared = await context.prepare_messages(model, "system")
    assert appended in prepared
    assert s.state.compaction_count == int(applied)
    assert ("earlier messages compacted" in str(output)) == applied
    # Applied or dropped, the prepared-compaction notice never outlives the request that judged it.
    assert "compaction ready" not in "".join(text for _, text in ui.status_bar.fragments())
    if changed:
        assert s.messages[2]["content"] == "edited since preparation"
    await context.background_history.cancel()


@pytest.mark.parametrize("during_close", [False, True])
async def test_cancel_clears_the_prepared_compaction_notice(tmp_path, monkeypatch, during_close):
    from wizolt.model import ModelClient
    from wizolt.ui.cli.presentation import Presentation

    s, context = _huge_history(tmp_path, steps=16, chars=2048)
    model = _CountingModel(s)
    raw = context.request_tokens(context.model_messages("system"))
    monkeypatch.setattr(context, "auto_compaction_limit", lambda: int(raw / 0.9))
    output = []
    ui = Presentation(s, output_fn=output.append)
    phases = []
    ready = asyncio.Event()

    def notify(phase):
        phases.append(phase)
        ui.history_summary_status(phase)
        if phase == "ready":
            ready.set()

    context.hooks.on_history_summary = notify

    async def summarize(self, *args, **kwargs):
        return None, [], json.dumps({"summary": "Earlier work is complete."})

    closing = asyncio.Event()
    if during_close:
        async def close(self):
            closing.set()
            await asyncio.Event().wait()
        monkeypatch.setattr(ModelClient, "close", close)
    monkeypatch.setattr(ModelClient, "api_request", summarize)
    await context.prepare_messages(model, "system")
    await asyncio.wait_for((closing if during_close else ready).wait(), 1)
    assert ("compaction ready" in "".join(text for _, text in ui.status_bar.fragments())) == (not during_close)
    await context.background_history.cancel()
    assert "compaction ready" not in "".join(text for _, text in ui.status_bar.fragments())
    assert phases == (["running", "finished"] if during_close else ["running", "ready", "finished"])


async def test_foreground_fallback_cancels_pending_summary(tmp_path, monkeypatch):
    from wizolt.model import ModelClient

    s, context = _huge_history(tmp_path, steps=16, chars=2048)
    raw = context.request_tokens(context.model_messages("system"))
    limit = int(raw / 0.9)
    monkeypatch.setattr(context, "auto_compaction_limit", lambda: limit)
    started, cancelled = asyncio.Event(), asyncio.Event()
    phases = []
    context.hooks.on_history_summary = phases.append

    async def slow_request(self, *args, **kwargs):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(ModelClient, "api_request", slow_request)
    model = _CountingModel(s)
    await context.prepare_messages(model, "system")
    await asyncio.wait_for(started.wait(), 1)
    limit = raw
    await context.prepare_messages(model, "system")
    assert cancelled.is_set()
    assert s.state.compaction_count == model.calls == 1
    assert phases == ["running", "finished"]


async def test_provider_change_before_background_summary_starts_discards_prepared_request(tmp_path, monkeypatch):
    from wizolt.model import ModelClient

    s, context = _huge_history(tmp_path, steps=16, chars=2048)
    model = _CountingModel(s)
    raw = context.request_tokens(context.model_messages("system"))
    monkeypatch.setattr(context, "auto_compaction_limit", lambda: int(raw / 0.9))
    calls = []

    async def summarize(self, *args, provider, **kwargs):
        calls.append(provider.url)
        return None, [], '{"summary": "Earlier work is complete."}'

    monkeypatch.setattr(ModelClient, "api_request", summarize)
    await context.prepare_messages(model, "system")
    s.config.provider.url = "https://new-provider.example/v1"
    await asyncio.sleep(0)  # The old request has been planned, but its task had not run yet.
    assert not calls
    await context.prepare_messages(model, "system")
    await asyncio.sleep(0)
    assert calls == [s.config.provider.url]
    await context.background_history.cancel()
