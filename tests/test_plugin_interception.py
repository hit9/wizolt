"""The interception core against real worker processes: chains, continuations and failure policy."""

import asyncio
from types import MappingProxyType

import pytest

from wizolt.plugins.hostcalls import Continuations
from wizolt.plugins.runtime import PluginRuntime
from wizolt.plugins.scope import InvocationScope
from wizolt.sdk import Context, Plugin, PluginError
from wizolt.sdk.operations import ModelRequest, ModelResponse, Refusal, ToolCall, ToolResult


@pytest.fixture
async def runtime():
    instance = PluginRuntime(lambda: Context("root", "main", "/tmp", "idle", 0, 0, "model", 0))
    yield instance
    await instance.close()


async def enable(runtime, tmp_path, name, body):
    path = tmp_path / f"{name}.py"
    path.write_text("from wizolt.sdk.operations import *\nSDK_VERSION = 1\n" + body)
    await runtime.manage("enable", str(path))
    return runtime.entries[name].active


def call(tool="Bash", **arguments):
    return ToolCall("call-1", tool, MappingProxyType(arguments or {"command": "ls"}))


class Core:
    """The host implementation under test: records what reached it."""

    def __init__(self, result=None, wait: asyncio.Event | None = None):
        self.seen = []
        self.result = result or ToolResult("core ran")
        self.wait = wait
        self.started = asyncio.Event()

    async def __call__(self, value):
        self.seen.append(value)
        self.started.set()
        if self.wait is not None:
            await self.wait.wait()
        return self.result


TAG = """
def setup(p):
    async def tag(ctx, value, next):
        marks = value.arguments.get("marks", "") + "{name}>"
        result = await next(value.replace(arguments={{**value.arguments, "marks": marks}}))
        return result.replace(content=result.content + "<{name}")
    p.intercept("tool.call", tag)
"""


async def test_empty_chain_calls_core_with_the_same_value(runtime):
    core, value = Core(), call()
    assert await runtime.interception.run("tool.call", value, core) is core.result
    assert core.seen[0] is value  # No copy, no encoding, no worker.


async def test_chain_nests_in_saved_order_and_wraps_results_in_reverse(runtime, tmp_path):
    await enable(runtime, tmp_path, "alpha", TAG.format(name="alpha"))
    await enable(runtime, tmp_path, "beta", TAG.format(name="beta"))
    core = Core()
    result = await runtime.interception.run("tool.call", call(), core)
    assert core.seen[0].arguments["marks"] == "alpha>beta>"
    assert result.content == "core ran<beta<alpha"
    runtime.interception_order.save(["beta"])
    result = await runtime.interception.run("tool.call", call(), core)
    assert core.seen[1].arguments["marks"] == "beta>alpha>" and result.content == "core ran<alpha<beta"


async def test_refusal_and_synthetic_results_skip_the_core(runtime, tmp_path):
    await enable(
        runtime,
        tmp_path,
        "policy",
        """
def setup(p):
    async def guard(ctx, value, next):
        if "rm" in value.arguments["command"]:
            return Refusal("no deletes")
        if value.arguments["command"] == "cached":
            return ToolResult("from cache")
        return await next(value)
    p.intercept("tool.call", guard)
""",
    )
    core = Core()
    assert await runtime.interception.run("tool.call", call(command="rm -rf x"), core) == Refusal("no deletes")
    assert await runtime.interception.run("tool.call", call(command="cached"), core) == ToolResult("from cache")
    assert not core.seen
    assert await runtime.interception.run("tool.call", call(command="ls"), core) == core.result


@pytest.mark.parametrize(
    "handler, reason",
    [
        ("await next(value); return await next(value)", "single-use"),
        ("await next(value); return Refusal('late')", "before next()"),
        ("return await next(ToolCall(value.id, 'Read', value.arguments))", "read-only"),
    ],
)
async def test_contract_violations_block_the_registration(runtime, tmp_path, handler, reason):
    await enable(runtime, tmp_path, "rogue", f"import asyncio\ndef setup(p):\n    async def h(ctx, value, next):\n        {handler}\n    p.intercept('tool.call', h)\n")
    with pytest.raises(PluginError, match=reason):
        await runtime.interception.run("tool.call", call(), Core())
    with pytest.raises(PluginError, match="blocked until it is reloaded or disabled"):
        await runtime.interception.run("tool.call", call(), Core())
    await runtime.manage("reload", "rogue")
    with pytest.raises(PluginError, match=reason):  # Reload restores the registration.
        await runtime.interception.run("tool.call", call(), Core())


