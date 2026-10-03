"""The shipped skill must describe public contracts and execute the code it teaches."""

import asyncio
import inspect
import json
import re
import subprocess
import sys
from dataclasses import fields
from pathlib import Path

import pytest

from wizolt import sdk
from wizolt.plugins.runtime import PluginRuntime
from wizolt.plugins.testing import PluginTrial, Stimulus
from wizolt.sdk.models import ModelReply
from wizolt.sdk.ui import Component
from wizolt.ui.bars import FIELDS, Template
from wizolt.ui.cli.plugin_appearance import AppearanceContribution
from wizolt.ui.render import Theme
from wizolt.ui.themes import DIFF_KEYS

REFERENCE = Path(__file__).parents[1] / "wizolt/skill/builtin/plugin-workshop"
GUIDE = Path(__file__).parents[1] / "docs/plugins.md"


def guide_examples() -> list[tuple[str, str]]:
    """Every plugin in the user guide, named as its figure marker names it (cost, meter, ...)."""
    names = ("meter", "cost", "note", "recall")
    blocks = re.findall(r"(?:<!-- figure: plugins-(\w+) -->\s*)?```python\n(.*?)```", GUIDE.read_text(), re.DOTALL)
    examples = [(marker or names[index], code) for index, (marker, code) in enumerate(blocks)]
    assert [name for name, _ in examples] == list(names), "keep this list in the guide's order"
    return examples


@pytest.mark.parametrize(("name", "code"), guide_examples())
async def test_user_guide_examples_pass_the_authoring_checks(tmp_path, name, code):
    from wizolt.ui.cli.plugin_testing import main

    path = tmp_path / f"{name}.py"
    path.write_text(code)
    # Shell-free and in-process: the same validation the agent runs before showing a plugin.
    assert await asyncio.to_thread(main, ["test", str(path), "--project", str(tmp_path), "--output", str(tmp_path)]) == 0


def section(document, title):
    return document.split(f"## {title}\n", 1)[1].split("\n## ", 1)[0]


def test_skill_links_reach_the_reference_and_installed_source():
    """The loaded skill names its references by absolute path: no lookup before reading them."""
    from wizolt.skill.invocation import Invocation
    from wizolt.skill.skillfile import SkillFile

    body = Invocation(SkillFile.parse(str(REFERENCE / "SKILL.md"), "plugin-workshop", "builtin")).prepared_body()
    assert f"[SDK.md]({REFERENCE / 'SDK.md'})" in body
    assert f"[APPEARANCE.md]({REFERENCE / 'APPEARANCE.md'})" in body
    assert "wizolt plugin paths" in body and '"${WIZOLT_EXECUTABLE:-wizolt}" plugin' in body
    for target in re.findall(r"\]\(([^)]+)\)", body):
        assert Path(target).is_absolute() and Path(target).is_file(), target
    for name in ("SDK.md", "APPEARANCE.md"):
        for target in re.findall(r"\]\(([^)]+)\)", (REFERENCE / name).read_text()):
            assert (REFERENCE / target).is_file(), (name, target)


