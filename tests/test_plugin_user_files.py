"""Files a user writes for a plugin, and the editor a plugin can hand them.

The abilities: `plugin.user_file_paths` (one shared lookup, the project's copy over the user's own,
reading and creating nothing) and `plugin.ui.edit` (the user's editor on the plugin's text). The
bundled `system_prompt` plugin is built only on them, so it is exercised end to end here.
"""

import asyncio

import pytest

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features
from wizolt.config import Config
from wizolt.sdk import Context, Plugin, PluginError
from wizolt.session import Session
from wizolt.ui.cli.plugin_dialogs import PluginDialogs
from wizolt.ui.cli.plugin_scenarios import ScriptedDialogs

PLUGIN_FILE = ("plugins", "system_prompt", "system.md")


@pytest.fixture
async def agent(tmp_path):
    (tmp_path / "project").mkdir()
    (tmp_path / "data").mkdir()
    config = Config(data_dir=str(tmp_path / "data"))
    session = Session(cwd=str(tmp_path / "project"), config=config)
    bootstrap_features(session)
    session.system_prompt = "You are wizolt."
    instance = Agent(session, output_fn=lambda _text: None)
    instance.sent = []  # Each request's full message list.

    async def request(messages, tools, *, reason="normal"):
        instance.sent.append(messages)
        return {"role": "assistant", "content": "ok"}, [], "ok"

    instance.model.request = request
    yield instance
    await session.plugins.close()


def system(messages):
    return messages[0]["content"]


class Editor:
    """The user at their editor: saves `reply` (None = quits without saving), records what opened."""

    def __init__(self, reply):
        self.reply = reply
        self.opened = []

    async def call(self, owner, service, arguments):
        assert service == "ui.edit"
        self.opened.append(arguments["text"])
        return {"text": self.reply}


async def prompt_command(agent, editor):
    agent.session.plugins.interactions.handler = editor.call
    return await agent.session.plugins.invoke("system_prompt", "command", "prompt", {})


def plugin_files(tmp_path):
    return sorted(path for path in tmp_path.rglob("system.md"))


# --- the lookup ----------------------------------------------------------------------------------


def test_the_project_copy_comes_before_the_users_own_and_nothing_is_touched(tmp_path):
    context = Context("s", "main", str(tmp_path / "project"), "idle", 0, 0, "m", 0, data_dir=str(tmp_path / "data"))
    paths = Plugin("notes").user_file_paths(context, "today.md")
    assert paths == (tmp_path / "project" / ".wizolt" / "plugins" / "notes" / "today.md", tmp_path / "data" / "plugins" / "notes" / "today.md")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("name", ["../escape.md", "sub/dir.md", ".hidden", "", "x" * 65])
def test_a_user_file_is_one_plain_file_name(name):
    context = Context("s", "main", "/project", "idle", 0, 0, "m", 0, data_dir="/data")
    with pytest.raises(PluginError, match="one plain file name"):
        Plugin("notes").user_file_paths(context, name)


# --- the editor ----------------------------------------------------------------------------------


class Tui:
    managed = True

    def __init__(self, reply):
        self.reply = reply

    async def edit_text(self, text):
        return self.reply


async def test_the_host_hands_the_editor_the_text_and_returns_what_was_saved():
    dialogs = PluginDialogs(Tui("edited"), presentation=None)  # type: ignore[arg-type]
    assert await dialogs.call("notes", "ui.edit", {"text": "draft"}) == {"text": "edited"}
    with pytest.raises(PluginError, match="takes text"):
        await dialogs.call("notes", "ui.edit", {"text": 3})


async def test_a_trial_scripts_the_editor_like_any_other_interaction():
    dialogs = ScriptedDialogs([{"expect": {"kind": "edit"}, "reply": "edited"}, {"expect": {"kind": "edit"}, "reply": None}], 60, 20, 1)
    assert dialogs.edit({"text": "draft"}) == {"text": "edited"}
    assert dialogs.edit({"text": "again"}) == {"text": None}
    dialogs.finish()
    assert dialogs.trace == [{"edit": "draft", "result": "edited"}, {"edit": "again", "result": None}]