async def test_downstream_work_cannot_outlive_its_handler(runtime, tmp_path):
    body = "import asyncio\ndef setup(p):\n    async def h(ctx, value, next):\n        asyncio.ensure_future(next(value))\n        await asyncio.sleep(0.1)\n        return ToolResult('detached')\n    p.intercept('tool.call', h)\n"
    await enable(runtime, tmp_path, "detach", body)
    core = Core(wait=asyncio.Event())
    with pytest.raises(PluginError, match="awaited inside the handler"):
        await runtime.interception.run("tool.call", call(), core)
    assert core.seen  # It had started; returning cancelled it rather than leaving it running.


async def test_handler_cannot_swallow_a_downstream_failure(runtime, tmp_path):
    await enable(
        runtime,
        tmp_path,
        "outer",
        """
def setup(p):
    async def h(ctx, value, next):
        try:
            return await next(value)
        except Exception:
            return ToolResult("pretend it worked")
    p.intercept("tool.call", h)
""",
    )
    await enable(runtime, tmp_path, "zinner", "def setup(p):\n    async def h(ctx, value, next):\n        raise RuntimeError('inner policy')\n    p.intercept('tool.call', h)\n")
    with pytest.raises(PluginError, match="zinner.*inner policy"):
        await runtime.interception.run("tool.call", call(), Core())
    assert not runtime.entries["outer"].active.failures
    assert "intercept:tool.call" in runtime.entries["zinner"].active.failures


async def test_an_out_of_bounds_host_value_fails_the_operation_without_blocking_the_plugin(runtime, tmp_path):
    # Python's JSON parser accepts NaN, so a model's tool arguments can carry one.
    generation = await enable(runtime, tmp_path, "relay", "def setup(p):\n    async def h(ctx, value, next):\n        return await next(value)\n    p.intercept('tool.call', h)\n")
    with pytest.raises(PluginError, match="Invalid ToolCall"):
        await runtime.interception.run("tool.call", call(command="x" * 10, nested=float("nan")), Core())
    # The host built a value outside the bounds; the plugin never saw it and keeps working.
    assert not generation.failures
    assert await runtime.interception.run("tool.call", call(), Core()) == ToolResult("core ran")


async def test_a_frame_the_host_cannot_send_fails_the_operation_without_blocking_the_plugin(runtime, tmp_path, monkeypatch):
    generation = await enable(runtime, tmp_path, "relay", "def setup(p):\n    async def h(ctx, value, next):\n        return await next(value)\n    p.intercept('tool.call', h)\n")
    monkeypatch.setattr("wizolt.plugins.process.MAX_REQUEST", 4096)  # Non-ASCII escapes 6x on the wire.
    with pytest.raises(PluginError, match="frame limit"):
        await runtime.interception.run("tool.call", call(command="猫" * 1000), Core())
    assert not generation.failures and not generation.worker.error
    assert await runtime.interception.run("tool.call", call(), Core()) == ToolResult("core ran")


async def test_an_outer_passthrough_relays_an_inner_refusal_without_being_blamed(runtime, tmp_path):
    outer = await enable(runtime, tmp_path, "aaa_outer", "def setup(p):\n    async def h(ctx, value, next):\n        return await next(value)\n    p.intercept('tool.call', h)\n")
    await enable(runtime, tmp_path, "zzz_inner", "def setup(p):\n    async def h(ctx, value, next):\n        return Refusal('not here')\n    p.intercept('tool.call', h)\n")
    core = Core()
    for _ in range(2):  # Still healthy the second time: nothing was blocked.
        assert await runtime.interception.run("tool.call", call(), core) == Refusal("not here")
    assert not outer.failures and not core.seen


async def test_disabling_an_unmatched_plugin_leaves_the_operation_running(runtime, tmp_path):
    relay = "def setup(p):\n    async def h(ctx, value, next):\n        return await next(value)\n    p.intercept('tool.call', h{})\n"
    await enable(runtime, tmp_path, "bashonly", relay.format(", match={'tool': 'Bash'}"))
    await enable(runtime, tmp_path, "everything", relay.format(""))
    core = Core(wait=asyncio.Event())
    operation = asyncio.create_task(runtime.interception.run("tool.call", call(tool="Read", path="x"), core))
    await asyncio.wait_for(core.started.wait(), 5)
    # bashonly never matched this Read: it is not pinned, and its disable cancels nothing.
    await runtime.manage("disable", "bashonly")
    assert "bashonly" not in runtime.entries and not operation.done()
    core.wait.set()
    assert await asyncio.wait_for(operation, 5) == ToolResult("core ran")


