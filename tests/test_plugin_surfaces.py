"""Reusable UI surfaces and structured facts, rather than special cases for individual mods."""

import json
from dataclasses import asdict, replace

import pytest
from agent_harness import session_with_provider

from wizolt.agent.engine import Agent
from wizolt.plugins.protocol import Snapshot
from wizolt.plugins.testing import PluginTrial
from wizolt.sdk import Context, ContextWindow, Line, Panel, PluginError, Text, Usage
from wizolt.ui.cli.plugin_preview import PreviewExporter
from wizolt.ui.cli.plugins import PluginView
from wizolt.ui.render import Theme


def context():
    return Context("test", "main", "/tmp", "running", 20, 0, "model", 2, 20, Usage(output_rate=42), ContextWindow(200, 1000, 900, (("system", 100), ("messages", 100))))


def test_public_snapshot_roundtrip_keeps_typed_immutable_facts():
    original = context()
    decoded = Context.decode(json.loads(json.dumps(asdict(original))))
    assert decoded == original
    assert decoded.window.parts == (("system", 100), ("messages", 100))
    with pytest.raises(AttributeError):
        decoded.usage.output_rate = 10


def test_colored_lines_share_clipping_and_all_slots_share_one_height_budget(tmp_path):
    panel = Panel((Line((Text("abc", "success"), Text("def", "warning"))),))
    fragments = PluginView.render([panel], 4, 1)
    assert "".join(text for _, text in fragments) == "abcd"
    assert [style for style, _ in fragments] == [Theme.fg("success"), Theme.fg("warning")]
    panels = {slot: [panel] for slot in PluginView.INPUT_SLOTS}
    projected = PluginView.project(panels, 4, 18)
    assert sum(bool(rows) for rows in projected.values()) == PluginView.input_rows(18) == 2
    assert not projected["below_input"]
    frame = {"context": asdict(context()), "fields": {}, "panels": {slot: asdict(panel) for slot in panels}}
    reports = PreviewExporter(tmp_path, height=18).export(frame, 0)
    assert next(item for item in reports if item["slot"] == "below_input")["text"] == ""
    assert next(item for item in reports if item["slot"] == "above_divider")["styles"] == PluginView.render([panel], 20, 1)


def test_multispan_rows_have_one_total_transport_budget():
    with pytest.raises(PluginError, match="characters"):
        Snapshot.check_panel(Panel((Line(tuple(Text("a" * 1000) for _ in range(5))),)))
    with pytest.raises(PluginError, match="spans"):
        Snapshot.check_panel(Panel((Line(tuple(Text("") for _ in range(257))),)))


async def test_sampling_can_collect_metrics_before_pure_rendering(tmp_path):
    source = tmp_path / "wave.py"
    source.write_text('''SDK_VERSION = 1
from wizolt.sdk import Line, Panel, Text
def setup(p):
    rates = []
    async def sample(event):
        rates.append(event.context.usage.output_rate)
    p.on("tick", sample)
    p.component("above_divider", lambda ctx: Panel((Line((Text(str(len(rates)), "success"), Text(str(ctx.window.parts[0]), "warning"))),)))
''')
    report = await PluginTrial(context()).run(str(source), times=(0, 1, 2))
    assert report.status == "passed", report.error
    panels = [Snapshot.decode(frame).panels["above_divider"] for frame in report.frames]
    assert [panel.rows[0].spans[0].text for panel in panels] == ["1", "2", "3"]
    assert "system" in panels[0].rows[0].spans[1].text


async def test_model_request_populates_context_categories_without_recomputing_on_sample(tmp_path, monkeypatch):
    session = session_with_provider(tmp_path)
    agent = Agent(session, output_fn=lambda _: None)
    source = tmp_path / "meter.py"
    source.write_text('SDK_VERSION = 1\ndef setup(p):\n    p.field("used", lambda ctx: ctx.window.used)\n')
    await session.plugins.manage("enable", str(source))

    async def answer(*args, **kwargs):
        return {"role": "assistant", "content": "done"}, [], "done"

    monkeypatch.setattr(agent.model, "request", answer)
    try:
        await agent.run("hello")
        snapshot = session.plugins.snapshot()
        assert dict(snapshot.window.parts)["messages"] > 0
        parts = session.plugins.context_parts
        session.usage.completion_tokens = 50
        other = session.plugins.snapshot()
        assert other.window.parts is parts
        assert other.usage.output_tokens == 50
        assert replace(snapshot, usage=other.usage).window == other.window
    finally:
        await session.plugins.close()
        await agent.model.close()
        session.close()


