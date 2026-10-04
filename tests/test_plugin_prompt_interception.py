"""prompt.submit through the real engine: effective input, original history, once per item."""

import pytest
from test_session_persistence import session_with_data_dir

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features
from wizolt.base import ModelRequestRetry
from wizolt.shellhooks import HookCommand, PromptBlocked, ShellHooks

SHORTHAND = """
from wizolt.sdk.operations import Prompt, Refusal
SDK_VERSION = 1
def setup(p):
    async def expand(ctx, prompt, next):
        with open(LOG, "a") as log:
            log.write(prompt.origin + ":" + prompt.text + "\\n")
        if "password" in prompt.text:
            return Refusal("keep secrets out of prompts")
        return await next(prompt.replace(text=prompt.text.replace("@@ship", "run the release checklist")))
    p.intercept("prompt.submit", expand{match})
"""


@pytest.fixture
async def agent(tmp_path):
    session = session_with_data_dir(tmp_path)
    bootstrap_features(session)
    instance = Agent(session, output_fn=lambda _text: None)
    instance.requests = []
    instance.replies = ["ok"] * 5
    instance.retry_once = False

    async def request(messages, tools):
        if instance.retry_once:
            instance.retry_once = False
            raise ModelRequestRetry()
        instance.requests.append(messages)
        return {"role": "assistant", "content": "ok"}, [], "ok"

    instance.model.request = request
    yield instance
    await session.plugins.close()


async def enable(agent, tmp_path, match=""):
    log = tmp_path / "calls.log"
    path = tmp_path / "shorthand.py"
    path.write_text(f"LOG = {str(log)!r}\n" + SHORTHAND.format(match=match))
    await agent.session.plugins.manage("enable", str(path))
    return log


def sent_text(messages):
    return "\n".join(str(message.get("content")) for message in messages if message.get("role") == "user")


async def test_model_gets_effective_input_and_history_keeps_the_original(agent, tmp_path):
    await enable(agent, tmp_path)
    await agent.run("please @@ship today")
    assert "please run the release checklist today" in sent_text(agent.requests[0])
    assert agent.session.transcript_messages[0]["content"] == "please @@ship today"
    receipt = agent.session.operation_receipts[-1]
    assert receipt.operation == "prompt.submit" and receipt.core == "completed" and "release checklist" in receipt.effective


async def test_refused_input_never_becomes_a_turn(agent, tmp_path):
    await enable(agent, tmp_path)
    with pytest.raises(PromptBlocked, match="refused by plugin shorthand: keep secrets out of prompts"):
        await agent.run("my password is hunter2")
    assert agent.requests == [] and agent.session.messages == []


async def test_shell_hook_sees_the_effective_text(agent, tmp_path):
    seen = tmp_path / "hook.json"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ncat > {seen}\n")
    hook.chmod(0o755)
    table = {"UserPromptSubmit": [{"matcher": "", "hooks": [{"type": "command", "command": str(hook)}]}]}
    agent.session.shell_hooks = ShellHooks(HookCommand.parse_table(table), str(tmp_path))
    await enable(agent, tmp_path)
    await agent.run("@@ship")
    assert "run the release checklist" in seen.read_text()


async def test_follow_up_is_admitted_once_across_request_rebuilds(agent, tmp_path):
    log = await enable(agent, tmp_path)
    agent.session.enqueue_user_input("and @@ship it")
    agent.retry_once = True  # The first request is rebuilt: claim, release and claim again.
    await agent.run("start")
    assert log.read_text().splitlines() == ["user:start", "followup:and @@ship it"]
    assert "run the release checklist it" in sent_text(agent.requests[0])


async def test_refused_follow_up_is_withheld_once(agent, tmp_path):
    log = await enable(agent, tmp_path)
    printed = []
    agent.output_fn = printed.append
    agent.session.enqueue_user_input("the password is x")
    agent.retry_once = True
    await agent.run("start")
    assert sum("Follow-up withheld: refused by plugin shorthand" in str(line) for line in printed) == 1
    assert log.read_text().count("password") == 1
    assert "password" not in sent_text(agent.requests[0])


async def test_origin_matcher_and_child_inputs(agent, tmp_path):
    log = await enable(agent, tmp_path, match=", match={'origin': 'followup'}")
    agent.session.enqueue_user_input("from the parent model", origin="child")
    await agent.run("typed by the user")
    assert not log.exists()  # Neither the initial input nor a child inbox input matched.
    agent.session.enqueue_user_input("typed later")
    await agent.run("again")
    assert log.read_text().splitlines() == ["followup:typed later"]


async def test_attachments_can_be_omitted_but_not_invented(agent, tmp_path):
    path = tmp_path / "inventor.py"
    path.write_text(
        "from wizolt.sdk.operations import Prompt\nSDK_VERSION = 1\ndef setup(p):\n"
        "    async def h(ctx, prompt, next):\n        return await next(prompt.replace(attachments=('secret.png',)))\n"
        "    p.intercept('prompt.submit', h)\n"
    )
    await agent.session.plugins.manage("enable", str(path))
    with pytest.raises(Exception, match="never add them"):
        await agent.run("look")
