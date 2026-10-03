"""Shared real-terminal scenarios; imported by each multiplexer acceptance module."""

import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import pytest
from prompt_toolkit.utils import get_cwidth

WIDE, NARROW = 100, 62
TALL, SHORT = 30, 18
CYCLES = 30
SEED_LINES = 20
MARKERS = 200
DRIVER = Path(__file__).with_name("tmux_driver.py")


def test_transcript_survives_resize_roundtrip_between_frames(pane):
    """SIGWINCH can coalesce while the app is descheduled; equal final sizes prove nothing."""
    log = pane.path / "roundtrip.log"
    log.with_suffix(".pause").touch()
    pane.send(f"{sys.executable} {DRIVER} 0 0 {log} command-stream")
    deadline = time.monotonic() + 20
    while not log.exists() or "frames paused" not in log.read_text():
        assert time.monotonic() < deadline, pane.visible()
        time.sleep(0.01)
    try:
        pane.resize(WIDE, 14)
        _settled_capture(pane)
        pane.resize(WIDE, TALL)
        _settled_capture(pane)
    finally:
        log.with_suffix(".resume").touch()
    while "RESULT-23" not in "\n".join(pane.capture()):
        assert time.monotonic() < deadline, pane.visible()
        time.sleep(0.05)
    text = "\n".join(_settled_capture(pane))
    for index in range(24):
        assert len(re.findall(rf"COMMAND-{index}\b", text)) == 1, text
        assert len(re.findall(rf"RESULT-{index}\b", text)) == 1, text


def test_builtin_plugin_can_be_enabled_resized_and_disabled(pane):
    """Manage a real packaged plugin while the input and renderer remain host-owned."""
    config = pane.path / "plugins.toml"
    config.write_text(
        f'[paths]\ndata_dir = "{pane.path}/data"\n'
        '[provider]\nactive = "test"\n[provider.test]\n'
        'url = "http://127.0.0.1:9/v1"\nkey = "test"\nmodel = "test-model"\n'
    )
    entry = pane.path / "plugins.py"
    entry.write_text(
        "from wizolt.ui.cli.update import UpdateChecker\n"
        "from wizolt.providers.sync import CatalogRuntime\n"
        "from wizolt.__main__ import main\n"
        "UpdateChecker.load_cached = lambda self: False\n"
        "CatalogRuntime.refresh_due = lambda self: False\n"
        "main()\n"
    )
    pane.send(f"{sys.executable} {entry} --config {config} --yolo")

    def wait(text):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            visible = pane.visible()
            assert "Unhandled exception" not in visible, visible
            if text in visible:
                return visible
            time.sleep(.05)
        raise AssertionError(f"missing {text!r}: {visible}")

    wait("test-model")
    pane.send("/plugins")
    wait("disabled")
    pane.keys("Enter")
    wait("1. enable")
    pane.keys("Enter")
    wait("pet  enabled")
    pane.keys("Escape")
    wait("on standby")
    for width, height in ((40, 18), (100, 30), (60, 20)):
        pane.resize(width, height)
        wait("on standby")
    pane.send("/plugins")
    wait("Plugins")
    pane.keys("Enter")
    wait("1. reload")
    pane.keys("Down")
    wait("2. disable")
    pane.keys("Enter")
    wait("pet  disabled")
    pane.keys("Escape")
    pane.send("/plugins")
    visible = wait("Plugins")
    assert "on standby" not in visible
    pane.keys("Escape", "C-d")


def test_plugin_appearance_reload_preserves_input_and_updates_bars(pane):
    config = pane.path / "appearance-plugin.toml"
    config.write_text(
        f'[paths]\ndata_dir = "{pane.path}/data"\n'
        '[provider]\nactive = "test"\n[provider.test]\n'
        'url = "http://127.0.0.1:9/v1"\nkey = "test"\nmodel = "test-model"\n'
        '[ui.statusbar]\nformat = "preset:plugins.look.compact"\n'
    )
    source = pane.path / "look.py"
    source.write_text('SDK_VERSION = 1\ndef setup(p):\n    p.preset("statusbar", "compact", "PLUGIN-LOOK {model}")\n')
    subprocess.run(
        [sys.executable, "-m", "wizolt", "plugin", "enable", str(source), "--config", str(config), "--project", str(pane.path)],
        check=True, capture_output=True, text=True,
    )
    pane.send(f"{sys.executable} -m wizolt --config {config} --yolo")

    def wait(text):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            visible = pane.visible()
            assert "Unhandled exception" not in visible, visible
            if text in visible:
                return visible
            time.sleep(.05)
        raise AssertionError(f"missing {text!r}: {visible}")

    wait("PLUGIN-LOOK test-model")
    pane.resize(50, 20)
    wait("PLUGIN-LOOK test-model")
    source.write_text('SDK_VERSION = 1\ndef setup(p):\n    p.preset("statusbar", "compact", "RELOADED-LOOK {model}")\n')
    pane.send("/plugins reload look")
    wait("RELOADED-LOOK test-model")
    pane.send("/theme")
    wait("h/l tab")
    pane.keys("Escape")
    pane.send("/plugins disable look")
    wait('"disabled"')
    pane.send("/plugins enable look")
    wait("RELOADED-LOOK test-model")
    pane.keys("C-d")


def test_detail_sheets_stay_navigable_across_resize(pane):
    log = pane.path / "details.log"
    # Shell-exported dimensions can stay frozen while the PTY is resized.
    pane.send(f"LINES=24 COLUMNS=80 {sys.executable} {DRIVER} 0 0 {log} details")

    def visible_with(text):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            visible = pane.visible()
            if text in visible:
                return visible
            time.sleep(0.05)
        raise AssertionError(f"missing {text!r}: {visible}")

    for title in ("Command", "Log"):
        visible_with(title + " · read-only")
        for index in range(15):
            pane.resize(NARROW if index % 2 else WIDE, SHORT if index % 2 else TALL)
            _settled_capture(pane)
            pane.keys("G")
            visible = visible_with("DETAIL-TAIL")
            assert "Ctrl-O" in visible and "tmux-driver" in visible
            pane.keys("g")
            visible_with("BODY-000")
        pane.keys("Escape")
    deadline = time.monotonic() + 10
    while "closed log" not in log.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    pane.keys("draft-still-editable")
    visible_with("draft-still-editable")


