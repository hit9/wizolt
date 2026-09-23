"""Global memory: MEMORY.md parsing, the session-fixed catalog, @mem: grammar and expansion,
the @mem: completion menu, the /memory command, and global AGENTS.md injection."""

import asyncio

import pytest
from agent_harness import session
from prompt_toolkit.document import Document

from wizolt.base import MAX_AGENTS_MD_TOKENS, MAX_MEMORY_FILE_BYTES, SESSION_EVENT_KEY
from wizolt.cli import CommandLoop
from wizolt.cli.commands import memory_command
from wizolt.cli.view import CommandCompleter
from wizolt.config import Config
from wizolt.context import ContextManager
from wizolt.engine import Agent
from wizolt.memory import MAX_CATALOG_CHARS, MAX_REFERENCES, MemoryEntry, parse_memory, preview, validate_memory
from wizolt.mentions import active_mention, encode_mem_mention, scan_mentions
from wizolt.session import Session, bootstrap_features
from wizolt.tools import EditTool, ReadTool

MEMORY_MD = """# MEMORY

Notes kept across projects.

## 设计讨论使用中文
用户偏好以中文讨论设计方案。

## 共用部署环境
多个项目共用部署环境；
执行部署前应重新核实细节。

## 无正文条目
"""


def write_memory(tmp_path, text=MEMORY_MD):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    (data_dir / "MEMORY.md").write_text(text, encoding="utf-8")
    return data_dir / "MEMORY.md"


def write_memory_outside(tmp_path, text=MEMORY_MD):
    """A memory file in a data dir outside the workspace cwd, the real-world layout."""
    data_dir = tmp_path.parent / (tmp_path.name + "-data")
    data_dir.mkdir(exist_ok=True)
    (data_dir / "MEMORY.md").write_text(text, encoding="utf-8")
    return data_dir


def session_with_data(tmp_path, data_dir):
    config = Config()
    config.data_dir = str(data_dir)
    s = Session(cwd=str(tmp_path), config=config)
    bootstrap_features(s)
    return s


def completions(completer, text):
    return [c.text for c in completer.get_completions(Document(text), None)]


# --- parsing ---


def test_parse_memory_entries_keep_file_order_and_bodies():
    entries = parse_memory(MEMORY_MD)
    assert entries == [
        MemoryEntry("设计讨论使用中文", "用户偏好以中文讨论设计方案。"),
        MemoryEntry("共用部署环境", "多个项目共用部署环境；\n执行部署前应重新核实细节。"),
        MemoryEntry("无正文条目", ""),
    ]


def test_parse_memory_accepts_legacy_id_and_screenshot_style_metadata():
    text = "# MEMORY\n\n## 7f3a91c2 旧标题\n旧正文\n\n## PR body 要求\n\nid: 133e87f9\nscope: orion-arm-ai\n\n中文 Markdown。\n"
    assert parse_memory(text) == [
        MemoryEntry("旧标题", "旧正文"),
        MemoryEntry("PR body 要求", "scope: orion-arm-ai\n\n中文 Markdown。"),
    ]


def test_validate_memory_rejects_missing_and_duplicate_titles():
    with pytest.raises(ValueError, match="line 1"):
        validate_memory("##\n正文\n")
    with pytest.raises(ValueError, match="duplicate title"):
        validate_memory("## 相同标题\n一\n## 相同标题\n二\n")
    with pytest.raises(ValueError, match="no memory sections"):
        validate_memory("plain text without a heading\n")


def test_new_memory_file_is_available_to_completion_in_the_same_session(tmp_path):
    s = session(tmp_path)
    assert "No saved entries yet" in s.memory.catalog()
    path = write_memory(
        tmp_path,
        "# MEMORY\n\n## PR body 要求 (orion-arm-ai 路径下的项目)\n\nid: 133e87f9\nscope: orion-arm-ai\n\nPR body 使用中文 Markdown。\n",
    )
    assert path.exists()
    rows = list(CommandCompleter(memories=s.memory.menu_entries).get_completions(Document("@mem:"), None))
    assert [row.display_text for row in rows] == ["PR body 要求 (orion-arm-ai 路径下的项目)"]
    assert "133e87f9" not in rows[0].display_meta_text
    assert rows[0].text == '@mem:"PR body 要求 (orion-arm-ai 路径下的项目)"'


