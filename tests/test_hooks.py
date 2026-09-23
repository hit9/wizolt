"""The presentation seam: one UiHooks object per agent, shared with every layer it owns.

These are the invariants the consolidation rests on. Each of them was, at some point in the
refactor, false in a way no other test noticed: an agent whose layers had drifted onto different
hooks objects still passed the suite, because the wiring tests read whichever object they were
given.
"""

import dataclasses
import pathlib
import re

from test_session_persistence import session_with_data_dir

from wizolt import engine as engine_module
from wizolt.engine import Agent
from wizolt.hooks import UiHooks

WIZOLT = pathlib.Path(__file__).resolve().parent.parent / "wizolt"


def _agent(tmp_path):
    return Agent(session_with_data_dir(tmp_path), output_fn=lambda _text: None)


def test_an_agent_shares_one_hooks_object_with_its_layers(tmp_path):
    agent = _agent(tmp_path)
    assert agent.model.hooks is agent.hooks
    assert agent.context.hooks is agent.hooks
    assert agent.tools.hooks is agent.hooks


def test_the_layers_own_no_hook_attribute_of_their_own(tmp_path):
    """The old per-layer attributes are gone, so a stale writer cannot quietly do nothing.

    It cannot raise either -- these are ordinary objects -- which is exactly why the seam has to
    be the only channel the tests use.
    """
    agent = _agent(tmp_path)
    for layer in (agent, agent.model, agent.context, agent.tools):
        for name in ("on_stream", "on_builtin_call", "on_retry_wait", "on_compaction", "on_tool_batch"):
            assert not hasattr(layer, name), (layer, name)
    # writing through the model still reaches the agent: one object, not two
    received = []
    agent.model.hooks.on_stream = lambda kind, text: received.append((kind, text))
    agent.start_textual_tool_correction([], "Read")
    assert received == [(f"correcting malformed tool call 1/{engine_module.MAX_TEXTUAL_TOOL_CORRECTIONS} · Read", "")]


def test_use_hooks_reshares_every_layer_and_leaks_nothing(tmp_path):
    agent = _agent(tmp_path)
    agent.hooks.on_tool_batch = lambda _silent: None
    agent.hooks.question_fn = lambda _specs: None

    fresh = UiHooks(on_stream=lambda _kind, _text: None)
    agent.use_hooks(fresh)

    assert agent.model.hooks is fresh and agent.context.hooks is fresh and agent.tools.hooks is fresh
    assert agent.hooks is fresh
    # a rebind starts from the new object's fields, not the previous object's
    assert fresh.on_tool_batch is None and fresh.question_fn is None


def test_a_headless_agent_reports_nothing(tmp_path):
    """No presenter wired means every hook is absent, which each layer reads as its own default."""
    assert all(getattr(UiHooks(), f.name) is None for f in dataclasses.fields(UiHooks))
    assert all(getattr(_agent(tmp_path).hooks, f.name) is None for f in dataclasses.fields(UiHooks))


def test_every_hook_field_has_a_reader_in_production():
    """A field nothing reads is a dead seam: it would be wired, and silently ignored.

    tests/ is excluded on purpose -- a field kept alive only by tests is the same dead weight.
    """
    read = set()
    for path in WIZOLT.rglob("*.py"):
        if path.name == "hooks.py":
            continue
        read |= set(re.findall(r"\.hooks\.(\w+)", path.read_text()))
    declared = {f.name for f in dataclasses.fields(UiHooks)}
    assert declared - read == set(), f"declared but never read in production: {sorted(declared - read)}"


def test_no_hook_is_read_by_name_off_a_layer():
    """A `getattr(layer, "<hook>", None)` read is invisible to the checker and to the test above.

    ToolScript read `getattr(runner, "script_status", None)` after the field moved into the runner's
    hooks: always None, so a running script never reached the divider, and nothing failed.
    """
    names = "|".join(f.name for f in dataclasses.fields(UiHooks))
    pattern = re.compile(rf"""(?:getattr|hasattr|setattr)\([^)]*["']({names})["']""")
    found = [f"{path.relative_to(WIZOLT)}: {match.group(1)}" for path in WIZOLT.rglob("*.py") for match in pattern.finditer(path.read_text())]
    assert found == []
