"""tools.offer through the real engine: what each request offers, and what a call may run.

The model is scripted, so every assertion reads the tools a request actually carried, as the
provider would see them; the plugins run in real workers.
"""

import json

import pytest
from test_session_persistence import session_with_data_dir

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features
from wizolt.model import ModelClient
from wizolt.tools import Tool

# A user's own policy plugin: it reads the choice the user saved and narrows every turn to it.
POLICY = """
import json, pathlib
def setup(p):
    async def offer(ctx, tools, next):
        hidden = json.loads(pathlib.Path({choice!r}).read_text())
        return await next(tools.without(*hidden))
    p.intercept("tools.offer", offer)
"""

NOTES = """
def setup(p):
    async def search(ctx, args):
        return "found " + args["query"]
    p.tool("search", "Search my notes", {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}, search)
"""


@pytest.fixture
async def agent(tmp_path):
    session = session_with_data_dir(tmp_path)
    session.config.path = str(tmp_path / "config.toml")  # Where plugin settings are saved.
    (tmp_path / "config.toml").write_text("")
    bootstrap_features(session)
    session.settings.yolo = True  # Plugin calls confirm like any mutating tool.
    instance = Agent(session, output_fn=lambda _text: None)
    instance.sent = []  # The tools each request carried, as the wire JSON.
    instance.script = []  # A tool call (or a list: one batch) per step; then a final answer.

    async def request(messages, tools, *, reason="normal"):
        instance.sent.append(json.dumps(tools, sort_keys=True))
        if instance.script:
            calls = instance.script.pop(0)
            return {"role": "assistant", "content": ""}, calls if isinstance(calls, list) else [calls], ""
        return {"role": "assistant", "content": "ok"}, [], "ok"

    instance.model.request = request
    yield instance
    await session.plugins.close()


async def enable(agent, tmp_path, name, source):
    path = tmp_path / f"{name}.py"
    path.write_text("SDK_VERSION = 1\n" + source)
    await agent.session.plugins.manage("enable", str(path))


def names(sent):
    return [tool["function"]["name"] for tool in json.loads(sent)]


async def run_turn_with_a_tool_call(agent, call, text="work"):
    """One turn of two requests: the model calls `call`, then answers. Returns the tool result."""
    agent.script.append(call)
    await agent.run(text)
    return next(message["content"] for message in reversed(agent.session.messages) if message.get("role") == "tool")


def read_call(tmp_path, call_id):
    (tmp_path / "a.txt").write_text("alpha")
    return ModelClient.tool_call(call_id, "Read", {"path": str(tmp_path / "a.txt")})


# --- the cache: a changed choice costs one miss, then the tools are stable again ------------------


async def test_changing_the_choice_changes_the_tools_once_and_then_they_stay_byte_identical(agent, tmp_path):
    choice = tmp_path / "hidden.json"
    choice.write_text("[]")
    await enable(agent, tmp_path, "policy", POLICY.format(choice=str(choice)))

    await run_turn_with_a_tool_call(agent, read_call(tmp_path, "r1"))
    choice.write_text('["Subagent"]')  # The user's one adjustment, between turns.
    for index in range(2, 5):
        await run_turn_with_a_tool_call(agent, read_call(tmp_path, f"r{index}"))

    before, after = agent.sent[:2], agent.sent[2:]
    assert len(set(before)) == 1 and len(set(after)) == 1  # Within a turn and across turns: identical bytes.
    assert before[0] != after[0]  # The adjustment is the one change.
    assert "Subagent" in names(before[0]) and "Subagent" not in names(after[0])


async def test_a_policy_that_answers_the_same_way_never_changes_the_tools(agent, tmp_path):
    choice = tmp_path / "hidden.json"
    choice.write_text('["Subagent"]')
    await enable(agent, tmp_path, "policy", POLICY.format(choice=str(choice)))
    for text in ("one", "two", "three"):
        await agent.run(text)
    assert len(set(agent.sent)) == 1


async def test_a_turn_offers_one_set_even_if_the_choice_changes_mid_turn(agent, tmp_path):
    """The chain runs once per turn: a change saved while the turn works waits for the next turn."""
    choice = tmp_path / "hidden.json"
    choice.write_text("[]")
    await enable(agent, tmp_path, "policy", POLICY.format(choice=str(choice)))
    agent.script.append(ModelClient.tool_call("b1", "Bash", {"command": f"echo '[\"Subagent\"]' > {choice}"}))
    await agent.run("work")
    assert len(agent.sent) == 2 and agent.sent[0] == agent.sent[1]
    await agent.run("next")
    assert "Subagent" not in names(agent.sent[2])


async def test_without_a_registration_every_tool_is_offered_as_before(agent, tmp_path):
    await agent.run("hello")
    assert agent.session.offered_tools is None
    assert names(agent.sent[0]) == [schema["function"]["name"] for schema in Tool.builtin_schemas(agent.session)]


# --- what a handler may change ---------------------------------------------------------------------