@pytest.mark.parametrize("name,slot", [("context_bar", "above_divider"), ("token_wave", "below_input")])
async def test_published_skill_examples_run_with_structured_metrics(tmp_path, name, slot):
    from test_plugins import example

    report = await PluginTrial(context()).run(example(tmp_path, name), times=(0, 1, 2))
    assert report.status == "passed", report.error
    assert len(report.frames) == 3
    assert Snapshot.decode(report.frames[-1]).panels[slot].rows


def test_offline_facts_drive_the_actual_context_bar_preview(tmp_path, capsys):
    from test_plugins import example

    from wizolt.ui.cli.plugin_testing import main

    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps({"window": {"used": 200, "limit": 1000, "parts": [["system", 100], ["messages", 100]]}}))
    assert main(["test", example(tmp_path, "context_bar"), "--facts", str(facts), "--width", "30", "--project", str(tmp_path)]) == 0
    report = json.loads(capsys.readouterr().out)
    preview = report["previews"][0]
    assert "context 200 / 1000" in preview["text"]
    assert "█" in preview["text"] and "░" in preview["text"]


def test_trial_inputs_accept_pipes_and_symlinks(tmp_path, capsys):
    import os

    from test_plugins import example

    from wizolt.ui.cli.plugin_testing import main

    # What `--facts <(...)` hands the command: a /dev/fd path backed by a pipe.
    read, write = os.pipe()
    os.write(write, json.dumps({"window": {"used": 300, "limit": 1000}}).encode())
    os.close(write)
    try:
        assert main(["test", example(tmp_path, "context_bar"), "--facts", f"/dev/fd/{read}", "--project", str(tmp_path)]) == 0
    finally:
        os.close(read)
    assert "context 300 / 1000" in json.loads(capsys.readouterr().out)["previews"][0]["text"]
    history = tmp_path / "history.txt"
    history.write_text("Decided to ship.\n")
    (tmp_path / "link.txt").symlink_to(history)
    plugin = tmp_path / "digest.py"
    plugin.write_text("SDK_VERSION = 1\ndef setup(p):\n    async def summarize(ctx, text):\n        return 'Digest: ' + text.strip()\n    p.summarizer(summarize)\n")
    assert main(["test", str(plugin), "--summarize", str(tmp_path / "link.txt"), "--project", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["results"] == ["Digest: Decided to ship."]


def test_rejected_contribution_reports_the_validate_stage(tmp_path, capsys):
    from wizolt.ui.cli.plugin_testing import main

    plugin = tmp_path / "look.py"
    plugin.write_text('SDK_VERSION = 1\ndef setup(p):\n    p.preset("statusbar", "bad", "{no_such_field}")\n')
    assert main(["validate", str(plugin), "--project", str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["stage"] == "validate" and "no_such_field" in report["error"]


def test_bundled_pet_keeps_its_shape_and_theme_roles_in_every_mood():
    from prompt_toolkit.utils import get_cwidth

    from wizolt.plugins.builtin.pet import draw

    shapes = set()
    for status in ("", "running", "waiting", "failed", "completed", "interrupted"):
        for now in (0, 0.5, 1, 3.5):
            panel = draw(Context("a", "main", "/", status, 0, 0, "m", now))
            rows = [row.spans if isinstance(row, Line) else (row,) for row in panel.rows]
            assert all(span.role in Theme.ROLES for spans in rows for span in spans)
            face = "".join(span.text for span in rows[1])
            shapes.add((len(rows), get_cwidth(rows[0][0].text), get_cwidth(face.partition("  ")[0])))
    assert shapes == {(2, 6, 8)}  # Animation and mood never move the outline or the caption.