def test_preview_first_nonblank_line_clipped():
    long_line = "多个项目共用部署环境；执行部署前应重新核实细节。" + "x" * 30
    assert preview("\n\n  " + long_line + "\nsecond") == long_line[:40].rstrip() + "…"
    assert preview("\n  \n") == ""
    assert preview("short\nmore") == "short"


# --- @mem: grammar ---


def test_mem_mention_scans_bare_and_quoted_forms():
    spans = scan_mentions('see @mem:设计讨论使用中文 and @mem:"共用部署环境 2026" now')
    mem = [span for span in spans if span.kind == "mem"]
    assert [span.payload for span in mem] == ["设计讨论使用中文", "共用部署环境 2026"]
    assert all(span.complete for span in mem)
    # empty and truncated payloads are incomplete, an email is untouched
    assert scan_mentions("mail a@mem:x.com") == []
    assert not active_mention("use @mem:").complete


def test_encode_mem_mention_round_trips():
    assert encode_mem_mention("设计讨论使用中文") == '@mem:"设计讨论使用中文"'
    quoted = encode_mem_mention("共用部署环境 2026")
    assert quoted == '@mem:"共用部署环境 2026"'
    for form in (encode_mem_mention("设计讨论使用中文"), quoted):
        span = active_mention("use " + form)
        assert span is not None and span.kind == "mem" and span.complete
    # a leading quote must not produce a bare form that scans as quoted
    assert encode_mem_mention('"odd').startswith('@mem:"')
    assert encode_mem_mention("部署：提醒") == '@mem:"部署：提醒"'
    assert active_mention("use @mem:设计讨论使用中文，") is None
    assert [span.payload for span in scan_mentions("use @mem:设计讨论使用中文，继续")] == ["设计讨论使用中文"]
    assert [span.payload for span in scan_mentions(encode_mem_mention("设计讨论使用中文") + "继续讨论")] == ["设计讨论使用中文"]


# --- catalog ---


def test_catalog_lists_id_title_and_path_in_file_order(tmp_path):
    write_memory(tmp_path)
    s = session(tmp_path)
    catalog = s.memory.catalog()
    assert catalog.startswith("--- MEMORY ---")
    assert f"Path: {s.memory.path()}" in catalog
    first = catalog.index("- 设计讨论使用中文")
    second = catalog.index("- 共用部署环境")
    third = catalog.index("- 无正文条目")
    assert first < second < third
    assert "7f3a91c2" not in catalog
    assert "Edit MEMORY.md only when the user explicitly asks" in catalog


def test_catalog_is_fixed_for_the_session_even_if_the_file_changes(tmp_path):
    path = write_memory(tmp_path)
    s = session(tmp_path)
    before = s.memory.catalog()
    path.write_text("## 新条目\n新正文\n", encoding="utf-8")
    assert s.memory.catalog() == before  # fixed prefix: not re-read
    # send-time resolution does see the new entry
    assert "新正文" in s.memory.resolve_mentions("@mem:新条目")


def test_catalog_empty_without_file_or_entries(tmp_path):
    s = session(tmp_path)
    assert "No saved entries yet" in s.memory.catalog()
    assert "Edit MEMORY.md only when the user explicitly asks" in s.memory.catalog()
    write_memory(tmp_path, "# only prose, no entries\n")
    assert "needs repair" in session(tmp_path).memory.catalog()


def test_catalog_truncation_reports_omitted_entries(tmp_path):
    text = "".join(f"## 条目{i}\n正文{i}\n" for i in range(800))
    write_memory(tmp_path, text)
    s = session(tmp_path)
    catalog = s.memory.catalog()
    assert "catalog truncated" in catalog
    assert "of 800 entries omitted" in catalog
    # the entry list is capped; the fixed header above it (~800 chars of rules and the path) is not
    assert len(catalog) <= MAX_CATALOG_CHARS + 1200


def test_catalog_warns_when_file_is_over_the_cap(tmp_path):
    text = f"## 设计讨论使用中文\n{'x' * (MAX_MEMORY_FILE_BYTES + 1)}\n"
    write_memory(tmp_path, text)
    s = session(tmp_path)
    catalog = s.memory.catalog()
    assert f"over the {MAX_MEMORY_FILE_BYTES}-byte cap" in catalog
    assert "no entries were loaded" in catalog
    assert "- 设计讨论使用中文" not in catalog