async def test_matcher_prefilters_without_invoking_the_worker(runtime, tmp_path):
    generation = await enable(runtime, tmp_path, "bashonly", "def setup(p):\n    async def h(ctx, value, next):\n        return ToolResult('bash')\n    p.intercept('tool.call', h, match={'tool': 'Bash'})\n")
    calls = generation.calls
    sequence = generation.worker.sequence
    core = Core()
    assert await runtime.interception.run("tool.call", call(tool="Read", path="x"), core) is core.result
    assert generation.worker.sequence == sequence and generation.calls == calls
    assert await runtime.interception.run("tool.call", call(), core) == ToolResult("bash")


async def test_preserve_mode_cannot_change_the_response(runtime, tmp_path):
    request = ModelRequest("r1", "turn", tools=("Bash",))
    response = ModelResponse("hello")
    body = "def setup(p):\n    async def h(ctx, value, next):\n        result = await next(value.replace(model='other'))\n        return {result}\n    p.intercept('model.request', h{mode})\n"
    await enable(runtime, tmp_path, "router", body.format(result="result", mode=""))
    core = Core(result=response)
    assert await runtime.interception.run("model.request", request, core) == response
    assert core.seen[0].model == "other"
    await runtime.manage("disable", "router")
    await enable(runtime, tmp_path, "rewriter", body.format(result="result.replace(text='changed')", mode=""))
    with pytest.raises(PluginError, match="preserve"):
        await runtime.interception.run("model.request", request, Core(result=response))
    await runtime.manage("disable", "rewriter")
    await enable(runtime, tmp_path, "replacer", body.format(result="result.replace(text='changed')", mode=", response='replace'"))
    assert runtime.interception.replaces("model.request", request)
    assert (await runtime.interception.run("model.request", request, Core(result=response))).text == "changed"


async def test_cancellation_during_next_cancels_core_and_releases_leases(runtime, tmp_path):
    generation = await enable(runtime, tmp_path, "wrap", "def setup(p):\n    async def h(ctx, value, next):\n        return await next(value)\n    p.intercept('tool.call', h)\n")
    core = Core(wait=asyncio.Event())
    task = asyncio.create_task(runtime.interception.run("tool.call", call(), core))
    await asyncio.wait_for(core.started.wait(), 5)
    assert generation.invocations == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert generation.invocations == 0
    assert not generation.worker.error and not generation.failures  # Cooperative cancel keeps the worker.
    assert await runtime.interception.run("tool.call", call(), Core()) == ToolResult("core ran")


async def test_next_pauses_the_handler_deadline_but_handler_work_does_not(runtime, tmp_path, monkeypatch):
    monkeypatch.setattr(type(runtime.interception), "HANDLER_SECONDS", 0.5)
    await enable(
        runtime,
        tmp_path,
        "timed",
        """
import asyncio
def setup(p):
    async def h(ctx, value, next):
        if value.arguments["command"] == "slow-handler":
            await asyncio.sleep(2)
        return await next(value)
    p.intercept("tool.call", h)
""",
    )

    class SlowCore(Core):
        async def __call__(self, value):
            await asyncio.sleep(1)  # Downstream time is not the handler's budget.
            return await super().__call__(value)

    assert await runtime.interception.run("tool.call", call(), SlowCore()) == ToolResult("core ran")
    with pytest.raises(PluginError, match="timed out"):
        await runtime.interception.run("tool.call", call(command="slow-handler"), Core())


async def test_auxiliary_requests_skip_active_registrations_but_core_descendants_do_not(runtime, tmp_path):
    await enable(runtime, tmp_path, "tagger", TAG.format(name="tagger"))
    core = Core()
    auxiliary = InvocationScope(active=frozenset({"tagger/tool.call"}), auxiliary=True)
    assert await runtime.interception.run("tool.call", call(), core, scope=auxiliary) is core.result
    seen = []

    async def nesting(value):
        # A core descendant, like a ToolScript's nested call, enters the full chain again.
        seen.append(await runtime.interception.run("tool.call", call(), core))
        return ToolResult("outer core")

    assert (await runtime.interception.run("tool.call", call(), nesting)).content == "outer core<tagger"
    assert seen[0].content == "core ran<tagger"


