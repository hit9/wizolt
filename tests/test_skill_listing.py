"""Skills that appear mid-session reach the model by an appended message, never by rewriting the
prefix: the SKILLS index and the Skill tool stay frozen until the conversation's head is rebuilt."""

from agent_harness import session

from wizolt.agent.context import ContextManager
from wizolt.agent.engine import Agent
from wizolt.agent.prompts import SYSTEM_PROMPT
from wizolt.agent.runner import ToolRunner
from wizolt.base import SESSION_EVENT_KEY, ToolCall
from wizolt.tools import Tool
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli.commands import skills_command


def _skill(root, name, description="d"):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nbody of {name}\n", encoding="utf-8")


def _prefix(s):
    return [message["content"] for message in ContextManager(s).model_messages(SYSTEM_PROMPT) if str(message["content"]).startswith("--- SKILLS ---")]


def _tools(s):
    return [schema["function"]["name"] for schema in Tool.resolved_schemas(s)]


async def test_a_new_skill_is_announced_once_and_the_prefix_stays(tmp_path):
    skills_dir = tmp_path / ".wizolt" / "skills"
    _skill(skills_dir, "first", "the first")
    s = session(tmp_path)
    agent = Agent(s, output_fn=lambda _text: None)
    prefix, tools = _prefix(s), _tools(s)

    _skill(skills_dir, "second", "the second")
    [announcement] = await agent.skill_announcement()

    assert announcement[SESSION_EVENT_KEY] == "new_skills"
    assert announcement["content"].startswith("--- NEW SKILLS ---")
    assert "- second [project]: the second" in announcement["content"]
    assert _prefix(s) == prefix and _tools(s) == tools  # byte-identical: the cache holds
    assert await agent.skill_announcement() == []  # once
    assert s.skills.get("second") is not None  # and loadable now


async def test_compaction_folds_announced_skills_into_the_index(tmp_path):
    skills_dir = tmp_path / ".wizolt" / "skills"
    _skill(skills_dir, "first")
    s = session(tmp_path)
    agent = Agent(s, output_fn=lambda _text: None)
    _prefix(s)
    _skill(skills_dir, "second")
    await agent.skill_announcement()

    agent.context.apply_compaction(None, [])

    assert "- second [project]" in _prefix(s)[0]
    _skill(skills_dir, "third")
    assert "third" in (await agent.skill_announcement())[0]["content"]  # later arrivals: announced again


async def test_session_without_skill_tool_does_not_grow_one(tmp_path, without_builtin_skills):
    s = session(tmp_path)
    agent = Agent(s, output_fn=lambda _text: None)
    assert "Skill" not in _tools(s)

    _skill(tmp_path / ".wizolt" / "skills", "late")

    assert await agent.skill_announcement() == []  # nothing the model could load it with
    assert "Skill" not in _tools(s)
    assert s.skills.command("/late") is not None  # the user can still start it


async def test_opening_a_file_in_a_package_brings_its_skills(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _skill(repo / ".wizolt" / "skills", "root-skill")
    _skill(repo / ".wizolt" / "skills", "shared", "from the root")
    package = repo / "packages" / "web"
    _skill(package / ".claude" / "skills", "web-e2e", "run the web tests")
    _skill(package / ".claude" / "skills", "shared", "from the package")
    (package / "app.js").write_text("x\n", encoding="utf-8")
    s = session(repo)
    agent = Agent(s, output_fn=lambda _text: None)
    _prefix(s)
    assert s.skills.get("web-e2e") is None

    runner = ToolRunner(s, ContextManager(s), output_fn=lambda _text: None)
    await runner.run([ToolCall("r", "Read", [{"path": "packages/web/app.js", "ranges": [[1, 0]]}], payload={"path": "packages/web/app.js"})])
    [announcement] = await agent.skill_announcement()

    assert "- web-e2e [project]: run the web tests" in announcement["content"]
    assert s.skills.get("web-e2e").location == "packages/web/.claude/skills/web-e2e"
    assert s.skills.get("shared").description == "from the root"  # a package only adds skills


def test_reload_reports_what_changed(tmp_path):
    skills_dir = tmp_path / ".wizolt" / "skills"
    _skill(skills_dir, "stays")
    _skill(skills_dir, "goes")
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda _text: None), output_fn=lambda _text: None)

    assert skills_command(loop, "reload") == "Reloaded skills. Nothing changed."
    _skill(skills_dir, "arrives")
    (skills_dir / "goes" / "SKILL.md").unlink()
    assert skills_command(loop, "reload") == "Reloaded skills. Enabled: `arrives`. Disabled: `goes`."