def test_fresh_window_output_uses_available_screen_before_scrolling(pane):
    # A fresh pane starts at the top, unlike a shell with a screenful of prior output.
    # Reaching native history alone is not enough: recent output must remain visible too.
    pane.fresh_window()
    log = pane.path / "fresh.log"
    deadline = time.monotonic() + 5
    while "FRESH-READY>" not in pane.visible().splitlines():
        assert time.monotonic() < deadline, "the new window's shell prompt did not appear"
        time.sleep(0.02)
    pane.send("echo FRESH-SHELL-CONTEXT")
    deadline = time.monotonic() + 5
    while "FRESH-SHELL-CONTEXT" not in pane.visible().splitlines():
        assert time.monotonic() < deadline, "the new window's shell did not finish starting"
        time.sleep(0.02)
    pane.send(f"{sys.executable} {DRIVER} 40 0.02 {log} fresh")
    _wait_for_markers(log, 4)
    _settled_capture(pane)
    visible = pane.visible()
    assert "FRESH-SHELL-CONTEXT" in visible, visible
    for marker in range(1, 5):
        assert f"MARKER-{marker:04d}" in visible

    log.with_suffix(".more").touch()
    _wait_for_markers(log, 40)
    for width, height in [(WIDE, TALL), (NARROW, SHORT), (WIDE, TALL)]:
        pane.resize(width, height)
        lines = _settled_capture(pane)
        seen = Counter(int(m) for m in re.findall(r"MARKER-(\d+)", "\n".join(lines)))
        assert seen == Counter(range(1, 41))
        visible = pane.visible()
        assert "MARKER-0040" in visible
        assert len(re.findall(r"MARKER-\d+", visible)) >= 5
        assert sum(line == "+>" or line.startswith("+> ") for line in lines) == 1
        assert "\n".join(lines).count("tmux-driver") == 1


@pytest.mark.parametrize("height", [12, 18, 30])
def test_long_inline_selector_keeps_context_visible(pane, height):
    pane.resize(WIDE, height)
    log = pane.path / "choices.log"
    pane.send("echo SHELL-CONTEXT")
    pane.send(f"{sys.executable} {DRIVER} 0 0 {log} choices")

    def visible_containing(needle):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            visible = pane.visible()
            if needle in visible:
                return visible
            time.sleep(0.05)
        raise AssertionError(f"missing {needle!r} in visible pane:\n{visible}")

    visible_containing("WIZOLT-BANNER")
    for cycle in range(3):
        log.with_suffix(f".open-{cycle}").touch()
        visible_containing("provider-00")
        pane.keys("G")
        opened = visible_containing("provider-79")
        assert "WIZOLT-BANNER" in opened, opened
        banner_row = opened.splitlines().index("WIZOLT-BANNER")
        if cycle == 1:
            pane.keys("/", "provider-42")
            visible_containing("provider-42")
            pane.keys("Enter", "Enter")
        else:
            pane.keys("Escape")
        deadline = time.monotonic() + 15
        while f"closed {cycle}:" not in log.read_text():
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.05)
        _settled_capture(pane)
        visible = visible_containing("tmux-driver")
        assert "WIZOLT-BANNER" in visible
        assert visible.splitlines().index("WIZOLT-BANNER") == banner_row, "closing the selector scrolled the context"
        assert "PROVIDER-COMMAND" in visible
        history = "\n".join(pane.capture())
        assert history.count("WIZOLT-BANNER") == 1
        assert history.count("PROVIDER-COMMAND") == 1
        assert "SHELL-CONTEXT" in history
    assert "closed 1: provider-42" in log.read_text()


def test_cli_startup_banner_survives_growing_and_shrinking_pane(pane):
    config = pane.path / "config.toml"
    config.write_text(
        f'[paths]\ndata_dir = "{pane.path}/data"\n'
        '[provider]\nactive = "test"\n[provider.test]\n'
        'url = "http://127.0.0.1:9/v1"\nkey = "test-only"\nmodel = "tmux-startup-model"\n'
    )
    # Run the real CLI entry and startup handoff. Only background network checks are disabled;
    # no model request is made, and the session/config live entirely inside the temporary pane.
    entry = pane.path / "startup.py"
    entry.write_text(
        "from wizolt.ui.cli.update import UpdateChecker\n"
        "from wizolt.providers.sync import CatalogRuntime\n"
        "from wizolt.__main__ import main\n"
        "UpdateChecker.load_cached = lambda self: False\n"
        "CatalogRuntime.refresh_due = lambda self: False\n"
        "main()\n"
    )
    pane.send(f"{sys.executable} {entry} --config {config}")
    deadline = time.monotonic() + 15
    while not any("tmux-startup-model" in line for line in pane.capture()):
        assert time.monotonic() < deadline, "CLI did not start"
        time.sleep(0.05)
    # The pre-frame starting line and the prompt's starting placeholder both go away once the
    # import warm-up settles; neither may linger in history or come back on a replay.
    deadline = time.monotonic() + 15
    while "starting…" in "\n".join(pane.capture()):
        assert time.monotonic() < deadline, "CLI never finished starting"
        time.sleep(0.05)
    _settled_capture(pane)
    pane.send("/agents")
    deadline = time.monotonic() + 15
    while "(current)" not in pane.visible():
        assert time.monotonic() < deadline, "main-only agent picker did not open:\n" + pane.visible()
        time.sleep(0.05)
    assert "main" in pane.visible()
    pane.keys("Escape")
    for cycle, (width, height) in enumerate([(140, 45), (62, 18), (100, 30)] * 3):
        pane.resize(width, height)
        # Editing (without submitting) confirms a fresh frame after each resize, rather than
        # accepting the previous size's capture while SIGWINCH is still being handled.
        pane.keys("C-u")
        draft = f"startup-check-{cycle}"
        pane.literal(draft)
        deadline = time.monotonic() + 15
        while not any(draft in line for line in pane.capture()):
            assert time.monotonic() < deadline, "CLI did not render the new draft"
            time.sleep(0.05)
        lines = _settled_capture(pane)
        assert "\n".join(lines).count("Type / for commands.") == 1
        assert "starting…" not in "\n".join(lines)
        assert sum(line == ">" or line.startswith("> ") for line in lines) == 1
        assert sum("tmux-startup-model" in line for line in lines) == 1


