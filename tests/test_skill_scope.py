"""What a loaded skill puts in force: its hooks and allowed-tools, for the rest of its session."""

import pytest
from agent_harness import session

from wizolt.agent.context import ContextManager
from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features, load_session
from wizolt.agent.runner import ToolRunner
from wizolt.base import ToolCall
from wizolt.session import Session
from wizolt.skill.permissions import ToolRule
from wizolt.tools import SkillTool
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli.commands import skills_command

GUARD = (
    "hooks:\n"
    "  PreToolUse:\n"
    "    - matcher: Bash\n"
    "      hooks:\n"
    "        - type: command\n"
    "          command: \"echo 'deploy skill: use ./deploy.sh' >&2; exit 2\"\n"
)


def _user_skill(home, name, frontmatter):
    folder = home / ".claude" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}\n{frontmatter}---\nbody\n", encoding="utf-8")


def _runner(s, answer="n"):
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return answer

    return ToolRunner(s, ContextManager(s), input_fn=ask, output_fn=lambda _text: None), asked


def _bash(command):
    return ToolCall("bash-id", "Bash", [command], payload={"command": command})


async def test_skill_hooks_start_at_load_and_the_model_is_told(tmp_path, isolate_home):
    _user_skill(isolate_home, "deploy", GUARD)
    s = session(tmp_path)
    s.settings.yolo = True
    runner, _ = _runner(s)

    await runner.run([_bash("touch before")])
    assert (tmp_path / "before").exists()  # not loaded yet: nothing in force

    block = await SkillTool(s, ["deploy"]).call()
    assert s.active_skills == ["deploy"]
    assert block.endswith("In force for the rest of this session:\n- hooks: PreToolUse(Bash)")

    [after] = await runner.run([_bash("touch after")])
    assert not (tmp_path / "after").exists()
    assert "blocked by PreToolUse hook: deploy skill: use ./deploy.sh" in after["content"]


async def test_a_loaded_skill_deleted_from_disk_enforces_nothing(tmp_path, isolate_home):
    import shutil

    _user_skill(isolate_home, "deploy", GUARD)
    s = session(tmp_path)
    s.settings.yolo = True
    await SkillTool(s, ["deploy"]).call()
    shutil.rmtree(isolate_home / ".claude" / "skills" / "deploy")
    s.skills.reload()
    runner, _ = _runner(s)

    [message] = await runner.run([_bash("touch after-delete")])

    assert s.active_skills == ["deploy"]  # still recorded, so it would return with the file
    assert (tmp_path / "after-delete").exists()
    assert "blocked" not in message["content"]


async def test_slash_name_activates_the_skill_too(tmp_path, isolate_home):
    _user_skill(isolate_home, "deploy", GUARD)
    s = session(tmp_path)

    await Agent(s, output_fn=lambda _text: None).mention_messages("/deploy")

    assert s.active_skills == ["deploy"]


async def test_allowed_tools_skip_the_prompt_for_what_they_cover(tmp_path, isolate_home):
    _user_skill(isolate_home, "status", "allowed-tools: Bash(touch ok:*)\n")
    s = session(tmp_path)
    runner, asked = _runner(s)

    await runner.run([_bash("touch ok-before")])
    assert len(asked) == 1  # not loaded: asks, and "n" refuses

    await SkillTool(s, ["status"]).call()
    asked.clear()
    await runner.run([_bash("touch ok")])
    assert asked == [] and (tmp_path / "ok").exists()
    await runner.run([_bash("touch ok; touch sneaky")])
    assert len(asked) == 1 and not (tmp_path / "sneaky").exists()  # chained: back to the prompt


