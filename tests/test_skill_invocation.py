"""Starting a skill: `/name args`, argument placeholders, and `!`command`` context at load time."""

import asyncio
import time

import pytest
from agent_harness import session
from prompt_toolkit.document import Document

from wizolt.agent.context import ContextManager
from wizolt.agent.engine import Agent
from wizolt.base import SESSION_EVENT_KEY
from wizolt.skill.invocation import parse_command, substitute_arguments
from wizolt.skill.trust import ProjectTrust
from wizolt.tools import SkillTool
from wizolt.ui.cli import CommandLoop
from wizolt.utils.shellrun import run_shell


def _skill(tmp_path, name, frontmatter="", body="body"):
    folder = tmp_path / ".wizolt" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n{frontmatter}---\n{body}\n", encoding="utf-8")
    # Project skills that run commands wait for the repository to be trusted (test_skill_trust).
    ProjectTrust(str(tmp_path / "data" / ProjectTrust.FILE_NAME), str(tmp_path)).grant()


def _loop(s):
    return CommandLoop(Agent(s, output_fn=lambda _text: None), output_fn=lambda _text: None)


# --- arguments ---


def test_argument_placeholders():
    assert substitute_arguments("deploy $ARGUMENTS now", "staging eu") == "deploy staging eu now"
    assert substitute_arguments("to $0 in $1, $ARGUMENTS[1] again, $5 missing", 'staging "eu west"') == "to staging in eu west, eu west again,  missing"
    # No placeholder: the arguments still arrive.
    assert substitute_arguments("Just do it.", "fast") == "Just do it.\n\nARGUMENTS: fast"
    # No arguments: shell positionals in an existing skill are left alone.
    assert substitute_arguments("awk '{print $1}'", "") == "awk '{print $1}'"
    assert substitute_arguments("all: [$ARGUMENTS]", "") == "all: []"


def test_parse_command():
    assert parse_command("/deploy staging now") == ("deploy", "staging now")
    assert parse_command("/deploy") == ("deploy", "")
    assert parse_command("/review\nfocus on errors\nand tests") == ("review", "focus on errors\nand tests")
    assert parse_command("/usr/bin/env is broken") is None  # a path, not a command
    assert parse_command("deploy") is None


# --- the model's Skill tool ---


async def test_skill_tool_passes_arguments(tmp_path):
    _skill(tmp_path, "deploy", "argument-hint: <env>\n", "Deploy to $0.")
    output = await SkillTool(session(tmp_path), ["deploy", "staging"]).call()

    assert output.startswith('<Skill name="deploy" source="project" args="staging">')
    assert "Deploy to staging." in output


async def test_dynamic_commands_run_at_load_and_need_approval(tmp_path):
    _skill(tmp_path, "status", "", "Branch: !`echo main-$0`\nBroken: !`echo oops >&2; exit 3`")
    tool = SkillTool(session(tmp_path), ["status", "x"])

    assert tool.needs_confirmation()
    view = tool.approval_view()
    assert view is not None and view.text == "echo main-x\necho oops >&2; exit 3"  # as they will run
    output = await tool.call()
    assert "Branch: main-x" in output
    assert "Broken: [`echo oops >&2; exit 3` failed with exit code 3: oops]" in output


def test_plain_skill_loads_without_approval(tmp_path):
    _skill(tmp_path, "guide", "", "Read the conventions.")
    tool = SkillTool(session(tmp_path), ["guide"])

    assert not tool.needs_confirmation()
    assert tool.approval_view() is None


async def test_repeat_loads_collapse_only_when_identical(tmp_path):
    _skill(tmp_path, "deploy", "", "Deploy to $0.")
    s = session(tmp_path)
    staging = await SkillTool(s, ["deploy", "staging"]).call()
    prod = await SkillTool(s, ["deploy", "prod"]).call()
    messages = [{"role": "tool", "content": f"tr.{index} {body}"} for index, body in enumerate((staging, prod, staging), 1)]

    deduped = [message["content"] for message in ContextManager(s).dedup_skill_loads(messages)]
    assert "Deploy to staging." in deduped[0]
    assert "Deploy to prod." in deduped[1]  # other arguments: a different load, kept whole
    assert "repeat load of skill deploy; instructions shown earlier at tr.1" in deduped[2]