# --- @mem: expansion ---


def test_resolve_mentions_inlines_current_body(tmp_path):
    write_memory(tmp_path)
    s = session(tmp_path)
    block = s.memory.resolve_mentions("按 @mem:设计讨论使用中文 讨论")
    assert block.startswith("--- MEMORY MENTIONS ---")
    assert "## 设计讨论使用中文" in block
    assert "用户偏好以中文讨论设计方案。" in block


def test_resolve_mentions_unknown_and_duplicate_titles_are_named_not_guessed(tmp_path):
    write_memory(tmp_path)
    s = session(tmp_path)
    path = tmp_path / "data" / "MEMORY.md"
    path.write_text(MEMORY_MD + "\n## b2c3d4e5 设计讨论使用中文\n另一个同名条目\n", encoding="utf-8")
    block = s.memory.resolve_mentions("@mem:不存在的标题 @mem:设计讨论使用中文")
    assert "不存在的标题: no memory entry with this title" in block
    assert "2 entries share this title; ask the user which one, do not guess" in block
    assert "另一个同名条目" not in block


def test_resolve_mentions_dedupes_and_caps(tmp_path):
    write_memory(tmp_path)
    s = session(tmp_path)
    block = s.memory.resolve_mentions("@mem:设计讨论使用中文 和 @mem:设计讨论使用中文")
    assert block.count("## 设计讨论使用中文") == 1

    many = "".join(f"## 条目{i}\n正文\n" for i in range(MAX_REFERENCES + 2))
    write_memory(tmp_path, many)
    store = session(tmp_path).memory
    text = " ".join(f"@mem:条目{i}" for i in range(MAX_REFERENCES + 2))
    block = store.resolve_mentions(text)
    assert block.count("## ") == MAX_REFERENCES
    assert f"2 additional memory mention(s) omitted at the {MAX_REFERENCES}-reference cap" in block


def test_resolve_mentions_without_file_reports_missing_memory(tmp_path):
    s = session(tmp_path)
    assert "cannot be resolved" in s.memory.resolve_mentions("@mem:设计讨论使用中文")


def test_resolve_mentions_refuses_oversized_file(tmp_path):
    write_memory(tmp_path, f"## 设计讨论使用中文\n{'x' * (MAX_MEMORY_FILE_BYTES + 1)}")
    s = session(tmp_path)
    assert "exceeds its" in s.memory.resolve_mentions("@mem:设计讨论使用中文")


def test_engine_attaches_memory_mentions_as_session_event(tmp_path):
    write_memory(tmp_path)
    s = session(tmp_path)
    agent = Agent(s, output_fn=lambda text: None)
    blocks = asyncio.run(agent.mention_messages("按 @mem:设计讨论使用中文 讨论"))
    assert len(blocks) == 1
    assert blocks[0]["role"] == "user"
    assert blocks[0].get(SESSION_EVENT_KEY) == "memory_mentions"
    assert "用户偏好以中文讨论设计方案。" in blocks[0]["content"]


# --- completion menu ---


def test_mem_completion_lists_titles_in_file_order_without_ids(tmp_path):
    write_memory(tmp_path)
    s = session(tmp_path)
    completer = CommandCompleter(memories=s.memory.menu_entries)
    texts = completions(completer, "按 @mem:")
    assert texts == ['@mem:"设计讨论使用中文"', '@mem:"共用部署环境"', '@mem:"无正文条目"']
    assert not any("133e87f9" in text for text in texts)


def test_mem_completion_filters_by_title_and_body_keyword(tmp_path):
    write_memory(tmp_path)
    s = session(tmp_path)
    completer = CommandCompleter(memories=s.memory.menu_entries)
    assert completions(completer, "按 @mem:部署") == ['@mem:"共用部署环境"']
    assert completions(completer, "按 @mem:核实") == ['@mem:"共用部署环境"']  # body keyword
    assert completions(completer, "按 @mem:中文") == ['@mem:"设计讨论使用中文"']
    assert completions(completer, "按 @mem:不存在") == []


