"""--operations: real handlers, the live chain executor and a scripted core; nothing real runs."""

import json
import time

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


ADAPTERS = """
from wizolt.sdk.operations import ModelResponse, ModelToolCall
SDK_VERSION = 1
def setup(p):
    async def prompt(ctx, value, next):
        return await next(value.replace(attachments=value.attachments + ("smuggled.png",)))
    async def model(ctx, request, next):
        return ModelResponse("done", (ModelToolCall("t1", "Bash", {}),))
    async def compose(ctx, blocks, next):
        return await next(blocks.add("notes", "remember the deadline"))
    p.intercept("prompt.submit", prompt)
    p.intercept("model.request", model, response="replace")
    p.intercept("context.compose", compose)
"""


@pytest.mark.parametrize(
    "entry, needle",
    [
        ({"operation": "prompt.submit", "input": {"text": "hi", "attachments": ["a.png"]}, "next": {"text": "hi"}}, "never add them"),
        ({"operation": "model.request", "input": {"id": "m1", "purpose": "turn", "tools": ["Read"]}}, "does not offer: Bash"),
    ],
)
def test_trials_apply_the_live_adapter_rules(tmp_path, capsys, entry, needle):
    plugin = tmp_path / "adapters.py"
    plugin.write_text(ADAPTERS)
    fixture = tmp_path / "operations.json"
    fixture.write_text(json.dumps([entry]))
    code = main(["test", str(plugin), "--operations", str(fixture), "--project", str(tmp_path)])
    report = json.loads(capsys.readouterr().out)
    assert code == 1 and needle in report["error"]


def test_trials_place_plugin_blocks_as_a_live_request_does(tmp_path, capsys):
    plugin = tmp_path / "adapters.py"
    plugin.write_text(ADAPTERS)
    blocks = [{"id": "system", "text": "sys", "editable": True}, {"id": "conversation", "text": "user: hi"}]
    fixture = tmp_path / "operations.json"
    fixture.write_text(json.dumps([{"operation": "context.compose", "input": {"blocks": blocks}, "next": {"blocks": blocks}}]))
    code = main(["test", str(plugin), "--operations", str(fixture), "--project", str(tmp_path)])
    report = json.loads(capsys.readouterr().out)
    assert code == 0, report["error"]
    # Namespaced and placed between the header and the conversation, as the host renders it.
    ids = [block["id"] for block in report["operations"][0]["effective"]["blocks"]]
    assert ids == ["system", "plugin:adapters:notes", "conversation"]


def test_validate_rejects_operation_fixtures(tmp_path):
    plugin = tmp_path / "guard.py"
    plugin.write_text(PLUGIN)
    with pytest.raises(SystemExit):
        main(["validate", str(plugin), "--operations", str(plugin)])


def test_timeout_bounds_intercept_handlers(tmp_path, capsys):
    """--timeout is the deadline for each worker call, intercept handlers included: a handler
    that never returns fails the trial at the deadline instead of hanging to the class default."""
    plugin = tmp_path / "slow.py"
    plugin.write_text(
        "import asyncio\n"
        "SDK_VERSION = 1\n"
        "def setup(p):\n"
        "    async def slow(ctx, call, next):\n"
        "        await asyncio.sleep(30)\n"
        "    p.intercept('tool.call', slow)\n"
    )
    fixture = tmp_path / "operations.json"
    fixture.write_text(json.dumps([{"operation": "tool.call", "input": bash("ls"), "next": {"content": "a"}}]))
    started = time.monotonic()
    code = main(["test", str(plugin), "--operations", str(fixture), "--project", str(tmp_path), "--timeout", "0.5"])
    report = json.loads(capsys.readouterr().out)
    assert code == 1 and "timed out" in report["error"] and time.monotonic() - started < 10
