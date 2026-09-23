"""Shared worker-handoff test helpers; the behavior tests live in the test_worker_* and
test_delegate_* modules."""

from agent_harness import session

from wizolt.engine import Agent


async def _requested_system(tmp_path, custom=None):
    s = session(tmp_path)
    if custom is not None:
        s.system_prompt = custom
    agent = Agent(s, output_fn=lambda text: None)
    request = await agent.prepare_request([{"role": "user", "content": "hi"}])
    return s, request.messages[0]["content"]


# --- Delegation (steps 4-5): the worker is driven through DelegateTool with a scripted model. ---


class FakeModelClient:
    """Stands in for wizolt.engine.ModelClient: records every request and replays a script of
    (assistant, tool_calls, content) triples, so the worker's loop is exercised without HTTP."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.received_tools = []
        self.last_compaction_model = ""

    async def request(self, messages, request_tools=None):
        self.requests.append(messages)
        self.received_tools.append(request_tools)
        return self.script.pop(0)

    def estimated_request_tokens(self, messages, tools=None):
        return sum(len(str(message)) for message in messages) // 4

    def cancel_active_request(self):
        pass


def _delegate_session(tmp_path):
    parent = session(tmp_path)
    parent.config.worker_provider = "default"
    parent.settings.worker = True
    return parent


async def _delegate_call(parent, runner, **args):
    from wizolt.tools.delegate import DelegateTool

    tool = DelegateTool(parent, [args])
    tool.runner = runner
    return await tool.call()


def _delegate_runner(parent):
    from wizolt.context import ContextManager
    from wizolt.runner import ToolRunner

    return ToolRunner(parent, ContextManager(parent), input_fn=lambda *a: "y", output_fn=lambda text: None)


def _worker_history_for_compaction(parent):
    """Return the spawned worker with a fat synthetic history appended after the first send."""
    worker = parent.worker
    assert worker is not None
    big = "x" * 100_000
    worker.messages.extend(
        [
            {"role": "user", "content": big + " u1"},
            {"role": "assistant", "content": big + " a1"},
            {"role": "user", "content": big + " u2"},
            {"role": "assistant", "content": big + " a2"},
            {"role": "user", "content": big + " u3"},
            {"role": "assistant", "content": big + " a3"},
            {"role": "user", "content": "final small request"},
        ]
    )
    return worker