def test_mem_completion_quotes_titles_with_spaces(tmp_path):
    write_memory(tmp_path, "## 共用部署环境 2026\n正文\n")
    s = session(tmp_path)
    completer = CommandCompleter(memories=s.memory.menu_entries)
    assert completions(completer, "按 @mem:共用") == ['@mem:"共用部署环境 2026"']


def test_mem_completion_row_cap_ends_in_keep_typing_notice(tmp_path):
    text = "".join(f"## 条目{i}\n正文{i}\n" for i in range(CommandCompleter.MAX_ROWS + 10))
    write_memory(tmp_path, text)
    s = session(tmp_path)
    completer = CommandCompleter(memories=s.memory.menu_entries)
    rows = list(completer.get_completions(Document("按 @mem:条目"), None))
    assert len(rows) == CommandCompleter.MAX_ROWS
    assert rows[-1].text == "@mem:条目"  # reinserts the query unchanged
    assert "keep typing" in str(rows[-1].display_text)


def test_mem_menu_shows_title_and_preview(tmp_path):
    write_memory(tmp_path)
    s = session(tmp_path)
    completer = CommandCompleter(memories=s.memory.menu_entries)
    rows = list(completer.get_completions(Document("按 @mem:"), None))
    assert rows[0].display_text == "设计讨论使用中文"
    assert "用户偏好以中文讨论设计方案。" in rows[0].display_meta_text


# --- /memory command ---


def loop_for(tmp_path):
    s = session(tmp_path)
    return s, CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)


def test_memory_command_lists_titles_bodies_and_references_without_ids(tmp_path):
    write_memory(tmp_path)
    _s, loop = loop_for(tmp_path)
    rendered = memory_command(loop, "")
    assert "### Memories · 3" in rendered
    assert "用户偏好以中文讨论设计方案。" in rendered
    assert "执行部署前应重新核实细节。" in rendered
    assert '`@mem:"设计讨论使用中文"`' in rendered
    assert "133e87f9" not in rendered


def test_memory_command_flags_duplicate_titles(tmp_path):
    write_memory(tmp_path, MEMORY_MD + "\n## b2c3d4e5 设计讨论使用中文\n另一个同名条目\n")
    _s, loop = loop_for(tmp_path)
    rendered = memory_command(loop, "")
    assert "duplicate title" in rendered
    assert "rename one" in rendered
    assert "needs repair" in rendered


def test_memory_command_hides_legacy_id_field_and_reports_unparseable_file(tmp_path):
    path = write_memory(tmp_path, "## PR body 要求\n\nid: 133e87f9\nscope: orion-arm-ai\n\n中文 Markdown。\n")
    _s, loop = loop_for(tmp_path)
    rendered = memory_command(loop, "")
    assert "PR body 要求" in rendered
    assert "中文 Markdown。" in rendered
    assert "133e87f9" not in rendered
    path.write_text("remember this but no heading\n", encoding="utf-8")
    assert "needs repair" in memory_command(loop, "")
    path.write_bytes(b"\xff")
    assert "not readable as UTF-8" in memory_command(loop, "")


def test_memory_command_empty_and_usage(tmp_path):
    _s, loop = loop_for(tmp_path)
    assert "No memories yet" in memory_command(loop, "")
    assert memory_command(loop, "extra") == "Usage: /memory"


def test_memory_command_reports_oversized_file_without_listing_partial_entries(tmp_path):
    write_memory(tmp_path, f"## 设计讨论使用中文\n{'x' * (MAX_MEMORY_FILE_BYTES + 1)}")
    _s, loop = loop_for(tmp_path)
    rendered = memory_command(loop, "")
    assert "No entries were loaded" in rendered
    assert "设计讨论使用中文" not in rendered


# --- global AGENTS.md ---


