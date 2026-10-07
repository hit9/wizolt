"""Real worker RPC owns interactive waits; only terminal rendering is replaced here."""

import asyncio
from dataclasses import asdict

import pytest

from wizolt.plugins.runtime import PluginRuntime
from wizolt.sdk import Context, Line, PluginError, Text
from wizolt.sdk.views import Action, Choice, Document, Field, Form, Section, Selection, View


def interactive_plugin(tmp_path):
    path = tmp_path / "interactive.py"
    path.write_text('''SDK_VERSION = 1
def setup(plugin):
    async def ask(ctx, args):
        answer = await plugin.ui.input("Name", default="review")
        return "cancelled" if answer is None else answer
    plugin.command("ask-name", "Ask a name", ask)
''')
    return str(path)


def runtime_for(tmp_path):
    return PluginRuntime(lambda: Context("s", "main", str(tmp_path), "idle", 0, 0, "test", 0))


class Terminal:
    """Stand in for a person who may take longer than a plugin's execution budget."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.exited = asyncio.Event()
        self.answer = asyncio.Event()
        self.owner = ""

    async def call(self, owner, service, arguments):
        if service != "ui.views.show":
            return {}
        View.decode(arguments["view"])
        self.owner = owner
        self.entered.set()
        try:
            await self.answer.wait()
            return {"result": {"action": "submit", "values": {"value": "Alice"}}}
        finally:
            self.exited.set()


async def test_human_wait_pauses_action_deadline(tmp_path):
    runtime = runtime_for(tmp_path)
    terminal = Terminal()
    runtime.interactions.handler = terminal.call
    try:
        await runtime.manage("enable", interactive_plugin(tmp_path))
        runtime.ACTION_TIMEOUT = 0.2
        action = asyncio.create_task(runtime.invoke("interactive", "command", "ask-name", {}))
        await asyncio.wait_for(terminal.entered.wait(), 3)
        await asyncio.sleep(0.3)
        assert not action.done()
        terminal.answer.set()
        assert await asyncio.wait_for(action, 3) == "Alice"
        assert terminal.owner == "interactive"
    finally:
        await runtime.close()


@pytest.mark.parametrize("management", ["disable", "reload"])
async def test_retirement_dismisses_interaction_and_unpins_generation(tmp_path, management):
    runtime = runtime_for(tmp_path)
    terminal = Terminal()
    runtime.interactions.handler = terminal.call
    try:
        await runtime.manage("enable", interactive_plugin(tmp_path))
        action = asyncio.create_task(runtime.invoke("interactive", "command", "ask-name", {}))
        await asyncio.wait_for(terminal.entered.wait(), 3)
        await runtime.manage(management, "interactive")
        assert await asyncio.wait_for(action, 3) == "cancelled"
        assert terminal.exited.is_set()
        assert not runtime.interactions.pending
        assert ("interactive" in runtime.entries) == (management == "reload")
    finally:
        await runtime.close()


async def test_cancelled_action_closes_host_interaction(tmp_path):
    runtime = runtime_for(tmp_path)
    terminal = Terminal()
    runtime.interactions.handler = terminal.call
    try:
        await runtime.manage("enable", interactive_plugin(tmp_path))
        action = asyncio.create_task(runtime.invoke("interactive", "command", "ask-name", {}))
        await asyncio.wait_for(terminal.entered.wait(), 3)
        action.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(action, 3)
        assert terminal.exited.is_set()
        assert not runtime.interactions.pending
    finally:
        await runtime.close()


@pytest.mark.parametrize("management", ["disable", "reload"])
async def test_retiring_call_cannot_reopen_a_dismissed_view(tmp_path, management):
    path = tmp_path / "interactive.py"
    path.write_text('''SDK_VERSION = 1
def setup(plugin):
    async def ask(ctx, args):
        await plugin.ui.input("First")
        return await plugin.ui.input("Second")
    plugin.command("ask", "Ask twice", ask)
''')
    runtime = runtime_for(tmp_path)
    terminal = Terminal()
    runtime.interactions.handler = terminal.call
    try:
        await runtime.manage("enable", str(path))
        action = asyncio.create_task(runtime.invoke("interactive", "command", "ask", {}))
        await asyncio.wait_for(terminal.entered.wait(), 3)
        await runtime.manage(management, "interactive")
        with pytest.raises(PluginError, match="being replaced or disabled"):
            await asyncio.wait_for(action, 3)
        assert not runtime.interactions.pending
        result = (await runtime.manage("list"))["plugins"]
        assert [item["status"] for item in result] == (["running"] if management == "reload" else [])
        if management == "reload":
            terminal.answer.set()
            assert await runtime.invoke("interactive", "command", "ask", {}) == "Alice"
    finally:
        await runtime.close()


async def test_headless_interaction_reports_unavailable(tmp_path):
    runtime = runtime_for(tmp_path)
    try:
        await runtime.manage("enable", interactive_plugin(tmp_path))
        with pytest.raises(PluginError, match="running TUI"):
            await runtime.invoke("interactive", "command", "ask-name", {})
    finally:
        await runtime.close()


async def test_blocked_worker_loses_its_deadline_exemption_when_view_closes(tmp_path):
    path = tmp_path / "blocked.py"
    path.write_text('''import time
from wizolt.sdk.views import Document, View
SDK_VERSION = 1
def setup(p):
    async def block(ctx, args):
        async with p.ui.open(View("Stuck plugin", Document("Esc still belongs to the host"))):
            time.sleep(30)
        return "unreachable"
    p.command("block", "Block this worker", block)
''')
    runtime = runtime_for(tmp_path)
    terminal = Terminal()
    runtime.interactions.handler = terminal.call
    try:
        await runtime.manage("enable", str(path))
        runtime.ACTION_TIMEOUT = .2
        action = asyncio.create_task(runtime.invoke("blocked", "command", "block", {}))
        await asyncio.wait_for(terminal.entered.wait(), 3)
        terminal.answer.set()
        with pytest.raises(PluginError, match="timed out"):
            await asyncio.wait_for(action, 5)
        assert terminal.exited.is_set()
        assert not runtime.interactions.pending
    finally:
        await runtime.close()


@pytest.mark.parametrize("body", [Document("hello", "markdown"), Document("hello", sections=(Section("added by x", "more"),)), Selection((Choice("a", "First", "Preview"),)), Form((Field("name", "Name"),))])
def test_view_wire_round_trip(body):
    view = View("Title", body, (Action("open", "Open", "o"),), fullscreen=True)
    assert View.decode(asdict(view)) == view


@pytest.mark.parametrize("view", [
    View("bad\x1b", Document("text")),
    View("bad", Document("x" * 256_001)),
    View("bad", Document("x" * 200_000, sections=(Section("more", "x" * 100_000),))),  # The limit counts sections.
    View("bad", Document("text", sections=(Section("two\nlines", "more"),))),
    View("bad", Selection((Choice("same", "One"), Choice("same", "Two")))),
    View("bad", Selection((Choice("one", "One"),), ("missing",))),
    View("bad", Form(())),
    View("bad", Document("text"), (Action("one", "One", "x"), Action("two", "Two", "x"))),
])
def test_invalid_view_is_rejected_before_rendering(view):
    with pytest.raises(PluginError):
        View.decode(asdict(view))


def reporting_plugin(tmp_path):
    path = tmp_path / "reporting.py"
    path.write_text('''SDK_VERSION = 1
from wizolt.sdk import Line, Text

def setup(plugin):
    async def go(ctx, args):
        await plugin.ui.report([Line((Text("half", "warning"),))])
        return [Line((Text("done", "success"),))]
    plugin.command("go", "Report rows as they arrive", go)
''')
    return str(path)


class RecordingTerminal:
    """Answer every service and record what crossed the wire."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    async def call(self, owner: str, service: str, arguments: dict) -> dict:
        self.calls.append((owner, service, arguments))
        return {}


