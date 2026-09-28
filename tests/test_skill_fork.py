"""`context: fork`: a skill that runs in the Delegate worker, returning only the worker's report."""

from test_worker_handoff import FakeModelClient, _delegate_runner, _delegate_session

from wizolt.agent.engine import Agent
from wizolt.base import ToolCall
from wizolt.tools import WORKER_TOOLS, SkillTool

FORKED = "context: fork\n"


def _user_skill(home, name, frontmatter, body="Audit every dependency."):
    folder = home / ".claude" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}\n{frontmatter}---\n{body}\n", encoding="utf-8")


def _parent(tmp_path):
    parent = _delegate_session(tmp_path)
    parent.worker_tool_enabled = True
    return parent


async def test_forked_skill_runs_in_the_worker_and_returns_its_report(tmp_path, isolate_home, monkeypatch):
    _user_skill(isolate_home, "audit", FORKED)
    parent = _parent(tmp_path)
    parent.settings.yolo = True
    model = FakeModelClient([({"role": "assistant", "content": "no vulnerable packages"}, [], "no vulnerable packages")])
    monkeypatch.setattr("wizolt.agent.engine.ModelClient", lambda session: model)
    asked = []
    runner = _delegate_runner(parent)
    runner.input_fn = lambda prompt: asked.append(prompt) or "y"

    [message] = await runner.run([ToolCall("s", "Skill", ["audit", "--prod"], payload={"name": "audit", "arguments": "--prod"})])

    assert len(asked) == 1  # a send is confirmed even under yolo, as Delegate's is
    order = model.requests[0][-1]["content"]
    assert 'call Skill(name="audit", arguments="--prod")' in order
    assert '<Delegate action="send"' in message["content"] and "no vulnerable packages" in message["content"]
    assert "Audit every dependency." not in message["content"]  # the instructions stayed in the worker
    assert parent.active_skills == []  # its hooks belong to the worker's session


def test_the_worker_itself_loads_a_forked_skill_inline(tmp_path, isolate_home):
    _user_skill(isolate_home, "audit", FORKED)
    worker = _parent(tmp_path)
    worker.tool_names = WORKER_TOOLS  # no Delegate: nothing to fork into

    assert not SkillTool(worker, ["audit"]).forks()


async def test_without_a_worker_a_forked_skill_loads_inline(tmp_path, isolate_home):
    from agent_harness import session

    _user_skill(isolate_home, "audit", FORKED)
    output = await SkillTool(session(tmp_path), ["audit"]).call()

    assert "Audit every dependency." in output


async def test_slash_name_asks_the_model_to_start_the_fork(tmp_path, isolate_home):
    _user_skill(isolate_home, "audit", FORKED)
    parent = _parent(tmp_path)

    [block] = await Agent(parent, output_fn=lambda _text: None).mention_messages("/audit prod")

    assert block["content"].startswith('<Skill name="audit" context="fork" invoked-by="user">')
    assert 'call Skill(name="audit", arguments="prod") now' in block["content"]
    assert "Audit every dependency." not in block["content"]


def test_unsupported_claude_code_fields_are_named(tmp_path, isolate_home):
    from agent_harness import session

    _user_skill(isolate_home, "picky", "context: sandbox\nagent: Explore\nmodel: opus\n")
    skill = session(tmp_path).skills.get("picky")

    assert not skill.fork
    assert set(skill.warnings) >= {
        "context 'sandbox' is not supported; the skill loads inline",
        "agent is not supported; the worker runs this skill",
        "model is not supported; the session's model runs this skill",
    }