async def test_reordering_changes_nothing_the_provider_sees(agent, tmp_path):
    await agent.run("plain")
    await enable(agent, tmp_path, "shuffle", "def setup(p):\n    async def h(ctx, tools, next):\n        return await next(tools.replace(tools=tools.tools[::-1]))\n    p.intercept('tools.offer', h)\n")
    await agent.run("shuffled")
    assert agent.sent[0] == agent.sent[1]


async def test_a_handler_cannot_invent_a_tool(agent, tmp_path):
    await enable(agent, tmp_path, "forger", "def setup(p):\n    async def h(ctx, tools, next):\n        return await next(tools.adding('Invented'))\n    p.intercept('tools.offer', h)\n")
    with pytest.raises(Exception, match="can add only available plugin tools, not: Invented"):
        await agent.run("hello")
    assert agent.sent == []  # The turn failed before any request went out.
    # And it failed like any turn: settled with its marker, so the session goes on.
    assert agent.session.messages[-1]["content"].startswith("[This turn ended early:")
    await agent.session.plugins.manage("disable", "forger")
    assert await agent.run("again") == "ok"


async def test_a_plugin_tool_whose_wire_name_a_provider_would_reject_is_not_available(agent, tmp_path):
    tool = "t" * 60  # `notes-` plus this is longer than the 64 characters providers accept.
    await enable(agent, tmp_path, "notes", NOTES.replace('"search"', repr(tool)))
    await enable(agent, tmp_path, "greedy", f"def setup(p):\n    async def h(ctx, tools, next):\n        return await next(tools.adding('notes.{tool}'))\n    p.intercept('tools.offer', h)\n")
    with pytest.raises(Exception, match="can add only available plugin tools"):
        await agent.run("hello")


# --- calls ----------------------------------------------------------------------------------------


async def test_a_removed_tool_is_refused_even_when_the_model_calls_it(agent, tmp_path):
    choice = tmp_path / "hidden.json"
    choice.write_text('["Read"]')
    await enable(agent, tmp_path, "policy", POLICY.format(choice=str(choice)))
    result = await run_turn_with_a_tool_call(agent, read_call(tmp_path, "r1"))
    assert "Read is not offered in this turn" in result and "alpha" not in result


async def test_removed_read_only_calls_in_one_batch_are_refused_too(agent, tmp_path):
    """Read-only calls normally run in parallel; a removed one must not slip through that path."""
    choice = tmp_path / "hidden.json"
    choice.write_text('["Read"]')
    await enable(agent, tmp_path, "policy", POLICY.format(choice=str(choice)))
    agent.script.append([read_call(tmp_path, "r1"), read_call(tmp_path, "r2")])
    await agent.run("work")
    results = [message["content"] for message in agent.session.messages if message.get("role") == "tool"]
    assert len(results) == 2 and all("Read is not offered in this turn" in result and "alpha" not in result for result in results)


async def test_a_plugin_tool_offered_directly_is_in_the_request_and_runs_like_the_gateway_call(agent, tmp_path):
    await enable(agent, tmp_path, "notes", NOTES)
    # Removing the gateway itself must not strand the tool offered in its place.
    resident = "def setup(p):\n    async def h(ctx, tools, next):\n        return await next(tools.adding('notes.search').without('Plugin'))\n    p.intercept('tools.offer', h)\n"
    await enable(agent, tmp_path, "resident", resident)

    result = await run_turn_with_a_tool_call(agent, ModelClient.tool_call("n1", "notes-search", {"query": "freeze"}))

    offered = json.loads(agent.sent[0])
    [schema] = [tool["function"] for tool in offered if tool["function"]["name"] == "notes-search"]
    assert schema["description"] == "Search my notes" and schema["parameters"]["required"] == ["query"]
    assert "Plugin" not in names(agent.sent[0])
    assert "found freeze" in result
    receipt = next(receipt for receipt in agent.session.operation_receipts if receipt.operation == "tools.offer")
    assert json.loads(receipt.effective) == {"added": ["notes.search"], "removed": ["Plugin"]}


async def test_a_plugin_tool_not_offered_directly_is_not_callable_by_its_wire_name(agent, tmp_path):
    await enable(agent, tmp_path, "notes", NOTES)
    result = await run_turn_with_a_tool_call(agent, ModelClient.tool_call("n1", "notes-search", {"query": "freeze"}))
    assert "unknown tool notes-search" in result


# --- the bundled tool_visibility plugin: /tools built on tools.offer ------------------------------


class Person:
    """Answers the plugin's picker with a fixed selection, and records what it was shown."""

    def __init__(self, selected=None):
        self.selected = selected
        self.shown = []

    async def call(self, owner, service, arguments):
        if service != "ui.views.show":
            return {}
        self.shown.append(arguments["view"])
        return {"result": None if self.selected is None else {"action": "submit", "selected": list(self.selected)}}


async def tools_command(agent, person):
    agent.session.plugins.interactions.handler = person.call
    return await agent.session.plugins.invoke("tool_visibility", "command", "tools", {})


async def test_the_bundled_plugin_ships_off_and_changes_nothing_until_enabled(agent):
    await agent.run("hello")
    assert "tool_visibility" not in agent.session.plugins.entries
    assert agent.session.offered_tools is None