def test_cli_agents_switch_rebuilds_only_selected_transcript(pane):
    config = pane.path / "agents.toml"
    config.write_text(
        f'[paths]\ndata_dir = "{pane.path}/data"\n'
        '[provider]\nactive = "test"\n[provider.test]\n'
        'url = "http://127.0.0.1:9/v1"\nkey = "test-only"\nmodel = "agents-model"\n'
        '[runtime]\ntheme = "forest"\n'
    )
    entry = pane.path / "agents.py"
    entry.write_text(
        "from wizolt.agent.engine import Agent\n"
        "from wizolt.model.client import ModelClient\n"
        "from wizolt.ui.cli.update import UpdateChecker\n"
        "from wizolt.providers.sync import CatalogRuntime\n"
        "from wizolt.__main__ import main\n"
        "original = Agent.start_session\n"
        "async def start(self):\n"
        "    await original(self)\n"
        "    if not self.session.agent_parent:\n"
        "        await self.session.subagents.spawn(self.session, 'reader', 'CHILD-TASK')\n"
        "async def request(self, messages, tools=None):\n"
        "    return {'role': 'assistant', 'content': 'CHILD-ANSWER'}, [], 'CHILD-ANSWER'\n"
        "Agent.start_session = start\n"
        "ModelClient.request = request\n"
        "UpdateChecker.load_cached = lambda self: False\n"
        "CatalogRuntime.refresh_due = lambda self: False\n"
        "main()\n"
    )
    pane.send(f"{sys.executable} {entry} --config {config} --yolo")

    def visible_with(needle):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            visible = pane.visible()
            assert "Unhandled exception" not in visible, visible
            if needle in visible:
                return visible
            time.sleep(0.05)
        raise AssertionError(f"missing {needle!r}:\n{visible}")

    visible_with("[main]")
    visible_with("[reader] completed")
    visible_with("[agents 2 · run 0]")
    assert "CHILD-ANSWER" not in pane.visible()
    for _ in range(2):
        pane.send("/agents")
        visible_with("Agents")
        pane.keys("Down", "Enter")
        visible_with("[reader] [yolo]")
        visible_with("[agents 2 · run 0]")
        for width, height in ((62, 18), (100, 30)):
            pane.resize(width, height)
            _settled_capture(pane)
            visible = visible_with("[reader] [yolo]")
            assert "CHILD-TASK" in "\n".join(pane.capture()), visible
        pane.send("/status")
        visible_with("Overview")
        visible_with("completed")
        pane.keys("Escape")
        _settled_capture(pane)
        pane.send("/agents")
        visible_with("Agents")
        pane.keys("Up", "Enter")
        visible_with("[main] [yolo]")
        assert "CHILD-TASK" not in pane.visible()
    pane.send("/exit")


def test_cli_subagent_approval_config_survives_resize_under_yolo(pane):
    config = pane.path / "approval.toml"
    config.write_text(
        f'[paths]\ndata_dir = "{pane.path}/data"\n'
        '[provider]\nactive = "test"\n[provider.test]\n'
        'url = "http://127.0.0.1:9/v1"\nkey = "test-only"\nmodel = "gpt-4"\n'
        'available_models = ["gpt-4", "o3"]\n[runtime]\ntheme = "forest"\n'
    )
    entry = pane.path / "approval.py"
    entry.write_text(
        "import asyncio\n"
        "from pathlib import Path\n"
        "from wizolt.base import ToolCall\n"
        "from wizolt.model.client import ModelClient\n"
        "from wizolt.ui.cli.update import UpdateChecker\n"
        "from wizolt.ui.cli import commands\n"
        "from wizolt.providers.sync import CatalogRuntime\n"
        "from wizolt.__main__ import main\n"
        "async def models(*args): return ()\n"
        "async def request(self, messages, tools=None):\n"
        "    if self.session.agent_parent:\n"
        f"        while not Path({str(pane.path / 'release-child')!r}).exists(): await asyncio.sleep(.02)\n"
        "        p = self.session.config.provider\n"
        "        text = 'CHILD-CONFIG-' + p.model + '-' + p.reasoning\n"
        "        return {'role': 'assistant', 'content': text}, [], text\n"
        "    if not any(m.get('role') == 'tool' for m in messages):\n"
        "        return {}, [ToolCall('spawn-1', 'Subagent', [{'action': 'spawn', 'name': 'child', 'message': 'APPROVAL-TASK-MARKER'}])], ''\n"
        "    if sum(m.get('role') == 'tool' for m in messages) == 1:\n"
        "        uid = next(uid for uid, e in self.session.subagents.entries.items() if e.parent)\n"
        "        return {}, [ToolCall('wait-1', 'Subagent', [{'action': 'wait', 'agent_ids': [uid], 'timeout': 60}])], ''\n"
        "    return {'role': 'assistant', 'content': 'PARENT-DONE'}, [], 'PARENT-DONE'\n"
        "ModelClient.request = request\n"
        "commands.remote_models = models\n"
        "UpdateChecker.load_cached = lambda self: False\n"
        "CatalogRuntime.refresh_due = lambda self: False\n"
        "main()\n"
    )
    pane.send(f"{sys.executable} {entry} --config {config} --yolo")

    def wait(needle):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            visible = pane.visible()
            assert "Unhandled exception" not in visible, visible
            if needle in visible:
                return visible
            time.sleep(0.05)
        raise AssertionError(f"missing {needle!r}:\n{visible}")

    wait("[main]")
    pane.send("start children")
    wait("View agent task")
    assert "PARENT-DONE" not in pane.visible()
    pane.send("c")
    wait("Subagent config")
    for cycle in range(CYCLES):
        pane.resize(NARROW if cycle % 2 else WIDE, SHORT if cycle % 2 else TALL)
        _settled_capture(pane)
        wait("Subagent config")
        assert "\n".join(pane.capture()).count("APPROVAL-TASK-MARKER") == 1
    pane.keys("Up", "Up", "Up", "Enter")
    wait("Configured models")
    pane.keys("Down", "Enter")
    wait("Request API")
    pane.keys("Enter")
    wait("Reasoning effort")
    pane.literal("G")
    pane.keys("Enter")
    wait("Set provider.reasoning = high")
    wait("Subagent config")
    pane.resize(WIDE, TALL)
    _settled_capture(pane)
    pane.keys("Escape")
    wait("View agent task")
    pane.send("v")
    wait("read-only")
    assert "o3" in pane.visible()
    pane.keys("Escape")
    wait("View agent task")
    pane.keys("Enter")
    # Loading model settings used to strand the parent in DISPATCH with an empty prefix.
    # Hold the child so the assertion covers the waiting turn, before reset_turn hides it.
    wait("Subagent  wait")
    wait("+>")
    pane.send("/agents")
    wait("Agents")
    pane.keys("Down")
    wait("┌─ child")
    pane.literal("x")
    wait("Stop child?")
    pane.keys("Enter")  # Back is the safe default; the child continues.
    wait("Agents")
    pane.keys("Escape")
    wait("+>")
    (pane.path / "release-child").touch()
    wait("PARENT-DONE")
    wait("[child] completed")
    assert "CHILD-CONFIG" not in pane.visible()
    pane.send("/agents")
    wait("Agents")
    pane.keys("Down", "Enter")
    wait("CHILD-CONFIG-o3-high")
    pane.send("/exit")