async def test_waiting_on_the_editor_does_not_count_against_the_commands_deadline(agent):
    await agent.session.plugins.manage("enable", "system_prompt")
    agent.session.plugins.ACTION_TIMEOUT = 0.2
    release = asyncio.Event()

    async def slow(owner, service, arguments):
        await release.wait()
        return {"text": None}

    agent.session.plugins.interactions.handler = slow
    action = asyncio.create_task(agent.session.plugins.invoke("system_prompt", "command", "prompt", {}))
    await asyncio.sleep(0.4)
    assert not action.done()
    release.set()
    assert "nothing was written" in await asyncio.wait_for(action, 3)


# --- the bundled system_prompt plugin -------------------------------------------------------------


async def test_the_bundled_plugin_ships_off_and_enabling_it_writes_nothing(agent, tmp_path):
    await agent.run("off")
    await agent.session.plugins.manage("enable", "system_prompt")
    await agent.run("on")
    assert plugin_files(tmp_path) == []
    assert system(agent.sent[0]) == system(agent.sent[1])  # No file: wizolt's own prompt, unchanged.


async def test_prompt_opens_the_current_prompt_and_a_saved_edit_replaces_it_costing_the_cache_once(agent, tmp_path):
    await agent.session.plugins.manage("enable", "system_prompt")
    await agent.run("one")
    editor = Editor("You are terse.\n")

    reply = await prompt_command(agent, editor)

    assert editor.opened == ["You are wizolt."]  # Starts from the prompt the last request carried.
    assert plugin_files(tmp_path) == [tmp_path.joinpath("data", *PLUGIN_FILE)]
    assert "applies from the next request" in reply
    for text in ("two", "three", "four"):
        await agent.run(text)
    systems = [system(messages) for messages in agent.sent]
    assert systems[1].startswith("You are terse.") and len(set(systems[1:])) == 1  # One change, then stable.
    assert systems[0] != systems[1]


@pytest.mark.parametrize(
    ("reply", "said"),
    [
        (None, "The editor did not save; nothing was written."),
        ("You are wizolt.", "No changes; nothing was written."),
        ("You are wizolt.\n", "No changes; nothing was written."),  # The editor's own final newline.
        ("   ", "Not saved: an empty prompt. Delete the file to return to wizolt's own."),
    ],
)
async def test_quitting_unchanged_or_empty_writes_nothing(agent, tmp_path, reply, said):
    await agent.session.plugins.manage("enable", "system_prompt")
    await agent.run("one")
    assert await prompt_command(agent, Editor(reply)) == said
    assert plugin_files(tmp_path) == []


async def test_an_edit_goes_to_the_file_in_effect_and_the_projects_copy_wins(agent, tmp_path):
    project = tmp_path.joinpath("project", ".wizolt", *PLUGIN_FILE)
    project.parent.mkdir(parents=True)
    project.write_text("Project prompt.")
    await agent.session.plugins.manage("enable", "system_prompt")
    editor = Editor("Project prompt, edited.")
    await prompt_command(agent, editor)
    await agent.run("hello")
    assert editor.opened == ["Project prompt."]
    assert project.read_text() == "Project prompt, edited." and plugin_files(tmp_path) == [project]
    assert system(agent.sent[0]).startswith("Project prompt, edited.")


async def test_an_unusable_file_leaves_wizolts_prompt_in_place(agent, tmp_path):
    own = tmp_path.joinpath("data", *PLUGIN_FILE)
    own.parent.mkdir(parents=True)
    own.write_bytes(b"\xff\xfe not utf-8")
    await agent.session.plugins.manage("enable", "system_prompt")
    await agent.run("hello")
    assert system(agent.sent[0]).startswith("You are wizolt.")