async def test_tools_lists_the_offer_and_saves_a_choice_that_costs_the_cache_once(agent, tmp_path):
    await agent.session.plugins.manage("enable", "tool_visibility")
    await enable(agent, tmp_path, "notes", NOTES)
    await run_turn_with_a_tool_call(agent, read_call(tmp_path, "r1"))  # The plugin learns the offer.
    builtin = names(agent.sent[0])
    person = Person(selected=[name for name in builtin if name != "Subagent"] + ["notes.search"])

    reply = await tools_command(agent, person)

    [view] = person.shown
    assert [item["id"] for item in view["body"]["items"]] == [*builtin, "notes.search"]
    assert view["body"]["selected"] == builtin  # Everything built-in is offered until the user says otherwise.
    assert "1 hidden, 1 offered directly" in reply
    for index in range(2, 5):
        await run_turn_with_a_tool_call(agent, read_call(tmp_path, f"r{index}"))
    before, after = agent.sent[:2], agent.sent[2:]
    assert len(set(before)) == 1 and len(set(after)) == 1 and before[0] != after[0]
    assert "Subagent" not in names(after[0]) and "notes-search" in names(after[0])


async def test_the_choice_is_saved_for_the_next_session(agent, tmp_path):
    await agent.session.plugins.manage("enable", "tool_visibility")
    await agent.run("hello")
    builtin = names(agent.sent[0])
    await tools_command(agent, Person(selected=[name for name in builtin if name != "Bash"]))
    await agent.session.plugins.manage("reload", "tool_visibility")  # A new generation reads the saved choice.
    await agent.run("again")
    assert "Bash" not in names(agent.sent[-1])


async def test_cancelling_or_choosing_the_same_set_changes_nothing(agent):
    await agent.session.plugins.manage("enable", "tool_visibility")
    await agent.run("hello")
    assert await tools_command(agent, Person(selected=None)) == "Tools unchanged"
    assert await tools_command(agent, Person(selected=names(agent.sent[0]))) == "Tools unchanged"
    await agent.run("again")
    assert agent.sent[0] == agent.sent[1]


async def test_tools_works_before_the_first_turn_and_says_what_each_tool_does(agent, tmp_path):
    await agent.session.plugins.manage("enable", "tool_visibility")
    await enable(agent, tmp_path, "notes", NOTES)
    person = Person(selected=None)

    await tools_command(agent, person)

    [view] = person.shown
    labels = {item["id"]: item["label"] for item in view["body"]["items"]}
    assert labels["notes.search"] == "notes.search (plugin, offered directly) · Search my notes"
    assert labels["Read"].startswith("Read · ") and len(labels["Read"]) > len("Read · ")


# --- what a plugin can read about the agent, and what a turn records ------------------------------

FACTS = """
import json
def setup(p):
    async def facts(ctx, args):
        tools = await p.agent.tools()
        return json.dumps({"tools": [[t.name, t.kind, t.offered] for t in tools], "system": await p.agent.system_prompt()})
    p.command("facts", "Agent facts", facts)
"""


async def facts(agent):
    return json.loads(await agent.session.plugins.invoke("facts", "command", "facts", {}))


async def test_a_plugin_reads_the_tools_and_the_system_prompt_before_any_turn(agent, tmp_path):
    agent.session.system_prompt = "  You are wizolt.\n"
    await enable(agent, tmp_path, "notes", NOTES)
    await enable(agent, tmp_path, "facts", FACTS)

    seen = await facts(agent)

    builtin = [schema["function"]["name"] for schema in Tool.builtin_schemas(agent.session)]
    assert [name for name, kind, _ in seen["tools"] if kind == "builtin"] == builtin
    assert ["notes.search", "plugin", False] in seen["tools"]  # Not offered directly until a policy adds it.
    assert all(offered for _, kind, offered in seen["tools"] if kind == "builtin")  # No policy: all offered.
    assert seen["system"] == "You are wizolt."  # The `system` block exactly as compose receives it.


async def test_the_offered_flags_follow_the_last_turn(agent, tmp_path):
    choice = tmp_path / "hidden.json"
    choice.write_text('["Bash"]')
    await enable(agent, tmp_path, "policy", POLICY.format(choice=str(choice)))
    await enable(agent, tmp_path, "facts", FACTS)
    await agent.run("hello")

    offered = {name: flag for name, _, flag in (await facts(agent))["tools"]}

    assert offered["Bash"] is False and offered["Read"] is True


async def test_a_turn_records_its_offer_only_when_the_set_changes(agent, tmp_path):
    """Receipts are capped; an unchanged policy would otherwise push every other receipt out."""
    choice = tmp_path / "hidden.json"
    choice.write_text('["Subagent"]')
    await enable(agent, tmp_path, "policy", POLICY.format(choice=str(choice)))

    def offers():
        return [receipt for receipt in agent.session.operation_receipts if receipt.operation == "tools.offer"]

    for text in ("one", "two", "three"):
        await agent.run(text)
    assert len(offers()) == 1
    choice.write_text('["Bash"]')
    await agent.run("four")
    await agent.run("five")
    assert len(offers()) == 2 and json.loads(offers()[-1].effective)["removed"] == ["Bash"]