def test_paths_resolves_this_executable_independently_of_cwd_and_config(tmp_path):
    broken = tmp_path / "config.toml"
    broken.write_text("not valid TOML = [")
    result = subprocess.run(
        [sys.executable, "-P", "-m", "wizolt", "plugin", "paths", "--config", str(broken)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=True,
        timeout=10,
    )
    paths = {name: Path(path) for name, path in json.loads(result.stdout).items()}
    assert paths["sdk"] == Path(sdk.__file__).resolve().parent
    assert paths["source"] == paths["sdk"].parent
    assert paths["skill"] == REFERENCE / "SKILL.md"
    assert paths["api_reference"] == REFERENCE / "SDK.md"
    assert paths["appearance_reference"] == REFERENCE / "APPEARANCE.md"
    assert all(path.is_absolute() and path.exists() for path in paths.values())
    assert set(tmp_path.iterdir()) == {broken}


def test_paths_rejects_an_ignored_plugin_argument(capsys):
    from wizolt.ui.cli.plugin_commands import main

    with pytest.raises(SystemExit) as error:
        main(["paths", "pet"])
    assert error.value.code == 2
    assert "does not take a plugin name" in capsys.readouterr().err


def test_public_registration_methods_are_in_the_api_index():
    document = (REFERENCE / "SDK.md").read_text()
    index = section(document, "Public API index")
    actual = {name for name, value in vars(sdk.Plugin).items() if not name.startswith("_") and inspect.isfunction(value)}
    documented = set(re.findall(r"`plugin\.([a-z_]+)\(", index))
    assert documented == actual
    assert "plugin.models.complete(" in index and "handle.get()" in index


@pytest.mark.parametrize("value", [sdk.Context, sdk.Usage, sdk.ContextWindow, sdk.Viewport, sdk.Layout, sdk.Turn, sdk.ToolCounts, sdk.ToolActivity, Component, sdk.Text, sdk.Line, sdk.Panel, sdk.Event, ModelReply])
def test_value_tables_cover_every_public_field(value):
    document = (REFERENCE / "SDK.md").read_text()
    row = next(line for line in document.splitlines() if line.startswith(f"| `{value.__name__}` |"))
    names = set(re.findall(r"`([a-z_]+):", row))
    assert names == {field.name for field in fields(value)}


def test_appearance_tables_match_available_roles_fields_and_diff_keys():
    document = (REFERENCE / "APPEARANCE.md").read_text()
    for title, expected in (("Color roles", Theme.ROLES), ("Format fields", FIELDS), ("Diff keys", DIFF_KEYS)):
        body = section(document, title)
        if title != "Diff keys":
            body = "\n".join(line for line in body.splitlines() if line.startswith("|"))
        assert set(re.findall(r"`([^`]+)`", body)) == set(expected)
    groups = re.findall(r"`([^`]+)`", "\n".join(line for line in section(document, "Bar highlight groups").splitlines() if line.startswith("|")))
    assert Theme.bar_styles(set(groups))


async def test_shipped_appearance_example_validates_and_renders(tmp_path):
    document = (REFERENCE / "APPEARANCE.md").read_text()
    code = re.search(r"```python\n(.*?)```", document, re.DOTALL).group(1)
    path = tmp_path / "appearance_demo.py"
    path.write_text(code)
    context = sdk.Context("test", "main", str(tmp_path), "idle", 10, 0, "model", 0)
    runtime = PluginRuntime(lambda: context)
    runtime.validate = AppearanceContribution.validate
    try:
        await runtime.manage("enable", str(path))
        capability = runtime.entries["appearance_demo"].active.plugin
        for kind, choices in capability.presets.items():
            for source in choices.values():
                template = Template(source)
                values = dict.fromkeys(FIELDS, 0)
                values.update({"agent.name": "main", "model": "model", "running": True, "spinner": "●", "label": "working"})
                rendered = "".join(text for _, text in template.render(values, 80, Theme.bar_styles(template.styles)))
                assert ("main" if kind == "statusbar" else "working") in rendered
    finally:
        await runtime.close()


async def test_shipped_helper_exercises_services_models_and_summary(tmp_path):
    document = (REFERENCE / "SDK.md").read_text()
    code = re.search(r"```python\n(.*?)```", section(document, "Example: helper.py"), re.DOTALL).group(1)
    path = tmp_path / "helper.py"
    path.write_text(code)
    context = sdk.Context("test", "main", str(tmp_path), "idle", 0, 0, "model", 0)
    report = await PluginTrial(context).run(str(path), stimuli=(Stimulus("tool", "self_test"),))
    assert report.status == "passed", report.error
    assert report.results == ["Helper is ready"]
    runtime = PluginRuntime(lambda: context)

    async def model(operation, arguments):
        assert operation == "model.complete" and arguments["prompt"] in ("Question", "A short history")
        return {"text": "Answer", "model": "test", "usage": {"input_tokens": 12, "output_tokens": 3}}

    runtime.host_service = model
    try:
        await runtime.manage("enable", str(path))
        result = await runtime.invoke("helper", "command", "helper-ask", {"input": "Question"})
        assert result == "Answer\nTokens: 12 in / 3 out"
        assert await runtime.summarize("A short history", lambda _: None) == ("helper", "Answer")
    finally:
        await runtime.close()