def test_cli_agents_live_preview_and_stop_keys(pane):
    config = pane.path / "agent-controls.toml"
    config.write_text(
        f'[paths]\ndata_dir = "{pane.path}/data"\n'
        '[provider]\nactive = "test"\n[provider.test]\n'
        'url = "http://127.0.0.1:9/v1"\nkey = "test-only"\nmodel = "test-model"\n'
        '[runtime]\ntheme = "forest"\n[ui.statusbar]\nformat = "preset:lualine"\n'
    )
    entry = pane.path / "agent-controls.py"
    entry.write_text(
        "import asyncio\n"
        "from wizolt.base import ToolCall\n"
        "from wizolt.agent.engine import Agent\n"
        "from wizolt.model.client import ModelClient\n"
        "from wizolt.ui.cli.update import UpdateChecker\n"
        "from wizolt.providers.sync import CatalogRuntime\n"
        "from wizolt.__main__ import main\n"
        "original = Agent.start_session\n"
        "async def start(self):\n"
        "    await original(self)\n"
        "    if not self.session.agent_parent and not self.session.subagent_entries:\n"
        "        for name in ('api-review', 'ui-review'):\n"
        "            await self.session.subagents.spawn(self.session, name, 'Review the shared workspace without editing files.')\n"
        "async def request(self, messages, tools=None):\n"
        "    if not self.session.agent_parent:\n"
        "        if self.session.state.turn_step <= 2:\n"
        "            group = self.session.subagents\n"
        "            uid = next(uid for uid in group.entries if uid != self.session.uid)\n"
        "            action = 'inspect' if self.session.state.turn_step == 1 else 'archive'\n"
        "            return {}, [ToolCall(action, 'Subagent', [{'action': action, 'agent_id': uid}])], ''\n"
        "        return {'role': 'assistant', 'content': 'MAIN-ARCHIVE-DONE'}, [], 'MAIN-ARCHIVE-DONE'\n"
        "    if messages[-1].get('content') == 'finish':\n"
        "        answer = 'SELF-STOP-READY-' + str(sum(m.get('content') == 'finish' for m in messages))\n"
        "        return {'role': 'assistant', 'content': answer}, [], answer\n"
        "    counter = 0\n"
        "    while True:\n"
        "        counter += 1\n"
        "        name = self.session.agent_name\n"
        "        agent = self.session.subagents.entry(self.session.uid).agent\n"
        "        agent.hooks.on_stream('output', 'FLOW-' + name + '-' + str(counter))\n"
        "        await asyncio.sleep(.05)\n"
        "Agent.start_session = start\n"
        "ModelClient.request = request\n"
        "UpdateChecker.load_cached = lambda self: False\n"
        "CatalogRuntime.refresh_due = lambda self: False\n"
        "main()\n"
    )
    pane.send(f"{sys.executable} {entry} --config {config} --yolo")

    def wait(needle, absent="", *, prompt=False):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            visible = pane.visible()
            assert "Unhandled exception" not in visible, visible
            if needle in visible and (not absent or absent not in visible) and (not prompt or re.search(r"(?m)^>(?:\s|$)", visible)):
                return visible
            time.sleep(.05)
        raise AssertionError(f"missing {needle!r}:\n{visible}")

    wait("agents 3 · run 2")
    assert "[agents" not in pane.visible()
    pane.send("/agents")
    wait("Agents")
    pane.keys("Down")
    first = wait("FLOW-api-review-")
    assert "┌─ api-review" in first and "└" in first and "Live" in first
    flow = re.search(r"FLOW-api-review-\d+", first).group()
    wait("FLOW-api-review-", absent=flow)
    pane.literal("x")
    wait("Stop api-review?")
    pane.keys("Enter")  # Decline; the picker opens again at the same row.
    wait("api-review  running")
    pane.literal("x")
    wait("Stop api-review?")
    pane.keys("Up", "Enter")
    wait("api-review  interrupted")
    pane.keys("Down")
    wait("┌─ ui-review")
    pane.literal("X")
    wait("ui-review   interrupted")
    assert "Stop ui-review?" not in pane.visible()
    wait("agents 3 · run 0")
    pane.keys("Enter")  # Enter the stopped child, then let it complete a fresh turn.
    wait("ui-review  test-model", absent="┌─ ui-review")
    for turn, key in enumerate(("x", "X") * 3, 1):
        pane.send("finish")
        wait(f"SELF-STOP-READY-{turn}")
        pane.send("/agents")
        wait("ui-review   completed")
        wait("┌─ ui-review")
        pane.literal(key)
        if key == "x":
            wait("Stop ui-review?")
            pane.keys("Up", "Enter")
        # A completed child already shows run 0 in its Stop confirmation, which also has
        # no preview frame. Wait for the actual prompt before typing into the next view.
        wait("agents 3 · run 0", absent="┌─ ui-review", prompt=True)
        # A cancelled command must restore the prompt, not strand it in DISPATCH.
        pane.send("/agents")
        wait("┌─ ui-review")
        assert "cancelling" not in pane.visible()
        pane.keys("Escape")
        wait("agents 3 · run 0", absent="┌─ ui-review")
        _settled_capture(pane)
    pane.send("/agents")
    wait("┌─ ui-review")
    pane.literal("d")
    wait("Archive ui-review and its children?")
    pane.keys("Up", "Enter")
    # Archiving the agent being read keeps the user on it: the ordinary view, now read-only.
    wait("Archived · read-only · Enter opens /agents", absent="Archive ui-review")
    # The replayed transcript reaches the terminal on the view's first renders, after the prompt.
    assert "ui-review" in wait("SELF-STOP-READY-2")
    wait("agents 2 · run 0")
    pane.literal("typed")
    time.sleep(.2)
    assert "typed" not in pane.visible()
    pane.keys("Enter")  # The only action: open the picker, then return to main (first row).
    wait("┌─ ")
    pane.keys("Up", "Up", "Enter")
    wait("main  test-model", absent="read-only")
    # Reopening it from the picker shows the same read-only view.
    pane.send("/agents")
    wait("archived")
    pane.keys("Down", "Down", "Enter")
    wait("Archived · read-only · Enter opens /agents")
    wait("SELF-STOP-READY-2")
    pane.keys("Enter")
    wait("┌─ ")
    pane.keys("Up", "Up", "Enter")
    wait("main  test-model", absent="read-only")
    _settled_capture(pane)
    pane.send("archive via model")
    wait("slots released")
    wait("api-review: interrupted")
    wait("Approve")  # Even --yolo must stop here, after inspect ran without approval.
    pane.keys("Enter")
    wait("MAIN-ARCHIVE-DONE")
    pane.send("/agents")
    wait("api-review  archived")
    pane.keys("Escape")
    pane.send("/exit")


