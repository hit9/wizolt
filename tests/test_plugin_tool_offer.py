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
    bootstrap_features(session)
    session.settings.yolo = True  # Plugin calls confirm like any mutating tool.
    instance = Agent(session, output_fn=lambda _text: None)
    instance.sent = []  # The tools each request carried, as the wire JSON.
    instance.script = []  # Tool calls to answer with, one step at a time; then a final answer.

    async def request(messages, tools, *, reason="normal"):
        instance.sent.append(json.dumps(tools, sort_keys=True))
        if instance.script:
            return {"role": "assistant", "content": ""}, [instance.script.pop(0)], ""
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


# --- calls ----------------------------------------------------------------------------------------


async def test_a_removed_tool_is_refused_even_when_the_model_calls_it(agent, tmp_path):
    choice = tmp_path / "hidden.json"
    choice.write_text('["Read"]')
    await enable(agent, tmp_path, "policy", POLICY.format(choice=str(choice)))
    result = await run_turn_with_a_tool_call(agent, read_call(tmp_path, "r1"))
    assert "Read is not offered in this turn" in result and "alpha" not in result


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
