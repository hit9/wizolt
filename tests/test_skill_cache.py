"""The prompt-cache gate for skills, hooks and AGENTS.md: nothing they do mid-session rewrites a
prefix the provider already cached.

Each scenario runs the real agent and ModelClient against the test-only OpenAI behavior model
(DESIGN.md, "Cache test boundary") and checks every request against the previous one: the tool
block is byte-identical, the previous request's items are an exact prefix of the next, and the
provider reports a cache read. A change that reshapes the prefix fails here, not in production.
"""

import pytest
from openai_mock_server import OpenAIMockServer

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features
from wizolt.config import Config, ProviderConfig
from wizolt.model import ModelClient
from wizolt.session import Session
from wizolt.skill.trust import ProjectTrust
from wizolt.ui.cli.commands import skills_command


def _skill(root, name, description="d", extra="", body="body"):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n{extra}---\n{body}\n", encoding="utf-8")


def _session(tmp_path, api, hooks=None):
    config = Config()
    config.data_dir = str(tmp_path / "data")
    config.providers = {"default": ProviderConfig(url="http://test", key="sk-test", model="gpt-5.6", api=api, reasoning="medium", stream=False)}
    config.hooks = hooks or {}
    session = Session(cwd=str(tmp_path), config=config)
    session.settings.yolo = True
    bootstrap_features(session)
    return session


def _agent(session, monkeypatch, answers):
    server = OpenAIMockServer(answers)
    monkeypatch.setattr(ModelClient, "client", lambda self, **kwargs: server.client())
    return Agent(session, output_fn=lambda _text: None), server


def _assert_append_only(server, api):
    key = "input" if api == "responses" else "messages"
    for index in range(1, len(server.requests)):
        before, after = server.requests[index - 1], server.requests[index]
        assert after.get("tools") == before.get("tools"), f"request {index} reshaped the tool block"
        assert after[key][: len(before[key])] == before[key], f"request {index} rewrote an already-sent prefix"
        assert server.cache_events[index][1] > 0, f"request {index} read nothing from the cache"


@pytest.mark.parametrize("api", ["chat", "responses"])
async def test_skills_changing_on_disk_never_rewrite_the_prefix(tmp_path, monkeypatch, api):
    skills = tmp_path / ".wizolt" / "skills"
    _skill(skills, "first", "the first")
    _skill(skills, "doomed", "will be deleted")
    session = _session(tmp_path, api)
    agent, server = _agent(session, monkeypatch, ["one", "two", "three", "four"])

    await agent.run("start")
    _skill(skills, "second", "installed mid-session")
    _skill(skills, "first", "description edited mid-session")
    await agent.run("after an install and an edit")
    (skills / "doomed" / "SKILL.md").unlink()
    await agent.run("after a removal")
    _skill(skills, "runner", extra="", body="Now: !`date`")  # held back until trusted
    skills_command_loop = type("Loop", (), {"session": session})()
    assert skills_command(skills_command_loop, "trust").endswith("Enabled: `runner`.")
    await agent.run("after trusting the repository")

    _assert_append_only(server, api)
    announced = [item for item in server.requests[-1]["input" if api == "responses" else "messages"] if "--- NEW SKILLS ---" in str(item)]
    assert len(announced) == 2  # `second`, then `runner`: each told once, appended where it arrived


@pytest.mark.parametrize("api", ["chat", "responses"])
async def test_a_session_without_skills_keeps_its_tool_block_when_one_appears(tmp_path, monkeypatch, api):
    session = _session(tmp_path, api)
    agent, server = _agent(session, monkeypatch, ["one", "two"])

    await agent.run("start")
    _skill(tmp_path / ".wizolt" / "skills", "late")
    await agent.run("a skill was installed")

    _assert_append_only(server, api)
    assert all(tool.get("name", tool.get("function", {}).get("name")) != "Skill" for tool in server.requests[-1]["tools"])


@pytest.mark.parametrize("api", ["chat", "responses"])
async def test_loading_and_starting_skills_only_append(tmp_path, monkeypatch, api):
    skills = tmp_path / ".wizolt" / "skills"
    _skill(skills, "guide", body="Follow the guide for $0.")
    _skill(skills, "deploy", extra="argument-hint: <env>\n", body="Deploy to $ARGUMENTS.")
    ProjectTrust(str(tmp_path / "data" / ProjectTrust.FILE_NAME), str(tmp_path)).grant()
    session = _session(tmp_path, api)
    agent, server = _agent(
        session,
        monkeypatch,
        [{"tool": "Skill", "arguments": {"name": "guide", "arguments": "tests"}}, "guided", "deployed", "mentioned"],
    )

    await agent.run("use the guide")
    await agent.run("/deploy staging")
    await agent.run("remember $guide")

    _assert_append_only(server, api)


@pytest.mark.parametrize("api", ["chat", "responses"])
async def test_hooks_only_append(tmp_path, monkeypatch, api):
    hooks = {
        "UserPromptSubmit": [{"hooks": [{"command": "echo 'context from a hook'"}]}],
        "PostToolUse": [{"matcher": "Bash", "hooks": [{"command": "echo 'checked' >&2; exit 2"}]}],
        "Stop": [{"hooks": [{"command": "grep -q '\"stop_hook_active\": true' && exit 0; echo 'run the tests' >&2; exit 2"}]}],
    }
    session = _session(tmp_path, api, hooks)
    agent, server = _agent(session, monkeypatch, [{"tool": "Bash", "arguments": {"command": "true"}}, "done early", "done"])

    await agent.run("do it")

    _assert_append_only(server, api)
    assert len(server.requests) == 3  # the Stop hook sent the model back once


@pytest.mark.parametrize("api", ["chat", "responses"])
async def test_a_package_skill_found_mid_session_only_appends(tmp_path, monkeypatch, api):
    (tmp_path / ".git").mkdir()
    _skill(tmp_path / ".wizolt" / "skills", "root-skill")
    package = tmp_path / "packages" / "web"
    _skill(package / ".claude" / "skills", "web-e2e")
    (package / "app.js").write_text("x\n", encoding="utf-8")
    session = _session(tmp_path, api)
    agent, server = _agent(session, monkeypatch, [{"tool": "Read", "arguments": {"path": "packages/web/app.js"}}, "read it", "next"])

    await agent.run("look at the web app")
    await agent.run("carry on")

    _assert_append_only(server, api)
    assert "web-e2e" in str(server.requests[-1])


@pytest.mark.parametrize("api", ["chat", "responses"])
async def test_compaction_keeps_the_header_when_no_skill_changed(tmp_path, monkeypatch, api):
    _skill(tmp_path / ".wizolt" / "skills", "guide")
    (tmp_path / "AGENTS.md").write_text("# Rules\nbe careful\n", encoding="utf-8")
    session = _session(tmp_path, api)
    agent, server = _agent(session, monkeypatch, ["one", "two"])

    await agent.run("start")
    header_before = [item for item in server.requests[0]["input" if api == "responses" else "messages"][:4]]
    agent.context.apply_compaction(None, [])
    await agent.run("after compaction")

    key = "input" if api == "responses" else "messages"
    assert server.requests[1][key][:4] == header_before  # system, environment, AGENTS.md, SKILLS
    assert server.requests[1].get("tools") == server.requests[0].get("tools")
