"""Plugin mentions are pointers, never activation or eager capability expansion."""

import json

from agent_harness import session_with_provider
from prompt_toolkit.document import Document

from wizolt.agent.engine import Agent
from wizolt.base import SESSION_EVENT_KEY
from wizolt.mentions import scan_mentions
from wizolt.plugins.catalog import Installation
from wizolt.plugins.mentions import resolve_mentions
from wizolt.tools.plugin import PluginTool
from wizolt.ui.cli.loop import CommandLoop
from wizolt.ui.cli.view import CommandCompleter


def test_plugin_namespace_is_explicit_and_never_a_file_skill_or_mcp_alias():
    assert [(s.kind, s.payload) for s in scan_mentions("@plugin:layout, @layout user@plugin:pet")] == [("plugin", "layout"), ("bare", "layout")]
    assert resolve_mentions("@layout @skill:layout @mcp:layout @plugin:") == ""
    text = resolve_mentions("@plugin:layout @plugin:layout @plugin:unknown")
    assert text.count("[layout]") == 1 and "[unknown]" in text
    assert len(resolve_mentions(" ".join(f"@plugin:p{i}" for i in range(100)))) < 9000
    assert "omitted" in resolve_mentions("@plugin:" + "a" * 10000)


async def test_mention_and_completion_do_not_execute_even_broken_disabled_source(tmp_path):
    session = session_with_provider(tmp_path)
    source = tmp_path / "danger.py"
    marker = tmp_path / "executed"
    source.write_text(f'from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError("DO-NOT-INLINE")\n')
    session.plugins.catalog.save(Installation("danger", str(source), enabled=False))
    agent = Agent(session, output_fn=lambda _: None)
    loop = CommandLoop(agent, input_fn=lambda _: "", output_fn=lambda _: None)
    choices = list(loop.input_completer.get_completions(Document("use @plugin:"), None))
    assert {c.text for c in choices} == {"@plugin:danger", "@plugin:context_bar", "@plugin:layout", "@plugin:pet"}
    [block] = await agent.mention_messages("use @plugin:danger")
    assert block[SESSION_EVENT_KEY] == "plugin_mentions"
    assert "[danger]" in block["content"] and "DO-NOT-INLINE" not in block["content"]
    assert str(source) not in block["content"]
    assert not marker.exists() and not session.plugins.entries
    assert not session.plugins.catalog.read()[0]["danger"].enabled
    await session.plugins.close()
    session.close()


def test_plugin_completion_is_namespaced_and_complete_reference_does_not_trap_enter():
    completer = CommandCompleter(plugins=lambda: ("layout", "pet"))
    values = lambda text: [c.text for c in completer.get_completions(Document(text), None)]
    assert values("@pl") == ["@plugin:"]
    assert values("@plugin:la") == ["@plugin:layout"]
    assert values("@plugin:layout") == []
    assert values("@layout") == []  # No implicit alias can collide with skills or MCP.


async def test_plugin_list_exact_filter_and_mention_do_not_inline_tool_details(tmp_path):
    session = session_with_provider(tmp_path)
    path = tmp_path / "lookup.py"
    path.write_text('''SDK_VERSION = 1
def setup(p):
    async def run(ctx, args):
        return "unused"
    p.tool("operation", "SECRET_DESCRIPTION", {"type": "object"}, run)
''')
    try:
        await session.plugins.manage("enable", str(path))
        await session.plugins.manage("enable", "layout")
        tool = PluginTool(session, [{"action": "list", "name": "lookup"}])
        assert not tool.needs_confirmation()
        listed = json.loads(await tool.call())
        assert listed == {"tools": [{"name": "lookup", "tool": "operation", "description": "SECRET_DESCRIPTION"}]}
        assert json.loads(await PluginTool(session, [{"action": "list", "name": "unknown"}]).call()) == {"tools": []}
        [block] = await Agent(session, output_fn=lambda _: None).mention_messages("@plugin:lookup")
        assert "SECRET_DESCRIPTION" not in block["content"] and "operation" not in block["content"]
        await session.plugins.entries["lookup"].active.worker.close()
        assert json.loads(await tool.call()) == {"tools": []}, "a dead worker is not a usable tool"
    finally:
        await session.plugins.close()


def test_tui_plugin_candidate_selection_preserves_message(monkeypatch):
    from tui_harness import run_interactive_tui, wait_until

    from wizolt.ui.tui import TuiApp

    app = TuiApp(completer=CommandCompleter(plugins=lambda: ("layout", "pet")))

    def drive(pipe):
        wait_until(lambda: app.app is not None and app.app.is_running)
        pipe.send_text("use @plugin:la")
        wait_until(lambda: app.input_buffer.complete_state is not None)
        pipe.send_text("\r")
        wait_until(lambda: app.input_buffer.text == "use @plugin:layout" and app.input_buffer.complete_state is None)
        pipe.send_text(" please")
        wait_until(lambda: app.input_buffer.text == "use @plugin:layout please")
        app.app.loop.call_soon_threadsafe(app.app.exit)

    run_interactive_tui(monkeypatch, app, drive=drive)