def _wait_for_markers(log: Path, count: int, timeout: float = 30.0) -> None:
    """Block until the driver has written at least `count` markers."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.exists() and log.read_text().count("wrote MARKER-") >= count:
            return
        time.sleep(0.05)
    raise AssertionError(f"driver wrote fewer than {count} markers within {timeout}s (log exists: {log.exists()})")


@pytest.mark.parametrize("columns", [5, 7])
def test_physical_rows_matches_terminal_wide_glyph_and_tab_wrapping(pane, columns):
    from wizolt.ui.tui.scrollback import physical_rows

    pane.resize(columns, TALL)
    payload = "\x1b[31m" + "中" * 9 + "\r\na\tbcd\r\n\x1b[0m"
    script = pane.path / "rows.py"
    script.write_text(f"import sys, time\nsys.stdout.write('\\x1b[H\\x1b[2J' + {payload!r} + 'END')\nsys.stdout.flush()\ntime.sleep(30)\n")
    pane.send(f"{sys.executable} {script}")
    deadline = time.monotonic() + 15
    while True:
        lines = pane.visible().splitlines()
        if "END" in lines:
            break
        assert time.monotonic() < deadline, lines
        time.sleep(0.05)
    assert lines.index("END") == physical_rows(payload, columns)


def _settled_capture(pane, attempts: int = 40) -> list[str]:
    """Capture once the pane stops changing.

    A fixed pause is a bet on how loaded the machine is; CI runs four workers on two cores and
    loses that bet. Comparing consecutive captures waits for the thing that actually matters.
    """
    previous = pane.capture()
    for _ in range(attempts):
        time.sleep(0.15)
        current = pane.capture()
        if current == previous:
            return current
        previous = current
    capture_path = pane.path / "unstable-capture.txt"
    capture_path.write_text("\n".join(previous))
    raise AssertionError(
        f"pane did not settle after {attempts} captures; last capture saved to {capture_path}\n" + "Last 40 rows:\n" + "\n".join(previous[-40:])
    )


def _stop_selectors(log: Path, timeout: float = 10.0) -> None:
    """Wait for the driver to close its selector and stop reopening it before final capture."""
    log.with_suffix(".settle").touch()
    deadline = time.monotonic() + timeout
    while "selectors stopped\n" not in log.read_text():
        assert time.monotonic() < deadline, "the driver did not settle its selector"
        time.sleep(0.05)


def cycle_size(cycle: int) -> tuple[int, int]:
    return NARROW if cycle % 2 else WIDE, SHORT if cycle % 4 >= 2 else TALL


def test_transcript_survives_repeated_resize_cycles(pane):
    log = pane.path / "write.log"
    # A real split and zoom toggle exercise pane reflow, not only window resizing.
    pane.split()
    pane.zoom()
    # Seed the pane so the app starts partway down a screen that already has content, the way a
    # real session does. Width-change replay deliberately purges pre-application shell history.
    pane.send(f"i=1; while [ $i -le {SEED_LINES} ]; do echo SHELL-$i; i=$((i+1)); done")
    time.sleep(0.8)
    pane.send(f"{sys.executable} {DRIVER} {MARKERS} 0.05 {log}")
    _wait_for_markers(log, 1)

    for cycle in range(CYCLES):
        pane.resize(*cycle_size(cycle))
        pane.zoom()
        # Alternate settled resizes with rapid ones: the failure modes differ. A slow cycle lets
        # a full repaint land between resizes, a fast one leaves streaming output mid-flight.
        time.sleep(0.12 if cycle % 3 else 0.4)

    pane.resize(WIDE, TALL)
    # Let the driver finish before reading anything. Capturing while it still emits makes markers
    # written after the capture look destroyed, which under load is the difference between this
    # test passing alone and failing beside the rest of the suite.
    _wait_for_markers(log, MARKERS)
    _stop_selectors(log)
    lines = _settled_capture(pane)

    text = "\n".join(lines)
    seen = Counter(int(m) for m in re.findall(r"MARKER-(\d+)", text))
    written = {int(m) for m in re.findall(r"wrote MARKER-(\d+)", log.read_text())}
    assert written, "the driver never emitted anything; the acceptance run proves nothing"

    destroyed = sorted(written - set(seen))
    assert not destroyed, f"markers written but no longer in scrollback: {destroyed[:10]}"
    duplicated = sorted(marker for marker, count in seen.items() if count > 1)
    assert not duplicated, f"markers present more than once: {duplicated[:10]}"

    prompts = [line for line in lines if line == ">" or line.startswith("> ")]
    assert len(prompts) == 1, f"expected one prompt, found {len(prompts)}"
    assert sum("tmux-driver" in line for line in lines) == 1, "status missing or duplicated"

    # The driver opens and closes a selector on its own timer while all of this happens. Assert it
    # actually did: a run where the selector never opened proves nothing about selectors.
    cycles = log.read_text().count("selector cycle")
    assert cycles >= 2, f"the selector opened {cycles} times; this run did not exercise it"

    # A restored-wide capture alone can hide corruption at the narrower intermediate size.
    for width, height in [(NARROW, SHORT), (WIDE, TALL)]:
        pane.resize(width, height)
        lines = _settled_capture(pane)
        text = "\n".join(lines)
        assert Counter(int(m) for m in re.findall(r"MARKER-(\d+)", text)) == Counter(written)
        assert sum(line == ">" or line.startswith("> ") for line in lines) == 1
        assert text.count("tmux-driver") == 1


def _blank_rows_after(pane, cycles: int) -> int:
    """Emit a fixed transcript, then resize `cycles` times, and count the blank rows left."""
    log = pane.path / f"blank-{cycles}.log"
    pane.send(f"{sys.executable} {DRIVER} 40 0.03 {log}")
    # Let the whole transcript land before resizing, so both arms compare the same content and
    # the only difference between them is how many times the pane was resized.
    _wait_for_markers(log, 40)
    # Settle the selector before resizing, not after. The driver opens it on its own timer, so a
    # resize can land while it is open; shrinking the pane then pushes its rows into native
    # history, where they stay. That leaves option rows in the capture no later close can remove,
    # and their rows in the count this returns. Selectors under resize are what
    # `test_transcript_survives_repeated_resize_cycles` is for; what this measures is resizes.
    _stop_selectors(log)
    for cycle in range(cycles):
        pane.resize(*cycle_size(cycle))
        time.sleep(0.15)
    pane.resize(WIDE, TALL)
    lines = _settled_capture(pane)
    assert not any(" option " in line for line in lines), "the selector was still visible:\n" + "\n".join(lines)
    assert sum(line == ">" or line.startswith("> ") for line in lines) == 1, "prompt missing or duplicated"
    assert Counter(re.findall(r"MARKER-(\d+)", "\n".join(lines))) == Counter(f"{index:04d}" for index in range(1, 41))
    return sum(1 for line in lines if not line.strip())


def test_blank_rows_do_not_grow_with_resize_cycles(panes):
    """Repeated resizing must not accumulate blank rows.

    An absolute bound cannot express the property, because `UiPrinter` puts real blank rows
    between blocks and their number tracks the transcript. What must not happen is blank rows
    tracking the number of *resizes*, so the same transcript is resized a few times and many
    times and the two counts are compared.
    """
    few = _blank_rows_after(panes(), 4)
    many = _blank_rows_after(panes(), CYCLES)

    assert many <= few + 2, f"blank rows grew with resize count: {few} after 4 cycles, {many} after {CYCLES}"


@pytest.mark.parametrize("markers", [1, 40], ids=["short-history", "scrollback"])
def test_completed_rules_fit_each_width_without_losing_text(pane, markers):
    log = pane.path / "rules.log"
    pane.send(f"{sys.executable} {DRIVER} {markers} 0.01 {log} rules")
    deadline = time.monotonic() + 30
    while not log.exists() or "rules complete" not in log.read_text():
        assert time.monotonic() < deadline, "the driver did not finish its rules"
        time.sleep(0.05)

    # Inspect the narrow state too: returning to the original width hides wrapped rule tails.
    for width in (WIDE, NARROW, 37, WIDE, NARROW):
        pane.resize(width, TALL)
        lines = _settled_capture(pane)
        joined = "".join(lines)
        for marker in ("USER-BEGIN", "USER-END", "ANSWER-BEGIN", "ANSWER-END", "done in 1m05s"):
            assert joined.count(marker) == 1, (width, marker, lines)
        rules = [line for line in lines if "─" in line]
        assert len(rules) == 2, f"expected two single-row rules at width {width}: {rules}"
        assert all(get_cwidth(line) == width for line in rules), (width, rules)


def test_exit_drains_output_accepted_before_the_last_render(pane):
    log = pane.path / "exit.log"
    pane.send(f"{sys.executable} {DRIVER} 1 0.01 {log} exit")
    deadline = time.monotonic() + 30
    while not log.exists() or "driver exited" not in log.read_text():
        assert time.monotonic() < deadline, "the application did not exit"
        time.sleep(0.05)
    text = "\n".join(_settled_capture(pane))
    for marker in ("MARKER-0001", "EXIT-FIRST", "EXIT-SECOND"):
        assert text.count(marker) == 1, (marker, text)
    assert text.index("EXIT-FIRST") < text.index("EXIT-SECOND"), text


def test_zoom_on_a_fresh_pane_leaves_one_live_region(pane):
    """A pane zoom while the app still floats above unused space leaves no copy behind.

    The window-resize case is covered above; a zoom is a different reflow, and the state that
    matters is the one a session starts in: the app is not flush with the pane bottom, it sits
    partway down with empty rows below it. Erasing from the bottom of the pane then misses the
    rows the app actually occupies, and the previous live region -- divider, prompt and status
    row -- stays on screen as text while the app redraws lower.

    Height changes must replay the retained transcript too: erasing only the visible screen
    cannot remove a displaced live region that has already entered native scrollback.
    """
    log = pane.path / "zoom.log"
    pane.send(f"{sys.executable} {DRIVER} 60 0.05 {log} fresh")
    _wait_for_markers(log, 4)
    # A second pane, so the app's pane can be zoomed and unzoomed the way a user does it.
    pane.split(vertical=True)
    log.with_suffix(".more").touch()

    for _ in range(3):
        pane.zoom()
        time.sleep(0.3)
        pane.zoom()
        time.sleep(0.3)

    _wait_for_markers(log, 60)
    lines = _settled_capture(pane)
    text = "\n".join(lines)

    assert sum(line == "+>" or line.startswith("+> ") for line in lines) == 1, text
    assert text.count("tmux-driver") == 1, text
    written = {int(m) for m in re.findall(r"wrote MARKER-(\d+)", log.read_text())}
    seen = Counter(int(m) for m in re.findall(r"MARKER-(\d+)", text))
    assert not [marker for marker in written if seen[marker] != 1], (seen, text)


def test_bar_cascade_previews_survive_resize_and_cancel(pane):
    """Tabbed appearance previews, including animated samples, remain usable after 30 resizes."""
    log = pane.path / "bars.log"
    pane.send(f"{sys.executable} {DRIVER} 0 0 {log} bars")

    def visible_containing(needle):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            visible = pane.visible()
            if needle in visible:
                return visible
            time.sleep(0.03)
        raise AssertionError(f"missing {needle!r}:\n{visible}")

    visible_containing("bars-model")
    for cycle in range(3):
        log.with_suffix(f".open-{cycle}").touch()
        visible_containing("Colorscheme")
        pane.keys("h", "h")
        visible_containing("Running (preview)")
        chosen = ("capsule", "frame", "rail")[cycle]
        pane.keys(str(cycle + 2))
        visible_containing(chosen)
        for index in range(10):
            width, height = ((100, 30), (60, 18), (80, 24), (50, 12), (120, 35))[index % 5]
            pane.resize(width, height)
            time.sleep(0.06 if index % 2 else 0.15)
            _settled_capture(pane)
            visible = visible_containing(chosen)
            assert chosen in visible, visible
        pane.resize(100, 30)
        visible_containing("Running (preview)")
        if cycle == 0:
            pane.keys("Tab", "j")
            visible_containing("ripple")
            pane.keys("Escape")
        elif cycle == 1:
            pane.keys("C-c")
        else:
            pane.keys("Space", "Tab", "j", "j")
            visible_containing("aurora")
            pane.keys("Space", "Tab", "j", "j", "j", "j", "Space")
            visible_containing("Colors: slate")
            pane.keys("Enter")
        deadline = time.monotonic() + 15
        while f"closed {cycle}:" not in log.read_text():
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.03)
        history = "\n".join(_settled_capture(pane))
        for marker in range(5):
            assert history.count(f"BAR-MARKER-{marker}") == 1
        assert history.count("bars-model") == 1
    assert "closed 1: interrupted" in log.read_text()
    assert "divider.format: preset:rail" in log.read_text()
    assert "divider.sweep: preset:aurora" in log.read_text()
    assert "divider.theme: slate" in log.read_text()
    log.with_suffix(".open-3").touch()
    visible_containing("Colorscheme")
    pane.keys("l", "l")
    visible_containing("The status bar below")
    pane.resize(100, 30)
    _settled_capture(pane)
    header_row = next(i for i, line in enumerate(pane.visible().splitlines()) if "Colorscheme" in line)
    for key in ("h", "h", "h", "l", "l", "l"):
        pane.keys(key)
        _settled_capture(pane)
        visible = visible_containing("Colorscheme")
        assert next(i for i, line in enumerate(visible.splitlines()) if "Colorscheme" in line) == header_row, visible
    pane.keys("h")
    for width, height in ((100, 30), (60, 18), (80, 24)):
        pane.resize(width, height)
        pane.keys("j")
        visible = visible_containing("bars-model")
        assert visible.count("bars-model") == 1, visible
    pane.keys("Escape")
    deadline = time.monotonic() + 15
    while "closed 3:" not in log.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.03)
    log.with_suffix(".open-4").touch()
    visible_containing("Colorscheme")
    pane.keys("h", "j")
    visible_containing("❯ Explain this function")
    for width, height in ((60, 18), (100, 30), (80, 24)):
        pane.resize(width, height)
        # A shrinking pane clips the sample; the chosen row and shortcuts stay usable.
        _settled_capture(pane)
        visible = visible_containing("bars-model")
        assert "chevron" in visible and "e edit" in visible, visible
    pane.keys("e", "C-u", "λ", "Space", "Tab", "C-u", "→", "Space", "Enter")
    visible_containing("* custom")
    pane.keys("Enter")
    deadline = time.monotonic() + 15
    while "closed 4:" not in log.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.03)
    assert "Input: 'λ ' · running '→ '" in log.read_text()
    lines = _settled_capture(pane)
    assert sum(line == "λ" or line.startswith("λ ") for line in lines) == 1
    log.with_suffix(".done").touch()
    deadline = time.monotonic() + 15
    while "driver exited" not in log.read_text():
        assert time.monotonic() < deadline
        time.sleep(0.03)
    history = "\n".join(pane.capture())
    for marker in range(5):
        assert history.count(f"BAR-MARKER-{marker}") == 1


def test_format_draft_and_status_tabs_survive_resizes(pane):
    """A format draft keeps its text and keys through resizes, saves through the config, and the
    /status view and its printed report stay single and framed as the pane changes."""
    log = pane.path / "formats.log"
    pane.send(f"{sys.executable} {DRIVER} 0 0 {log} formats")

    def visible_containing(needle):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            visible = pane.visible()
            if needle in visible:
                return visible
            time.sleep(0.03)
        raise AssertionError(f"missing {needle!r}:\n{visible}")

    def logged(line):
        deadline = time.monotonic() + 15
        while line not in log.read_text():
            assert time.monotonic() < deadline, log.read_text()
            time.sleep(0.03)

    sizes = ((100, 30), (60, 18), (80, 24), (50, 14), (120, 35))
    visible_containing("FORMAT-MARKER")
    log.with_suffix(".open").touch()
    visible_containing("Colorscheme")
    pane.keys("l", "l")
    visible_containing("p placement")
    pane.keys("f")
    visible_containing("ui.statusbar.format")
    pane.keys("e")
    visible_containing("Ctrl-S apply")
    pane.keys("Home")
    pane.literal("MYBAR ")
    visible_containing("MYBAR ")
    for width, height in sizes:
        pane.resize(width, height)
        _settled_capture(pane)
        # The draft and the keys that save or leave it stay on screen at every size.
        visible = visible_containing("MYBAR")
        assert "Ctrl-S apply" in visible, visible
    pane.resize(100, 30)
    pane.keys("C-s")
    visible_containing("Draft applied")
    pane.keys("Escape")
    visible_containing("custom format, f to edit")
    pane.keys("Enter")
    logged("closed: statusbar.format: MYBAR ")

    visible_containing("Overview")
    pane.keys("l")
    visible_containing("compacts at")
    for width, height in sizes:
        pane.resize(width, height)
        _settled_capture(pane)
        # One frame, its tabs and its keys, at every size; a short pane scrolls the rest.
        visible = visible_containing("Overview")
        assert visible.count("Overview") == 1 and visible.count("Esc close") == 1, visible
    pane.resize(100, 30)
    visible_containing("compacts at")
    pane.keys("Escape")
    logged("status view closed")
    logged("status shown")
    visible_containing("All requests")
    for width, height in sizes:
        pane.resize(width, height)
        _settled_capture(pane)
    # The printed report is replayed once at each width, its frame intact.
    history = "\n".join(_settled_capture(pane))
    assert history.count("All requests") == 1 and history.count("permissions") == 1, history
    assert history.count("FORMAT-MARKER") == 1
    log.with_suffix(".done").touch()
    logged("driver exited")


@pytest.mark.parametrize("vertical", [False, True], ids=["width", "height"])
def test_streaming_tool_results_stay_attached_during_zoom(pane, vertical):
    """Tool completion and the next preview race resize; settled previews alone miss this."""
    log = pane.path / "command-stream.log"
    pane.send(f"{sys.executable} {DRIVER} 0 0 {log} command-stream")
    deadline = time.monotonic() + 15
    while "COMMAND-0" not in pane.visible():
        assert time.monotonic() < deadline, pane.visible()
        time.sleep(0.02)
    pane.split(vertical=vertical)
    for _ in range(30):
        pane.zoom()
        time.sleep(0.18)
    deadline = time.monotonic() + 15
    while "RESULT-23" not in "\n".join(pane.capture()):
        assert time.monotonic() < deadline, pane.visible()
        time.sleep(0.05)
    lines = _settled_capture(pane)
    (pane.path / "command-stream-capture.txt").write_text("\n".join(lines))
    previous = None
    for index in range(24):
        headers = [i for i, line in enumerate(lines) if re.search(rf"COMMAND-{index}\b", line)]
        results = [i for i, line in enumerate(lines) if re.search(rf"RESULT-{index}\b", line)]
        assert len(headers) == len(results) == 1, "\n".join(lines)
        assert results[0] == headers[0] + 1, "\n".join(lines)
        if previous is not None:
            assert headers[0] == previous + 1, "\n".join(lines)
        previous = results[0]


def test_command_preview_follows_header_without_moving_input(pane):
    pane.fresh_window()
    log = pane.path / "commands.log"
    pane.send(f"{sys.executable} {DRIVER} 0 0 {log} commands")

    def visible_containing(needle):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            visible = pane.visible()
            if needle in visible:
                return visible.splitlines()
            time.sleep(0.03)
        raise AssertionError(f"missing {needle!r}:\n{pane.visible()}")

    positions = []
    for index in range(3):
        visible_containing(f"COMMAND-{index}")
        for width, height in ((WIDE, TALL), (NARROW, SHORT), (NARROW, TALL), (WIDE, TALL)):
            pane.resize(width, height)
            # Clocks and the spark keep changing; settle geometry, not animated text.
            deadline = time.monotonic() + 5
            stable = 0
            while stable < 3:
                lines = pane.visible().splitlines()
                header = next((i for i, line in enumerate(lines) if f"COMMAND-{index}" in line), -10)
                live = next((i for i, line in enumerate(lines) if "running…" in line), -1)
                stable = stable + 1 if live == header + 2 and len(lines) == height else 0
                assert time.monotonic() < deadline, "\n".join(lines)
                time.sleep(0.05)
            if height == TALL:
                positions.append(next(i for i, line in enumerate(lines) if line.startswith("+>")))
        log.with_suffix(f".output-{index}").touch()
        visible_containing("five")
        log.with_suffix(f".next-{index}").touch()
    visible_containing("RESULT-2")
    history = "\n".join(_settled_capture(pane))
    assert all(history.count(f"COMMAND-{i}") == history.count(f"RESULT-{i}") == 1 for i in range(3)), history
    assert len(set(positions)) == 1
