"""--operations: real handlers, the live chain executor and a scripted core; nothing real runs."""

import json

import pytest

from wizolt.ui.cli.plugin_testing import main

PLUGIN = """
from wizolt.sdk.operations import Refusal, Summary
SDK_VERSION = 1
def setup(p):
    async def tools(ctx, call, next):
        if "rm" in call.arguments["command"]:
            return Refusal("no deletes")
        result = await next(call.replace(arguments={**call.arguments, "command": call.arguments["command"] + " --color=never"}))
        return result.replace(content=result.content.upper())
    async def summarize(ctx, span, next):
        return Summary("digest: " + span.text[:20])
    p.intercept("tool.call", tools, match={"tool": "Bash"})
    p.intercept("context.compact", summarize)
"""


def trial(tmp_path, capsys, operations, *extra):
    plugin = tmp_path / "guard.py"
    plugin.write_text(PLUGIN)
    fixture = tmp_path / "operations.json"
    fixture.write_text(json.dumps(operations))
    code = main(["test", str(plugin), "--operations", str(fixture), "--project", str(tmp_path), *extra])
    return code, json.loads(capsys.readouterr().out)


def bash(command):
    return {"id": "c1", "tool": "Bash", "arguments": {"command": command}}


def test_effective_input_delivered_result_and_provenance_are_recorded(tmp_path, capsys):
    code, report = trial(
        tmp_path,
        capsys,
        [
            {"operation": "tool.call", "input": bash("ls"), "next": {"content": "a b", "status": "ok"}},
            {"operation": "tool.call", "input": bash("rm -rf x")},
            {"operation": "context.compact", "input": {"text": "user: ship it", "trigger": "manual"}},
        ],
    )
    assert code == 0, report["error"]
    rewrite, refusal, summary = report["operations"]
    assert rewrite["effective"]["arguments"]["command"] == "ls --color=never"
    assert rewrite["result"] == {"type": "tool_result", "content": "A B", "status": "ok"} and rewrite["shaped"] == ["guard/tool.call"]
    assert refusal["result"] == {"type": "refusal", "reason": "no deletes"} and refusal["effective"] is None
    assert summary["result"]["text"] == "digest: user: ship it" and summary["origin"] == "guard/context.compact"


@pytest.mark.parametrize(
    "entry, needle",
    [
        ({"operation": "tool.call", "input": bash("ls")}, "gives no next result"),
        ({"operation": "tool.call", "input": bash("rm x"), "next": {"content": "x"}}, "without calling next()"),
        ({"operation": "tool.call", "input": bash("ls"), "next": {"error": "disk full"}}, "disk full"),
        ({"operation": "tool.run", "input": {}}, "Unknown operation"),
    ],
)
def test_missing_unused_and_failed_entries_fail_the_trial(tmp_path, capsys, entry, needle):
    code, report = trial(tmp_path, capsys, [entry])
    assert code == 1 and report["status"] == "failed" and needle in report["error"]


def test_nonmatching_operations_reach_the_scripted_core_directly(tmp_path, capsys):
    read = {"id": "r1", "tool": "Read", "arguments": {"path": "x"}}
    code, report = trial(tmp_path, capsys, [{"operation": "tool.call", "input": read, "next": {"content": "file"}}])
    assert code == 0 and report["operations"][0]["result"]["content"] == "file" and report["operations"][0]["origin"] == "core"


def test_validate_rejects_operation_fixtures(tmp_path):
    plugin = tmp_path / "guard.py"
    plugin.write_text(PLUGIN)
    with pytest.raises(SystemExit):
        main(["validate", str(plugin), "--operations", str(plugin)])
