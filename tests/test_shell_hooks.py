"""Shell hooks: config parsing, the tool-call events in the runner, and the turn events in the engine.

Hooks are real shell commands here: the subprocess is the boundary under test, and the contract
(stdin JSON, exit 2, stdout JSON) is what a hook written for Claude Code relies on.
"""

import json
import re

import pytest
from agent_harness import call, session

from wizolt.agent.context import ContextManager
from wizolt.agent.engine import Agent
from wizolt.agent.runner import ToolRunner
from wizolt.base import ConfigError, ToolCall
from wizolt.shellhooks import HookCommand, PromptBlocked, ShellHooks


def _hooks(s, table):
    s.shell_hooks = ShellHooks(HookCommand.parse_table(table), str(s.cwd))
    return s


def _hook(event, command, matcher=""):
    return {event: [{"matcher": matcher, "hooks": [{"type": "command", "command": command}]}]}


def _runner(s, outputs=None, answer="y"):
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return answer

    runner = ToolRunner(s, ContextManager(s), input_fn=ask, output_fn=lambda text: (outputs if outputs is not None else []).append(str(text)))
    return runner, asked


def _bash(command):
    return ToolCall("bash-id", "Bash", [command], payload={"command": command})


# --- configuration ---


def test_parse_claude_code_shape():
    hooks = HookCommand.parse_table(
        {
            "PreToolUse": [{"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": "guard.sh", "timeout": 5}]}],
            "Stop": [{"hooks": [{"command": "check.sh"}]}],
        }
    )

    assert [(hook.event, hook.command, hook.matcher, hook.timeout) for hook in hooks] == [
        ("PreToolUse", "guard.sh", "Bash|Edit", 5.0),
        ("Stop", "check.sh", "", 60.0),
    ]
    assert hooks[0].matches("Bash") and hooks[0].matches("Edit")
    assert not hooks[0].matches("BashOutput")  # the whole name, not a substring


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"SessionStart": []}, "unsupported event 'SessionStart'"),
        ({"PreToolUse": {"hooks": []}}, "must be a list of matcher groups"),
        ({"PreToolUse": [{"matcher": "Bash"}]}, "each entry needs a `hooks` list"),
        ({"PreToolUse": [{"matcher": "(", "hooks": []}]}, "invalid matcher '('"),
        ({"PreToolUse": [{"hooks": [{"type": "prompt", "command": "x"}]}]}, 'only `type = "command"`'),
        ({"PreToolUse": [{"hooks": [{"command": " "}]}]}, "non-empty `command`"),
        ({"PreToolUse": [{"hooks": [{"command": "x", "timeout": 0}]}]}, "positive number of seconds"),
    ],
)
def test_invalid_hooks_are_config_errors(table, message):
    with pytest.raises(ConfigError, match=re.escape(message)):
        HookCommand.parse_table(table)