# --- the user's /name ---


async def test_slash_name_starts_a_turn_with_the_skill_loaded(tmp_path):
    _skill(tmp_path, "deploy", "argument-hint: <env>\n", "Deploy to $ARGUMENTS. Branch !`echo main`.")
    s = session(tmp_path)

    assert await _loop(s).command("/deploy staging") == (False, False)  # not handled: it is a turn
    blocks = await Agent(s, output_fn=lambda _text: None).mention_messages("/deploy staging")
    assert len(blocks) == 1
    assert blocks[0][SESSION_EVENT_KEY] == "skill_command"
    assert blocks[0]["content"].startswith('<Skill name="deploy" source="project" args="staging" invoked-by="user">')
    assert "Deploy to staging. Branch main." in blocks[0]["content"]


async def test_user_only_skill_starts_from_slash_name(tmp_path):
    _skill(tmp_path, "ship", "disable-model-invocation: true\n", "Ship it.")
    blocks = await Agent(session(tmp_path), output_fn=lambda _text: None).mention_messages("/ship")

    assert "Ship it." in blocks[0]["content"]


async def test_non_user_invocable_and_builtin_names_are_not_skill_commands(tmp_path):
    _skill(tmp_path, "background", "user-invocable: false\n", "Knowledge.")
    _skill(tmp_path, "status", "", "A skill named like a built-in.")
    s = session(tmp_path)
    loop = _loop(s)
    emitted = []
    loop.presentation.emit_turn = emitted.append

    assert await loop.command("/background") == (True, False)
    assert emitted[-1] == "Unknown command: /background"
    assert not loop.skill_command("/status")  # the built-in wins the name
    assert await Agent(s, output_fn=lambda _text: None).mention_messages("/background") == []


def test_slash_completion_offers_skill_commands(tmp_path):
    _skill(tmp_path, "deploy", "argument-hint: <env>\n")
    _skill(tmp_path, "digest")
    _skill(tmp_path, "background", "user-invocable: false\n")
    _skill(tmp_path, "status")
    completer = _loop(session(tmp_path)).input_completer

    rows = {row.text: row.display_meta_text for row in completer.get_completions(Document("/d"), None)}
    assert rows["/deploy "] == "<env>"  # takes arguments: Enter opens them
    assert rows["/digest"] == "skill"
    assert "/diff" in rows
    assert not any(text.startswith("/background") for text in completer_texts(completer, "/b"))
    assert completer_texts(completer, "/status") == ["/status"]  # the built-in, once


def completer_texts(completer, text):
    return [row.text for row in completer.get_completions(Document(text), None)]


# --- running the commands ---


async def test_shell_timeout_kills_the_command(tmp_path):
    started = time.monotonic()
    result = await run_shell("sleep 30", cwd=str(tmp_path), timeout=0.2)

    assert result.timed_out
    assert result.stderr == "timed out after 0.2s"
    assert time.monotonic() - started < 5


async def test_cancelled_shell_leaves_nothing_running(tmp_path):
    marker = tmp_path / "survived"
    task = asyncio.ensure_future(run_shell(f"sleep 0.5; touch {marker}", cwd=str(tmp_path), timeout=30))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.8)

    assert not marker.exists()


async def test_shell_stdin_and_environment(tmp_path):
    result = await run_shell('read line; echo "$line/$EXTRA"', cwd=str(tmp_path), timeout=5, stdin="in\n", env={"EXTRA": "env"})

    assert (result.exit_code, result.stdout) == (0, "in/env\n")
