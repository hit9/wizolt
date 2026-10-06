"""Files a plugin declares for the user to write: nothing on disk until the user writes it, one
shared lookup (the project's copy over the user's own), and a read that follows every edit."""

import pytest

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features
from wizolt.config import Config
from wizolt.sdk import Plugin, PluginError
from wizolt.session import Session

# A user's own system-prompt plugin: their file, when it exists, replaces the system text.
SYSTEM_PROMPT = """SDK_VERSION = 1
def setup(p):
    p.user_file("system.md", "Replaces the system prompt", template="You are my assistant.")
    async def compose(ctx, blocks, next):
        path = p.user_file_path(ctx, "system.md")
        if path is not None:
            blocks = blocks.with_text("system", path.read_text())
        return await next(blocks)
    p.intercept("context.compose", compose)
"""


@pytest.fixture
async def agent(tmp_path):
    (tmp_path / "project").mkdir()
    session = Session(cwd=str(tmp_path / "project"), config=Config(data_dir=str(tmp_path / "data")))
    bootstrap_features(session)
    session.system_prompt = "You are wizolt."
    instance = Agent(session, output_fn=lambda _text: None)
    instance.systems = []

    async def request(messages, tools, *, reason="normal"):
        instance.systems.append(messages[0]["content"])
        return {"role": "assistant", "content": "ok"}, [], "ok"

    instance.model.request = request
    path = tmp_path / "system_prompt.py"
    path.write_text(SYSTEM_PROMPT)
    await session.plugins.manage("enable", str(path))
    yield instance
    await session.plugins.close()


def files_under(root):
    return sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file() and "plugins/system_prompt" in str(path))


async def test_declaring_and_using_a_file_writes_nothing(agent, tmp_path):
    await agent.run("hello")
    assert files_under(tmp_path) == []
    assert agent.systems[0].startswith("You are wizolt.")  # No file yet: the builtin text stands.


async def test_the_users_file_replaces_the_text_and_an_edit_applies_on_the_next_request(agent, tmp_path):
    own = tmp_path / "data" / "plugins" / "system_prompt" / "system.md"
    own.parent.mkdir(parents=True)
    own.write_text("You are terse.")
    await agent.run("one")
    own.write_text("You are verbose.")
    await agent.run("two")  # No reload between them.
    assert agent.systems[0].startswith("You are terse.") and agent.systems[1].startswith("You are verbose.")


async def test_the_projects_copy_overrides_the_users_own(agent, tmp_path):
    for base, text in ((tmp_path / "data", "user text"), (tmp_path / "project" / ".wizolt", "project text")):
        path = base / "plugins" / "system_prompt" / "system.md"
        path.parent.mkdir(parents=True)
        path.write_text(text)
    await agent.run("hello")
    assert agent.systems[0].startswith("project text")


async def test_the_host_knows_each_declared_file_and_its_template(agent):
    declared = agent.session.plugins.entries["system_prompt"].active.plugin.user_files
    assert {name: (item.description, item.template) for name, item in declared.items()} == {
        "system.md": ("Replaces the system prompt", "You are my assistant.")
    }


@pytest.mark.parametrize("name", ["../escape.md", "sub/dir.md", ".hidden", "", "x" * 65])
def test_a_declared_name_is_one_plain_file_name(name):
    with pytest.raises(PluginError, match="Invalid registration"):
        Plugin("p").user_file(name, "A file")


def test_templates_are_bounded_together():
    plugin = Plugin("p")
    plugin.user_file("a.md", "A", template="x" * (200 * 1024))
    with pytest.raises(PluginError, match="at most 256 KiB together"):
        plugin.user_file("b.md", "B", template="x" * (100 * 1024))


def test_reading_an_undeclared_file_is_an_error():
    with pytest.raises(PluginError, match="Undeclared user file"):
        Plugin("p").user_file_path(None, "system.md")  # type: ignore[arg-type]