async def test_a_reported_block_streams_through_the_real_worker(tmp_path):
    runtime = runtime_for(tmp_path)
    terminal = RecordingTerminal()
    runtime.interactions.handler = terminal.call
    try:
        await runtime.manage("enable", reporting_plugin(tmp_path))
        answer = await runtime.invoke("reporting", "command", "go", {})
        assert answer == [Line((Text("done", "success"),))]
        [(owner, service, arguments)] = terminal.calls
        assert (owner, service) == ("reporting", "ui.report")
        assert arguments == {"lines": [{"spans": [{"text": "half", "role": "warning"}]}]}
    finally:
        await runtime.close()


async def test_reporting_requires_interactive_ui(tmp_path):
    runtime = runtime_for(tmp_path)
    try:
        await runtime.manage("enable", reporting_plugin(tmp_path))
        with pytest.raises(PluginError, match="Interactive UI"):
            await runtime.invoke("reporting", "command", "go", {})
    finally:
        await runtime.close()


def streaming_plugin(tmp_path):
    path = tmp_path / "streaming.py"
    path.write_text('''SDK_VERSION = 1
import asyncio
from wizolt.sdk import Line, Text

def setup(plugin):
    async def go(ctx, args):
        await plugin.ui.report([Line((Text("first", "warning"),))])
        await asyncio.sleep(1.5)
        await plugin.ui.report([Line((Text("second", "warning"),))])
        return "done"
    plugin.command("go", "Report rows as they arrive", go)
''')
    return str(path)


async def test_a_report_arrives_while_the_action_is_still_running(tmp_path):
    # A frame crosses the pipe before the command answers: what makes progressive frames
    # on screen, rather than every block surfacing with the final answer.
    runtime = runtime_for(tmp_path)
    terminal = RecordingTerminal()
    seen: list[float] = []

    async def call(owner: str, service: str, arguments: dict) -> dict:
        seen.append(asyncio.get_running_loop().time())
        return await terminal.call(owner, service, arguments)

    runtime.interactions.handler = call
    try:
        await runtime.manage("enable", streaming_plugin(tmp_path))
        started = asyncio.get_running_loop().time()
        answer = await runtime.invoke("streaming", "command", "go", {})
        finished = asyncio.get_running_loop().time()
        assert answer == "done"
        assert [service for _owner, service, _arguments in terminal.calls] == ["ui.report", "ui.report"]
        first, second = seen
        # Each frame lands while the action is still in flight, and in arrival order:
        # the first before the second, the second before the answer.
        assert started < first < started + 1.0
        assert first + 1.0 < second < finished
    finally:
        await runtime.close()
