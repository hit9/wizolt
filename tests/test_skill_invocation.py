"""Starting a skill: `/name args`, argument placeholders, and `!`command`` context at load time."""

import asyncio
import os
import time

import pytest
from agent_harness import session
from prompt_toolkit.document import Document

from wizolt.agent.context import ContextManager
from wizolt.agent.engine import Agent
from wizolt.base import SESSION_EVENT_KEY
from wizolt.skill.invocation import Arguments, Invocation
from wizolt.skill.trust import ProjectTrust
from wizolt.tools import SkillTool
from wizolt.ui.cli import CommandLoop
from wizolt.utils.process import ShellCommand


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
    assert Arguments("staging eu").fill("deploy $ARGUMENTS now") == "deploy staging eu now"
    assert Arguments('staging "eu west"').fill("to $0 in $1, $ARGUMENTS[1] again, $5 missing") == "to staging in eu west, eu west again,  missing"
    # No placeholder: the arguments still arrive.
    assert Arguments("fast").fill("Just do it.") == "Just do it.\n\nARGUMENTS: fast"
    # No arguments: shell positionals in an existing skill are left alone.
    assert Arguments().fill("awk '{print $1}'") == "awk '{print $1}'"
    assert Arguments().fill("all: [$ARGUMENTS]") == "all: []"


def test_split_command():
    assert Invocation.split_command("/deploy staging now") == ("deploy", Arguments("staging now"))
    assert Invocation.split_command("/deploy") == ("deploy", Arguments())
    assert Invocation.split_command("/review\nfocus on errors\nand tests") == ("review", Arguments("focus on errors\nand tests"))
    assert Invocation.split_command("/usr/bin/env is broken") is None  # a path, not a command
    assert Invocation.split_command("deploy") is None


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
    result = await ShellCommand("sleep 30", str(tmp_path), timeout=0.2).run()

    assert result.timed_out
    assert result.stderr == "timed out after 0.2s"
    assert time.monotonic() - started < 5


async def test_cancelled_shell_leaves_nothing_running(tmp_path):
    pid_file = tmp_path / "pid"
    # A child in the command's group: the kill must reach past the shell itself.
    task = asyncio.ensure_future(ShellCommand(f"sleep 30 & echo $! > {pid_file}; wait", str(tmp_path), timeout=60).run())
    for _ in range(500):
        if pid_file.exists() and pid_file.read_text(encoding="utf-8").strip():
            break
        await asyncio.sleep(0.01)
    child = int(pid_file.read_text(encoding="utf-8"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(500):  # SIGKILL is delivered asynchronously; the orphan is reaped by init
        if not _alive(child):
            break
        await asyncio.sleep(0.01)
    assert not _alive(child)


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A killed orphan can linger as a zombie until init reaps it; a zombie runs nothing.
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


async def test_command_that_cannot_start_is_a_failed_result(tmp_path):
    result = await ShellCommand("true", str(tmp_path / "gone"), timeout=5).run()

    assert result.exit_code == ShellCommand.CANNOT_START
    assert result.stderr.startswith("cannot start:")


async def test_skill_command_output_is_capped(tmp_path):
    _skill(tmp_path, "dump", "", "Log:\n!`head -c 50000 /dev/zero | tr '\\0' x`\nEnd.")
    output = await SkillTool(session(tmp_path), ["dump"]).call()

    assert "x" * Invocation.MAX_COMMAND_OUTPUT in output
    assert "x" * (Invocation.MAX_COMMAND_OUTPUT + 1) not in output
    assert f"... ({50_000 - Invocation.MAX_COMMAND_OUTPUT} more characters cut)" in output
    assert output.rstrip().endswith("</Skill>") and "End." in output


async def test_skill_command_in_a_missing_directory_reports_instead_of_raising(tmp_path):
    _skill(tmp_path, "where", "", "Here: !`pwd`")
    s = session(tmp_path)
    s.cwd = str(tmp_path / "removed")

    output = await SkillTool(s, ["where"]).call()

    assert "Here: [`pwd` failed with exit code 127: cannot start:" in output


def test_exit_is_never_a_skill_command(tmp_path):
    _skill(tmp_path, "exit", "", "Not an exit.")
    _skill(tmp_path, "quit", "", "Not a quit.")
    loop = _loop(session(tmp_path))

    assert not loop.skill_command("/exit")
    assert not loop.skill_command("/quit")


async def test_shell_stdin_and_environment(tmp_path):
    result = await ShellCommand('read line; echo "$line/$EXTRA"', str(tmp_path), timeout=5, stdin="in\n", env={"EXTRA": "env"}).run()

    assert (result.exit_code, result.stdout) == (0, "in/env\n")