def test_a_malformed_config_hook_stops_session_assembly(tmp_path):
    from wizolt.agent.lifecycle import create_session

    config = tmp_path / "config.toml"
    config.write_text(f'[paths]\ndata_dir = "{tmp_path}"\n[[hooks.PreToolUse]]\nmatcher = "Bash"\nhooks = [{{ command = "" }}]\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="non-empty `command`"):
        create_session(path=str(config))


# --- tool events ---


async def test_pre_tool_use_exit_2_blocks_the_call(tmp_path):
    s = _hooks(session(tmp_path), _hook("PreToolUse", "echo 'no deletes here' >&2; exit 2", "Bash"))
    s.settings.yolo = True
    runner, _ = _runner(s)

    [message] = await runner.run([_bash("touch ran")])

    assert not (tmp_path / "ran").exists()
    assert "ToolError: blocked by PreToolUse hook: no deletes here" in message["content"]


async def test_hook_reads_claude_code_payload_and_environment(tmp_path):
    seen = tmp_path / "seen.json"
    s = _hooks(session(tmp_path), _hook("PreToolUse", f'cat > {seen}; echo "$CLAUDE_PROJECT_DIR" > {tmp_path}/dir'))
    s.settings.yolo = True
    runner, _ = _runner(s)

    await runner.run([_bash("true")])

    payload = json.loads(seen.read_text(encoding="utf-8"))
    assert payload["hook_event_name"] == "PreToolUse"
    assert payload["tool_name"] == "Bash"
    assert payload["tool_input"] == {"command": "true"}  # the model's own arguments
    assert payload["cwd"] == str(tmp_path)
    assert (tmp_path / "dir").read_text(encoding="utf-8").strip() == str(tmp_path)


async def test_matcher_limits_which_tools_a_hook_sees(tmp_path):
    s = _hooks(session(tmp_path), _hook("PreToolUse", "exit 2", "Edit"))
    s.settings.yolo = True
    runner, _ = _runner(s)

    [message] = await runner.run([_bash("touch ran")])

    assert (tmp_path / "ran").exists()
    assert "blocked" not in message["content"]


async def test_permission_allow_skips_and_ask_forces_the_prompt(tmp_path):
    allow = '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow"}}'
    s = _hooks(session(tmp_path), _hook("PreToolUse", f"echo '{allow}'"))
    runner, asked = _runner(s)
    await runner.run([_bash("touch allowed")])
    assert asked == [] and (tmp_path / "allowed").exists()

    ask = '{"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask"}}'
    s = _hooks(session(tmp_path), _hook("PreToolUse", f"echo '{ask}'"))
    s.settings.yolo = True
    runner, asked = _runner(s, answer="n")
    await runner.run([_bash("touch asked")])
    assert len(asked) == 1 and not (tmp_path / "asked").exists()


async def test_json_deny_blocks_with_its_reason(tmp_path):
    deny = '{"hookSpecificOutput": {"permissionDecision": "deny", "permissionDecisionReason": "use rg"}}'
    s = _hooks(session(tmp_path), _hook("PreToolUse", f"echo '{deny}'"))
    runner, _ = _runner(s)

    [message] = await runner.run([_bash("grep -r x .")])

    assert "blocked by PreToolUse hook: use rg" in message["content"]


async def test_post_tool_use_feedback_reaches_the_model_and_the_user(tmp_path):
    context = '{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "lint is clean"}}'
    s = session(tmp_path)
    hooks = [_hook("PostToolUse", f"echo '{context}'"), _hook("PostToolUse", "echo 'formatted the file' >&2; exit 2")]
    s.shell_hooks = ShellHooks(tuple(hook for table in hooks for hook in HookCommand.parse_table(table)), str(tmp_path))
    s.settings.yolo = True
    outputs = []
    runner, _ = _runner(s, outputs)

    [message] = await runner.run([_bash("echo done")])

    assert "done" in message["content"]
    assert message["content"].endswith("PostToolUse hook feedback:\nformatted the file\nlint is clean")
    assert any("formatted the file" in text for text in outputs)


async def test_broken_hook_is_shown_to_the_user_only(tmp_path):
    s = _hooks(session(tmp_path), _hook("PreToolUse", "echo 'missing dependency' >&2; exit 1"))
    s.settings.yolo = True
    outputs = []
    runner, _ = _runner(s, outputs)

    [message] = await runner.run([_bash("touch ran")])

    assert (tmp_path / "ran").exists()  # a broken hook does not block
    assert "missing dependency" not in message["content"]
    assert any("exited 1: missing dependency" in text for text in outputs)


async def test_hooked_read_calls_leave_the_parallel_path(tmp_path):
    (tmp_path / "a.txt").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b\n", encoding="utf-8")
    log = tmp_path / "log"
    s = _hooks(session(tmp_path), _hook("PreToolUse", f"echo x >> {log}", "Read"))
    runner, _ = _runner(s)

    assert not runner.parallel_safe(call("Read", [{"path": "a.txt"}]))
    await runner.run([call("Read", [{"path": "a.txt"}]), call("Read", [{"path": "b.txt"}])])

    assert log.read_text(encoding="utf-8").count("x") == 2  # both calls passed through the hook


# --- turn events ---


def _agent(tmp_path, table, replies):
    from test_session_persistence import session_with_data_dir

    s = _hooks(session_with_data_dir(tmp_path), table)
    agent = Agent(s, output_fn=lambda _text: None)
    requests = []

    async def request(messages, tools):
        requests.append(messages)
        content = replies[len(requests) - 1]
        return {"role": "assistant", "content": content}, [], content

    agent.model.request = request
    return agent, requests


async def test_user_prompt_submit_exit_2_refuses_the_turn(tmp_path):
    agent, requests = _agent(tmp_path, _hook("UserPromptSubmit", "echo 'no secrets in prompts' >&2; exit 2"), ["unused"])

    with pytest.raises(PromptBlocked, match="no secrets in prompts"):
        await agent.run("my password is hunter2")

    assert requests == []
    assert agent.session.messages == []


async def test_user_prompt_submit_stdout_is_context(tmp_path):
    agent, requests = _agent(tmp_path, _hook("UserPromptSubmit", "echo 'today is release day'"), ["ok"])

    await agent.run("hello")

    contents = [str(message.get("content")) for message in requests[0]]
    assert "--- HOOK CONTEXT ---\ntoday is release day" in contents


async def test_stop_hook_sends_the_model_back_once(tmp_path):
    # Blocks while stop_hook_active is false, as a Claude Code Stop hook guarding a loop would.
    script = "grep -q '\"stop_hook_active\": true' && exit 0; echo 'tests were not run' >&2; exit 2"
    agent, requests = _agent(tmp_path, _hook("Stop", script), ["all done", "ran the tests, all done"])

    answer = await agent.run("fix it")

    assert answer == "ran the tests, all done"
    assert len(requests) == 2
    assert "Stop hook: tests were not run" in [str(message.get("content")) for message in requests[1]]


async def test_a_stop_hook_that_never_lets_go_is_capped(tmp_path):
    replies = [f"answer {index}" for index in range(2 * (Agent.MAX_STOP_REDIRECTS + 1))]
    agent, requests = _agent(tmp_path, _hook("Stop", "echo 'not yet' >&2; exit 2"), replies)
    notices = []
    agent.output_fn = notices.append

    answer = await agent.run("finish")

    assert len(requests) == Agent.MAX_STOP_REDIRECTS + 1  # one answer, then one per redirect
    assert answer == f"answer {Agent.MAX_STOP_REDIRECTS}"
    assert any("Stop hooks sent the agent back 5 times" in str(text) for text in notices)
    assert agent._stop_redirects == Agent.MAX_STOP_REDIRECTS
    await agent.run("again")  # a new turn starts with a fresh budget
    assert len(requests) == 2 * (Agent.MAX_STOP_REDIRECTS + 1)


async def test_stop_hook_also_guards_a_next_hints_ending(tmp_path):
    from wizolt.base import ToolCall as Call

    script = "grep -q '\"stop_hook_active\": true' && exit 0; echo 'one more check' >&2; exit 2"
    agent, requests = _agent(tmp_path, _hook("Stop", script), [])
    hints = Call("h", "NextHints", [{"hints": ["run tests"]}], payload={"hints": ["run tests"]})
    script_replies = [({"role": "assistant", "content": "done"}, [hints], "done"), ({"role": "assistant", "content": "checked"}, [], "checked")]

    async def request(messages, tools):
        requests.append(messages)
        return script_replies[len(requests) - 1]

    agent.model.request = request
    answer = await agent.run("finish")

    assert answer == "checked"
    assert "Stop hook: one more check" in [str(message.get("content")) for message in requests[1]]


async def test_hook_that_cannot_start_is_reported_and_ignored(tmp_path):
    s = _hooks(session(tmp_path), _hook("PreToolUse", "true"))
    s.settings.yolo = True
    outputs = []
    runner, _ = _runner(s, outputs)
    s.cwd = str(tmp_path / "removed")  # the hook's working directory is gone

    [message] = await runner.run([ToolCall("r", "Read", [{"path": str(tmp_path / "x")}], payload={"path": str(tmp_path / "x")})])

    assert "blocked" not in message["content"]
    assert any("exited 127: cannot start:" in text for text in outputs)


async def test_hook_that_ignores_a_large_stdin_is_fine(tmp_path):
    s = _hooks(session(tmp_path), _hook("PostToolUse", "exit 0"))
    s.settings.yolo = True
    runner, _ = _runner(s)

    [message] = await runner.run([_bash("head -c 300000 /dev/zero | tr '\\0' y")])

    assert "yyyy" in message["content"]


def test_status_counts_active_hooks(tmp_path):
    from wizolt.ui.cli import CommandLoop
    from wizolt.ui.cli.commands import status

    s = _hooks(session(tmp_path), {**_hook("PreToolUse", "true"), **_hook("Stop", "true")})
    loop = CommandLoop(Agent(s, output_fn=lambda _text: None), output_fn=lambda _text: None)

    assert "hooks `2`" in status(loop, "")


def test_worker_keeps_tool_hooks_only(tmp_path):
    table = {**_hook("PreToolUse", "true"), **_hook("Stop", "true"), **_hook("UserPromptSubmit", "true")}
    worker = ShellHooks(HookCommand.parse_table(table), str(tmp_path)).detached()

    assert [hook.event for hook in worker.active(session(tmp_path))] == ["PreToolUse"]