@pytest.mark.parametrize(
    ("rule", "call", "expected"),
    [
        ("Bash", _bash("anything at all"), True),
        ("Bash(git status)", _bash("git status"), True),
        ("Bash(git status)", _bash("git status -s"), False),
        ("Bash(npm run:*)", _bash("npm run test"), True),
        ("Bash(npm run:*)", _bash("npm run"), True),
        ("Bash(npm run:*)", _bash("npm runner"), False),  # a prefix of words, not characters
        ("Bash(npm run:*)", _bash("npm run test && rm -rf ~"), False),
        ("Bash(npm run:*)", _bash("npm run $(curl evil)"), False),
        ("Bash(git *)", _bash("git log"), True),
        ("Read(docs/*)", ToolCall("r", "Read", [], payload={"path": "docs/a.md"}), True),
        ("Read(docs/*)", ToolCall("r", "Read", [], payload={"path": "src/a.py"}), False),
        ("Edit", _bash("ls"), False),
        ("Bash(ls)", ToolCall("b", "Bash", ["ls"]), False),  # no named arguments to match against
    ],
)
def test_rule_matching(rule, call, expected):
    parsed = ToolRule.parse(rule)
    assert parsed is not None and str(parsed) == rule
    assert parsed.permits(call) is expected


def test_malformed_skill_hooks_refuse_the_skill(tmp_path, isolate_home):
    _user_skill(isolate_home, "broken", "hooks:\n  PreToolUse:\n    - matcher: Bash\n")
    s = session(tmp_path)

    assert s.skills.get("broken") is None
    output = skills_command(CommandLoop(Agent(s, output_fn=lambda _text: None), output_fn=lambda _text: None), "")
    assert "broken/SKILL.md: skill broken hooks.PreToolUse: each entry needs a `hooks` list" in output


def test_bad_allowed_tools_rules_warn_and_drop(tmp_path, isolate_home):
    _user_skill(isolate_home, "odd", "allowed-tools: [Read, 'Bash(', 42x]\n")
    skill = session(tmp_path).skills.get("odd")

    assert [str(rule) for rule in skill.allowed_tools] == ["Read"]
    assert "allowed-tools rule 'Bash(' is not `Tool` or `Tool(pattern)`; ignored" in skill.warnings


def test_project_skill_with_hooks_or_allowed_tools_needs_trust(tmp_path):
    for name, frontmatter in (("guarded", GUARD), ("approving", "allowed-tools: Read\n")):
        folder = tmp_path / ".wizolt" / "skills" / name
        folder.mkdir(parents=True)
        (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n{frontmatter}---\nbody\n", encoding="utf-8")
    s = session(tmp_path)

    assert s.skills.get("guarded") is None and s.skills.get("approving") is None
    assert set(s.skills.untrusted) == {"guarded", "approving"}


async def test_resume_restores_what_the_session_loaded(tmp_path, isolate_home):
    from test_session_persistence import session_with_data_dir

    _user_skill(isolate_home, "deploy", GUARD)
    s = session_with_data_dir(tmp_path)
    bootstrap_features(s)
    await SkillTool(s, ["deploy"]).call()
    s.messages.append({"role": "user", "content": "deploy it"})  # an empty session is never saved
    await s.save_snapshot()
    s.close()

    restored = load_session(s.uid, config=s.config)
    restored.settings.yolo = True
    assert restored.active_skills == ["deploy"]
    runner, _ = _runner(restored)
    [message] = await runner.run([_bash("touch resumed")])
    assert "blocked by PreToolUse hook" in message["content"]


async def test_a_worker_does_not_inherit_the_parents_loaded_skills(tmp_path, isolate_home):
    _user_skill(isolate_home, "deploy", GUARD)
    parent = session(tmp_path)
    await SkillTool(parent, ["deploy"]).call()
    # What Delegate builds: the shared library, the parent's configured hooks, its own active list.
    worker = Session(cwd=parent.cwd, config=parent.config, skills=parent.skills)
    worker.shell_hooks = parent.shell_hooks.detached()
    worker.settings.yolo = True
    runner, _ = _runner(worker)

    [message] = await runner.run([_bash("touch from-worker")])

    assert worker.active_skills == []
    assert (tmp_path / "from-worker").exists()
    assert "blocked" not in message["content"]