async def test_continuation_tokens_are_single_use_and_bound_to_their_request():
    sent = []
    continuations = Continuations(sent.append, lambda parent: parent == 7)

    async def resume(value):
        return {"type": "tool_result", "content": "ok", "status": "ok"}

    continuations.register("token", 7, resume)
    continuations.dispatch({"continue": "token", "parent": 8, "input": {}})  # Foreign request.
    continuations.dispatch({"continue": "token", "parent": 7, "input": {}})  # Already consumed.
    continuations.dispatch({"continue": "forged", "parent": 7, "input": {}})
    assert [item.get("error", "")[:16] for item in sent] == ["next() is single"] * 3
    continuations.register("fresh", 7, resume)
    continuations.dispatch({"continue": "fresh", "parent": 7, "input": {}})
    await asyncio.sleep(0.01)
    assert sent[-1] == {"continue_result": "fresh", "value": {"type": "tool_result", "content": "ok", "status": "ok"}}


async def test_interceptors_get_only_their_operations_host_services(runtime, tmp_path):
    await enable(
        runtime,
        tmp_path,
        "nosy",
        """
def setup(p):
    p.configure({"type": "object", "properties": {"x": {"type": "string"}}})
    async def h(ctx, value, next):
        await p.settings.update({"x": "y"})
        return await next(value)
    p.intercept("tool.call", h)
""",
    )
    with pytest.raises(PluginError, match="settings.read is not available to this operation"):
        await runtime.interception.run("tool.call", call(), Core())


def test_registration_contract_is_checked_at_setup():
    plugin = Plugin("sample")

    async def handler(context, value, next):
        return await next(value)

    with pytest.raises(PluginError, match="Unknown operation"):
        plugin.intercept("tool.run", handler)
    with pytest.raises(PluginError, match="matches only"):
        plugin.intercept("tool.call", handler, match={"command": "ls"})
    with pytest.raises(PluginError, match="only to model.request"):
        plugin.intercept("tool.call", handler, response="replace")
    plugin.intercept("tool.call", handler, match={"tool": ("Bash", "Read")})
    with pytest.raises(PluginError, match="Duplicate interceptor"):
        plugin.intercept("tool.call", handler)


async def test_failed_observer_skips_only_itself(runtime, tmp_path):
    generation = await enable(
        runtime,
        tmp_path,
        "mixed",
        "def setup(p):\n    async def boom(event):\n        raise RuntimeError('observer')\n    p.on('turn.started', boom)\n    p.field('value', lambda ctx: 1)\n",
    )
    await runtime.emit("turn.started")
    assert "observer:turn.started" in generation.failures
    assert runtime.fields() == {"plugins.mixed.value": 1}  # Presentation keeps working.


async def test_order_cli_saves_one_list_that_reload_applies(tmp_path, capsys):
    import json

    from agent_harness import session_with_provider

    from wizolt.plugins.catalog import Installation
    from wizolt.plugins.session import SessionPlugins
    from wizolt.ui.cli.plugin_commands import main

    runtime = SessionPlugins(session_with_provider(tmp_path))
    for name in ("alpha", "beta", "gamma"):
        (tmp_path / f"{name}.py").write_text("SDK_VERSION = 1\ndef setup(p):\n    pass\n")
        runtime.catalog.save(Installation(name, str(tmp_path / f"{name}.py")))
    config = str(runtime.catalog.preferences.path)

    def order(*arguments):
        code = main(["order", *arguments, "--config", config])
        return code, json.loads(capsys.readouterr().out)

    # Saved names first; the rest, including bundled plugins, by name.
    assert order("move", "gamma", "--before", "alpha")[1]["order"] == ["gamma", "alpha", "beta", "context_bar", "layout", "pet"]
    assert "[plugin_manager.interception]" in runtime.catalog.preferences.path.read_text()
    assert runtime.interception_order.names == ()  # A live agent changes only on reload.
    runtime.reload_layout()
    assert runtime.interception_order.ordered({"alpha", "beta", "gamma"}) == ["gamma", "alpha", "beta"]
    code, failed = order("move", "missing", "--after", "alpha")
    assert code == 1 and "Unknown installed plugin: missing" in failed["error"]
    assert order("reset")[1]["saved"] == []
    await runtime.close()
