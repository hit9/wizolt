"""Offline interaction fixtures exercise actual worker RPC, dialog states and persistence."""

import json
from pathlib import Path

import pytest

from wizolt.ui.cli.plugin_testing import main


@pytest.fixture
def scenario(tmp_path):
    path = tmp_path / "pet.py"
    path.write_text("""from wizolt.sdk.views import Choice, View, Document
SDK_VERSION = 1
def setup(p):
    p.configure({"type":"object", "properties":{"pet":{"enum":["cat","dragon"]}}, "additionalProperties":False}, defaults={"pet":"cat"})
    async def choose(ctx,args):
        choice = await p.ui.select("Pet", items=(Choice("cat","Cat"),Choice("dragon","Dragon")))
        if choice is None:
            return "cancelled"
        saved = await p.settings.update({"pet":choice})
        return saved["pet"] + "/" + p.config["pet"]
    async def live(ctx,args):
        async with p.ui.open(View("Loading", Document("one"))) as view:
            await view.update(View("Ready", Document("two")))
            result = await view.result()
            return result.action if result else "cancelled"
    async def form(ctx,args):
        return await p.ui.input("Name", required=True) or "cancelled"
    async def guarded(ctx,args):
        try:
            return (await p.ui.show(View("Loading", Document("one")))).action
        except Exception:
            return "handled"
    p.command("pet","Choose",choose)
    p.command("live","Live",live)
    p.command("form","Form",form)
    p.command("guarded","Guarded",guarded)
""")
    config = tmp_path / "config.toml"
    config.write_text(f'[paths]\ndata_dir = "{tmp_path}/data"\n[plugins.pet]\npet = "cat" # keep\n')
    return path, config


def run_scenario(scenario, tmp_path, capsys, steps, command="pet", *options):
    path, config = scenario
    fixture = tmp_path / "answers.json"
    fixture.write_text(json.dumps(steps))
    code = main(
        ["test", str(path), "--config", str(config), "--call", "command:" + command, "--interactions", str(fixture), "--output", str(tmp_path / "output"), *options]
    )
    return code, json.loads(capsys.readouterr().out)


def test_scripted_selection_persists_only_in_trial_and_exports_stable_content(scenario, tmp_path, capsys):
    before = scenario[1].read_bytes()
    steps = [{"expect": {"title": "Pet", "kind": "selection"}, "reply": {"selected": ["dragon"]}}]
    code, report = run_scenario(scenario, tmp_path, capsys, steps)
    assert code == 0 and report["results"] == ["dragon/cat"]
    assert report["settings_changes"] == [{"values": {"pet": "dragon"}, "reset": []}]
    assert scenario[1].read_bytes() == before
    assert report["interactions"] and report["previews"]
    assert all(Path(item["svg_path"]).is_file() and Path(item["png_path"]).is_file() for item in report["previews"])
    code, repeated = run_scenario(scenario, tmp_path, capsys, steps)
    assert code == 0
    assert [item["render_sha256"] for item in report["previews"]] == [item["render_sha256"] for item in repeated["previews"]]
    assert report["previews"][0]["svg_path"] != repeated["previews"][0]["svg_path"]


@pytest.mark.parametrize(
    "steps",
    [
        [],
        [{"expect": {"title": "Wrong", "kind": "selection"}, "reply": None}],
        [{"expect": {"title": "Pet", "kind": "selection"}, "reply": {"selected": ["missing"]}}],
        [{"expect": {"title": "Pet", "kind": "selection"}, "reply": None}] * 2,
    ],
)
def test_missing_wrong_invalid_and_unused_answers_fail(scenario, tmp_path, capsys, steps):
    before = scenario[1].read_bytes()
    code, report = run_scenario(scenario, tmp_path, capsys, steps)
    assert code == 1 and report["status"] == "failed"
    assert scenario[1].read_bytes() == before


def test_live_update_and_action_result(scenario, tmp_path, capsys):
    code, report = run_scenario(
        scenario,
        tmp_path,
        capsys,
        [{"expect": {"title": "Loading", "kind": "document"}, "updates": [{"title": "Ready", "kind": "document"}], "reply": {"action": "submit"}}],
        "live",
    )
    assert code == 0 and report["results"] == ["submit"]
    assert [item["view"]["title"] for item in report["interactions"] if "view" in item] == ["Loading", "Ready", "Ready"]


def test_missing_expected_update_fails_even_when_the_plugin_handles_the_error(scenario, tmp_path, capsys):
    steps = [{"expect": {"title": "Loading", "kind": "document"}, "updates": [{"title": "Ready", "kind": "document"}], "reply": {"action": "submit"}}]
    code, report = run_scenario(scenario, tmp_path, capsys, steps, "guarded", "--timeout", "0.3")
    assert report["results"] == ["handled"]
    assert code == 1 and report["stage"] == "interactions"
    assert "1 expected updates did not arrive" in report["error"]


@pytest.mark.parametrize("reply,code", [({"values": {"value": "Alice"}}, 0), ({"values": {"value": ""}}, 1), (None, 0)])
def test_form_uses_real_required_field_validation(scenario, tmp_path, capsys, reply, code):
    actual, report = run_scenario(scenario, tmp_path, capsys, [{"expect": {"title": "Name", "kind": "form"}, "reply": reply}], "form")
    assert actual == code
    if not code:
        assert report["results"] == (["Alice"] if reply else ["cancelled"])


def test_render_digest_covers_content_color_and_geometry(tmp_path):
    from wizolt.ui.cli.plugin_preview import PreviewExporter

    exporter = PreviewExporter(tmp_path)
    original = exporter.draw([("fg:#ff0000", "one\ntwo")], "test", 40, 0)
    equivalent = exporter.draw([("fg:#ff0000", "one"), ("", "\n"), ("fg:#ff0000", "two")], "test", 40, 1)
    assert original["rows"] == 2
    assert original["render_sha256"] == equivalent["render_sha256"]
    variants = [
        exporter.draw([("fg:#00ff00", "one\ntwo")], "test", 40, 2),
        exporter.draw([("fg:#ff0000", "new\ntext")], "test", 40, 3),
        exporter.draw([("fg:#ff0000", "one\ntwo")], "test", 50, 4),
    ]
    assert all(item["render_sha256"] != original["render_sha256"] for item in variants)