def test_environment_injects_global_agents_md_before_project(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "AGENTS.md").write_text("# Global\nAlways reply tersely.\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("# Project\nAlways run pytest.\n", encoding="utf-8")
    s = session(tmp_path)
    env = ContextManager(s).environment()
    assert "--- Global instructions" in env
    assert "Always reply tersely." in env
    assert "--- Project instructions (AGENTS.md) ---" in env
    assert env.index("--- Global instructions") < env.index("--- Project instructions")


def test_environment_global_agents_md_only(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "AGENTS.md").write_text("# Global\nGlobal only.\n", encoding="utf-8")
    s = session(tmp_path)
    env = ContextManager(s).environment()
    assert "--- Global instructions" in env
    assert "Global only." in env
    assert "--- Project instructions" not in env


def test_relative_data_dir_global_agents_md_resolves_against_session_cwd(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "AGENTS.md").write_text("Relative global instruction.\n", encoding="utf-8")
    config = Config()
    config.data_dir = "data"
    s = Session(cwd=str(tmp_path), config=config)
    bootstrap_features(s)
    assert "Relative global instruction." in ContextManager(s).environment()
    assert s.system_info.global_agents_md_source == str(data_dir / "AGENTS.md")


def test_environment_agents_md_shared_budget_keeps_both_sources(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "AGENTS.md").write_text("\n".join(f"global line {i}" for i in range(20000)), encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("\n".join(f"project line {i}" for i in range(20000)), encoding="utf-8")
    s = session(tmp_path)
    context = ContextManager(s)
    env = context.environment()
    assert env.count("truncated to fit the prefix") == 2
    global_section = env.split("--- Project instructions", 1)[0]
    global_content = global_section.split("---\n", 1)[1].strip()
    assert "global line 0" in global_content
    assert "project line 0" in env.split("--- Project instructions", 1)[1]
    assert context.estimated_text_tokens(global_content) <= MAX_AGENTS_MD_TOKENS // 2


def test_environment_unused_instruction_share_goes_to_larger_source(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "AGENTS.md").write_text("Short global instruction.\n", encoding="utf-8")
    project = "P" * 20_000  # ~5,000 estimated tokens: fits the combined budget.
    (tmp_path / "AGENTS.md").write_text(project, encoding="utf-8")
    s = session(tmp_path)
    env = ContextManager(s).environment()
    assert project in env
    assert "truncated to fit the prefix" not in env


def test_environment_memory_catalog_in_environment_and_stable(tmp_path):
    write_memory(tmp_path)
    s = session(tmp_path)
    context = ContextManager(s)
    env = context.environment()
    assert "--- MEMORY ---" in env
    assert context.model_messages("sys", [{"role": "user", "content": "request"}])[1]["content"] == "--- Environment ---\n" + env
    (tmp_path / "data" / "MEMORY.md").write_text("## 新条目\n新正文\n", encoding="utf-8")
    assert context.environment() == env


# --- tool access to the memory file outside the workspace ---


def test_read_memory_file_needs_no_confirmation(tmp_path):
    data_dir = write_memory_outside(tmp_path)
    s = session_with_data(tmp_path, data_dir)
    assert not s.in_cwd(s.memory_path())
    tool = ReadTool(s, [{"path": s.memory_path()}])
    assert not tool.needs_confirmation()
    output = tool.call()
    assert "用户偏好以中文讨论设计方案。" in output.retained_text


def test_read_other_data_dir_file_still_asks_confirmation(tmp_path):
    data_dir = write_memory_outside(tmp_path)
    sibling = data_dir / "secrets.md"
    sibling.write_text("classified\n", encoding="utf-8")
    s = session_with_data(tmp_path, data_dir)
    assert ReadTool(s, [{"path": str(sibling)}]).needs_confirmation()
    assert not s.is_memory_path(str(sibling))


def test_edit_memory_file_outside_workspace(tmp_path):
    data_dir = write_memory_outside(tmp_path)
    s = session_with_data(tmp_path, data_dir)
    tool = EditTool(s, [s.memory_path(), "", [{"op": "replace", "old": "用户偏好以中文讨论设计方案。", "content": "用户偏好以简体中文讨论设计方案。"}]])
    tool.call()
    assert "简体中文" in (data_dir / "MEMORY.md").read_text(encoding="utf-8")


def test_edit_creates_memory_file_in_existing_data_dir(tmp_path):
    data_dir = tmp_path.parent / (tmp_path.name + "-data")
    data_dir.mkdir(exist_ok=True)
    s = session_with_data(tmp_path, data_dir)
    tool = EditTool(s, [s.memory_path(), "", [{"op": "create", "content": "## 设计讨论使用中文\n用户偏好以中文讨论设计方案。\n"}]])
    tool.call()
    assert "设计讨论使用中文" in (data_dir / "MEMORY.md").read_text(encoding="utf-8")
