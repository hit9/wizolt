"""Interactive command surfaces: the provider/model/api/reason selection chains and the stored
Bash output viewer."""


import pytest
from tui_harness import loop

from wizolt.base import (
    ImageRouteNotice,
    LogBlock,
    LogEdge,
    LogRole,
    ModelError,
)
from wizolt.model import ModelClient
from wizolt.ui.cli.commands import (
    api,
    config,
)
from wizolt.ui.cli.status import StatusReport
from wizolt.ui.tui import TUI_MODAL_PENDING


async def test_status_ends_with_the_documentation_link(tmp_path):
    """The command list lives in the docs now, so every /status ends with the one place to read it."""
    inside = [row.strip(" │") for row in StatusReport.of(loop(tmp_path)).text().splitlines()[1:-1]]
    last = [row for row in inside if row][-1]  # the last row with content; padding sits below it
    assert last.split() == ["docs", "https://wizolt.readthedocs.io"]


async def test_status_rows_keep_off_the_frame_and_related_rows_group(tmp_path):
    """Every tab used to run edge to edge: rows against the borders and one block of labels."""
    rows = StatusReport.of(loop(tmp_path)).text().splitlines()
    assert not rows[1].strip(" │") and not rows[-2].strip(" │")  # a blank row inside each edge
    assert all(row.lstrip().startswith("│  ") for row in rows[1:-1])  # two spaces beside the border
    overview = [row.strip(" │") for row in rows[2 : rows.index(next(row for row in rows if row.strip(" │") == "Progress"))]]
    # Who and on what, then how much: a blank row parts the two.
    assert overview.index("") > max(index for index, row in enumerate(overview) if row.startswith("model"))
    assert overview.index("") < min(index for index, row in enumerate(overview) if row.startswith("context"))


def test_a_wrapped_list_item_hangs_past_its_marker():
    """A long known fact used to wrap back under its bullet, so seven facts read as one wall."""
    from wizolt.ui.cli.status import Entry, table, words

    fact = "a fact long enough to wrap onto a second row of the narrow table"
    rows = ["".join(text for _, text in row) for row in table([("", [Entry("known", [("", fact)], marker=(words("• "),))])], 40, 8)]
    assert rows[0].startswith("known   • a fact")
    assert len(rows) > 1 and all(row.startswith(" " * 10) and row[10] != " " for row in rows[1:])


async def test_image_route_notice_matches_view_image_tree_vocabulary(tmp_path):
    command_loop = loop(tmp_path)
    blocks = []
    command_loop.presentation.tool_output = blocks.append

    command_loop.presentation.image_route_notice(ImageRouteNotice("main model rejected image input (400)", described_by="vision/model", images=("shot.png",)))

    [block] = blocks
    assert isinstance(block, LogBlock)
    root, children = block.items
    assert (root.label, root.text, root.role, root.meta) == (
        "Image",
        "shot.png",
        LogRole.META,
        " · main model rejected image input (400)",
    )
    [child] = children.items
    assert (child.label, child.text, child.role, child.edge) == ("described by", "vision/model", LogRole.TOOL, LogEdge.END)

    command_loop.presentation.image_route_notice(ImageRouteNotice("main model is text-only", described_by="vision/model", images=("a.png", "b.png")))
    multi_root = blocks[-1].items[0]
    assert (multi_root.label, multi_root.text) == ("Images", "2 attachments")


class ModalHarness:
    def __init__(self, keys, *, consumed=False):
        self.app = None
        self.keys = list(keys)
        # consumed=True hands each key to the next modal in line instead of replaying the whole
        # sequence for every modal, which is how a multi-modal flow (list -> detail -> list) is
        # driven end to end.
        self.consumed = consumed
        self.pos = 0
        self.frames = []
        self.exclusive = []

    def _drive(self, fragments_fn, key_fn, *, exclusive=False):
        self.exclusive.append(exclusive)
        self.frames.append(fragments_fn())
        result = TUI_MODAL_PENDING
        keys = self.keys[self.pos :] if self.consumed else self.keys
        for key in keys:
            if self.consumed:
                self.pos += 1
            result = key_fn(key, key if len(key) == 1 else "")
            self.frames.append(fragments_fn())
            if result is not TUI_MODAL_PENDING:
                return result
        return None

    async def show_modal(self, fragments_fn, key_fn, *, exclusive=False):
        return self._drive(fragments_fn, key_fn, exclusive=exclusive)






























































































async def test_api_command_reports_an_incompatible_builtin_tools_configuration_without_clearing_it(tmp_path):
    """Switching /api reports inactive builtin tools and never rewrites provider config."""
    command_loop = loop(tmp_path)
    provider_config = command_loop.session.config.provider
    provider_config.url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    provider_config.model = "qwen3.8-max-preview"
    provider_config.key = "sk-test"
    provider_config.api = "responses"
    provider_config.builtin_tools = ({"type": "web_search"}, {"type": "web_extractor"})

    assert await api(command_loop, "chat") == "Set provider.api = chat (wire: chat); builtin_tools inactive on chat"
    # The requested API value is applied and the provider configuration is left intact.
    assert provider_config.api == "chat"
    assert provider_config.builtin_tools == ({"type": "web_search"}, {"type": "web_extractor"})

    # The next request projects no provider-native tools on the mismatched wire.
    assert ModelClient(command_loop.session).builtin_tools() == []

    # Switching back restores the working Responses configuration without erasing it.
    assert await api(command_loop, "responses") == "Set provider.api = responses (wire: responses)"
    assert provider_config.builtin_tools == ({"type": "web_search"}, {"type": "web_extractor"})


async def test_api_command_reports_when_no_wire_accepts_the_configured_builtin_tools(tmp_path):
    """DeepSeek has no provider-side tools channel, so the shared config stays inactive."""
    command_loop = loop(tmp_path)
    provider_config = command_loop.session.config.provider
    provider_config.url = "https://api.deepseek.com/v1"
    provider_config.model = "deepseek-chat"
    provider_config.key = "sk-test"
    provider_config.builtin_tools = ({"type": "web_search"},)

    assert await api(command_loop, "chat") == "Set provider.api = chat (wire: chat); builtin_tools inactive on chat"
    assert provider_config.builtin_tools == ({"type": "web_search"},)


async def test_config_distinguishes_configured_and_active_builtin_tools(tmp_path):
    command_loop = loop(tmp_path)
    provider_config = command_loop.session.config.provider
    provider_config.url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    provider_config.model = "qwen3.8-max-preview"
    provider_config.api = "chat"
    provider_config.builtin_tools = ({"type": "web_search"}, {"type": "web_extractor"})

    inactive = config(command_loop, "")
    assert "provider.builtin_tools: web_search, web_extractor" in inactive
    assert "provider.resolved_builtin_tools: inactive on chat: web_search, web_extractor" in inactive

    provider_config.api = "responses"
    active = config(command_loop, "")
    assert "provider.resolved_builtin_tools: active: web_search, web_extractor" in active


async def test_api_command_uses_the_same_entry_policy_as_the_request_boundary(tmp_path):
    """A valid wire with an unsupported entry is reported immediately, not only on send."""
    command_loop = loop(tmp_path)
    provider_config = command_loop.session.config.provider
    provider_config.url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    provider_config.model = "qwen3.8-max-preview"
    provider_config.key = "sk-test"
    provider_config.builtin_tools = ({"type": "code_interpreter"},)

    assert await api(command_loop, "responses") == "Set provider.api = responses (wire: responses); unsupported builtin_tools: code_interpreter"
    with pytest.raises(ModelError):
        ModelClient(command_loop.session).builtin_tools()
