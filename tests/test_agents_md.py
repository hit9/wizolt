"""AGENTS.md instruction sources: section parsing, `@agents.md:` references, the completion menu,
resolution against the session snapshot and the disk, the fixed context prefix, and the tool and
runtime boundaries that treat the global file as the user's own durable store."""

import pytest
from prompt_toolkit.document import Document
from tui_harness import loop as command_loop_for

from wizolt.agentsmd import (
    MAX_REFERENCES,
    AgentsFile,
    AgentsReferenceError,
    MenuRow,
    Reference,
    display_path,
    global_agents_md_path,
)
from wizolt.base import SESSION_EVENT_KEY, ToolError
from wizolt.cli import CommandCompleter, TuiRuntime
from wizolt.cli.commands import status
from wizolt.cli.loop import CommandLoop
from wizolt.config import Config
from wizolt.context import ContextManager
from wizolt.engine import Agent
from wizolt.mentions import active_mention, scan_mentions
from wizolt.session import Session, bootstrap_features
from wizolt.tools import EditTool, ReadTool
from wizolt.tui import TuiApp

GLOBAL_TEXT = "# House style\nFour spaces for indentation.\n"
PROJECT_TEXT = "# Rules\nAlways run pytest.\n\n# Contributing\nPR body goes here.\n"


def agents_session(tmp_path, *, global_text=None, project_text=None, project_name="AGENTS.md", cwd_name=""):
    """A session whose data dir holds the global file and whose cwd holds the project one.

    Both files must exist before construction: the session snapshots them into SystemInfo once.
    `cwd_name` moves the workspace into a subdirectory, so the default data dir sits outside it."""

    work = tmp_path / cwd_name if cwd_name else tmp_path
    work.mkdir(parents=True, exist_ok=True)
    data = tmp_path / "data"
    data.mkdir(parents=True, exist_ok=True)
    if global_text is not None:
        (data / "AGENTS.md").write_text(global_text, encoding="utf-8")
    if project_text is not None:
        (work / project_name).write_text(project_text, encoding="utf-8")
    config = Config()
    config.data_dir = str(data)
    s = Session(cwd=str(work), config=config)
    bootstrap_features(s)
    return s


def agents_file(content, scope="project", display="./AGENTS.md", path="/workspace/AGENTS.md"):
    return AgentsFile(scope, path, display, content)


def completions(completer, text):
    return [completion.text for completion in completer.get_completions(Document(text), None)]


# --- sections ---


def test_sections_close_at_the_same_or_higher_level_and_parents_keep_their_subsections():
    content = "# Top\nfirst\n\n## Nested\nnested text\n\n### Deep\ndeep text\n\n# Second\nsecond text\n"
    f = agents_file(content)

    # Every section stands on its own (nested ones after their parent, in document order), so a
    # heading path of any depth resolves.
    assert [section.path for section in f.sections()] == [("Top",), ("Top", "Nested"), ("Top", "Nested", "Deep"), ("Second",)]
    top = f.sections()[0]
    assert top.text.startswith("# Top\nfirst\n")
    assert "## Nested\nnested text" in top.text  # a subsection is part of its parent's text
    assert "### Deep\ndeep text" in top.text
    assert "second text" not in top.text
    assert f.sections()[3].text.startswith("# Second\nsecond text")
    assert f.section_text("Top") == top.text
    assert f.section_text("Second") is not None
    assert f.section_text("Top/Nested").startswith("## Nested")
    assert f.section_text("Top/Nested/Deep").startswith("### Deep")


def test_text_before_the_first_heading_belongs_to_the_file_only(tmp_path):
    s = agents_session(tmp_path, project_text="preamble rule\n\n# Rules\nbody\n")
    project = s.agents.current_sources()[0]

    assert [section.heading for section in project.sections()] == ["Rules"]
    assert "preamble" not in project.sections()[0].text
    # The whole-file reference still carries it verbatim; the section reference does not.
    assert "preamble rule" in s.agents.resolve_mentions("@agents.md:project")
    assert "preamble rule" not in s.agents.resolve_mentions("@agents.md:project/Rules")


def test_a_heading_inside_a_fence_does_not_open_a_section():
    content = "# Real\nintro\n```python\n# LooksReal\nbody\n```\ntail\n# Later\nsecond\n"
    f = agents_file(content)

    assert [section.path for section in f.sections()] == [("Real",), ("Later",)]
    assert "```python\n# LooksReal\nbody\n```" in f.sections()[0].text  # kept verbatim, never a heading
    assert f.section_text("LooksReal") is None
    assert "intro" in f.sections()[0].text and "tail" in f.sections()[0].text


def test_sections_preserve_original_line_endings_and_blank_lines():
    content = "   # Parent\r\nfirst\r\n\r\n   ## Child ###\r\nchild\r\n\r\n"
    f = agents_file(content)

    assert [section.heading for section in f.sections()] == ["Parent", "Parent/Child"]
    assert f.section_text("Parent") == content
    assert f.section_text("Parent/Child") == "   ## Child ###\r\nchild\r\n\r\n"


def test_tilde_fences_and_longer_closing_fences_close_a_block():
    content = "# Real\n~~~\n# Fake\n~~~~\nstill inside? no\n# Later\nsecond\n"
    f = agents_file(content)

    assert [section.path for section in f.sections()] == [("Real",), ("Later",)]
    assert f.section_text("Fake") is None
    # The longer tilde run closed the block, so "still inside? no" is read as ordinary text.
    assert "still inside? no" in f.sections()[0].text


def test_an_unclosed_fence_swallows_the_rest_of_the_file():
    f = agents_file("# Real\n```\n# NotASection\n###### NeitherIsThis\n")

    assert [section.heading for section in f.sections()] == ["Real"]


def test_a_six_hashes_heading_is_a_heading_and_a_bare_hash_has_no_visible_path():
    deep = agents_file("# Real\nbefore\n\n###### Six\nsix text\n")
    assert [section.path for section in deep.sections()] == [("Real",), ("Real", "Six")]
    assert "###### Six\nsix text" in deep.sections()[0].text

    bare = agents_file("#\nbody\n")
    assert [section.path for section in bare.sections()] == [("",)]
    assert bare.sections()[0].heading == ""
    assert bare.sections()[0].text == "#\nbody\n"


def test_a_heading_with_no_body_is_still_a_section_with_a_row():
    f = agents_file("# Empty\n")

    assert [section.text for section in f.sections()] == ["# Empty\n"]
    assert f.section_text("Empty") == "# Empty\n"
    assert f.menu_rows()[1] == MenuRow("  # Empty", "", '@agents.md:"project/Empty"', "# Empty\n", "Project › Empty")


def test_loading_a_missing_file_yields_nothing(tmp_path):
    assert AgentsFile.load("project", str(tmp_path / "missing.md"), "./AGENTS.md") is None
    assert AgentsFile.project_file(str(tmp_path), "") is None
    (tmp_path / "AGENTS.md").write_text("body\n", encoding="utf-8")
    loaded = AgentsFile.project_file(str(tmp_path), "AGENTS.md")
    assert loaded is not None and loaded.content == "body\n" and loaded.display == "./AGENTS.md"


# --- reference encoding and scanning ---


@pytest.mark.parametrize(
    ("text", "payload", "scope", "heading"),
    [
        ("@agents.md:", "", "", ""),
        ("@agents.md:global", "global", "global", ""),
        ("@agents.md:project/Rules", "project/Rules", "project", "Rules"),
        ('@agents.md:"project/PR body"', "project/PR body", "project", "PR body"),
        ('@agents.md:"global/Contributing/PR body"', "global/Contributing/PR body", "global", "Contributing/PR body"),
    ],
)
def test_agents_mentions_scan_both_the_bare_and_the_quoted_forms(text, payload, scope, heading):
    spans = [span for span in scan_mentions("cite " + text + " please") if span.kind == "agents"]

    assert [(span.payload, span.complete) for span in spans] == [(payload, True)]
    reference = Reference.spans("cite " + text + " please")[0]
    assert (reference.payload, reference.scope, reference.heading) == (payload, scope, heading)


def test_reference_encoding_is_canonical_and_round_trips():
    f = agents_file("# Rules\nbody\n")
    assert f.reference() == "@agents.md:project"
    assert f.reference("Rules") == '@agents.md:"project/Rules"'

    spaced = agents_file("# PR body\nbody\n")
    encoded = spaced.reference("PR body")
    assert encoded == '@agents.md:"project/PR body"'  # a space forces the quoted, JSON-escaped form
    assert Reference.spans(encoded)[0].heading == "PR body"

    global_file = agents_file("# Contributing\nbody\n", scope="global", display="~/.wizolt/AGENTS.md")
    nested = global_file.reference("Contributing/PR body")
    assert nested == '@agents.md:"global/Contributing/PR body"'
    assert Reference.spans(nested)[0].payload == "global/Contributing/PR body"


def test_section_references_with_unicode_or_punctuation_round_trip_beside_prose():
    f = agents_file("# 中文规则\nbody\n# C++\nmore\n")
    for heading in ("中文规则", "C++"):
        inserted = f.reference(heading)
        assert inserted == f'@agents.md:"project/{heading}"'
        assert Reference.spans(inserted + "继续写")[0].heading == heading
        assert f.section_text(Reference.spans(inserted)[0].heading) is not None


def test_incomplete_quoted_reference_is_reported_instead_of_ignored(tmp_path):
    s = agents_session(tmp_path, project_text=PROJECT_TEXT)

    assert "incomplete @agents.md:" in s.agents.validation_error('cite @agents.md:"project/Rules')


def test_mention_scanning_keeps_addresses_and_other_namespaces_apart():
    assert scan_mentions("mail hit9@icloud.com") == []  # `@` follows a word character
    assert scan_mentions("a@b.com") == []
    assert [span.kind for span in scan_mentions("@file:a.py @agents.md:global @skill:release")] == ["file", "agents", "skill"]
    assert [span.payload for span in scan_mentions("@agents.md:global and @agents.md:project/Rules")] == ["global", "project/Rules"]
    assert [span.payload for span in scan_mentions("@agents.md:project/中文规则")] == ["project/中文规则"]
    assert [span.payload for span in scan_mentions("@agents.md:  then text")] == [""]  # the all-applicable form


def test_the_active_mention_at_the_cursor_is_an_agents_span():
    span = active_mention("use @agents.md:global")
    assert span is not None and span.kind == "agents" and span.payload == "global"
    assert active_mention("use @agents.md:") is not None
    assert active_mention("use @agents.md:global now") is None  # the cursor left the span


# --- menu rows ---


def test_the_whole_file_row_leads_a_files_own_rows():
    f = agents_file("# Rules\nAlways run pytest.\n")
    rows = f.menu_rows()

    assert rows[0] == MenuRow("Project · ./AGENTS.md", "whole file", "@agents.md:project")
    assert [row.display for row in rows[1:]] == ["  # Rules"]
    assert rows[1].insert == '@agents.md:"project/Rules"'
    assert rows[1].meta == "Always run pytest."  # the original body, not a paraphrase


def test_a_section_row_shows_a_bounded_excerpt_of_the_original_text():
    row = agents_file("# Long\n" + "word " * 60 + "\n").menu_rows()[1]

    assert row.meta.startswith("word word")
    assert row.meta.endswith("...")
    assert len(row.meta) <= AgentsFile.EXCERPT_LIMIT


def test_nested_headings_show_a_tree_and_filtered_results_show_their_path():
    f = agents_file("# 全局规则\n\n## PR body 要求\n适用范围：后端项目\n", scope="global", display="~/.wizolt/AGENTS.md")
    rows = f.menu_rows()

    assert [row.display for row in rows] == ["Global · ~/.wizolt/AGENTS.md", "  # 全局规则", "    ## PR body 要求"]
    assert [row.meta for row in rows] == ["whole file", "", "适用范围：后端项目"]
    completer = CommandCompleter(agents_rows=f.menu_rows)
    match = list(completer.get_completions(Document('@agents.md:"PR body'), None))
    assert (match[-1].display_text, match[-1].display_meta_text, match[-1].text) == (
        "Global › 全局规则 › PR body 要求",
        "适用范围：后端项目",
        '@agents.md:"global/全局规则/PR body 要求"',
    )


def test_a_fenced_heading_stays_in_its_parents_preview():
    rows = agents_file("# Rules\n```md\n# example\n```\n## Child\nchild body\n").menu_rows()

    assert rows[1].meta == "```md # example ```"
    assert rows[2].display == "    ## Child"


def test_an_unheaded_file_still_offers_its_whole_file_row():
    f = agents_file("just prose, no headings\n")

    assert f.sections() == []
    assert [row.insert for row in f.menu_rows()] == ["@agents.md:project"]


def test_the_session_menu_leads_with_all_applicable_then_each_file_and_section(tmp_path):
    s = agents_session(tmp_path, global_text=GLOBAL_TEXT, project_text=PROJECT_TEXT)
    display = display_path(global_agents_md_path(s.config.data_dir))
    rows = s.agents.menu_rows()

    assert rows[0] == MenuRow("All applicable", "every instructions file below", "@agents.md:")
    assert [row.display for row in rows] == [
        "All applicable",
        f"Global · {display}",
        "  # House style",
        "Project · ./AGENTS.md",
        "  # Rules",
        "  # Contributing",
    ]
    # A heading title with a space is inserted in the quoted, round-trippable form.
    assert [row.insert for row in rows] == [
        "@agents.md:",
        "@agents.md:global",
        '@agents.md:"global/House style"',
        "@agents.md:project",
        '@agents.md:"project/Rules"',
        '@agents.md:"project/Contributing"',
    ]


def test_the_menu_labels_a_claude_fallback_project_file(tmp_path):
    s = agents_session(tmp_path, project_text="# Claude rules\nbody\n", project_name="CLAUDE.md")

    assert [row.display for row in s.agents.menu_rows()] == ["All applicable", "Project · ./CLAUDE.md", "  # Claude rules"]
    assert "body" in s.agents.resolve_mentions("@agents.md:project")


def test_menu_filters_before_capping_so_late_sections_remain_findable(tmp_path):
    s = agents_session(tmp_path, project_text="".join(f"# Heading {index}\nbody\n" for index in range(80)))
    rows = s.agents.menu_rows()
    completer = CommandCompleter(agents_rows=s.agents.menu_rows)

    assert len(rows) == 82
    assert rows[0].insert == "@agents.md:"
    assert len(completions(completer, "cite @agents.md:")) == CommandCompleter.MAX_ROWS
    assert completions(completer, "cite @agents.md:79") == ['@agents.md:"project/Heading 79"']


def test_matching_rows_filter_by_source_heading_and_original_text(tmp_path):
    s = agents_session(tmp_path, global_text=GLOBAL_TEXT, project_text=PROJECT_TEXT)
    completer = CommandCompleter(agents_rows=s.agents.menu_rows)

    assert completions(completer, "cite @agents.md:contributing") == ['@agents.md:"project/Contributing"']
    assert completions(completer, 'cite @agents.md:"HOUSE STYLE') == ['@agents.md:"global/House style"']
    assert completions(completer, 'cite @agents.md:"Always run') == ['@agents.md:"project/Rules"']
    assert completions(completer, 'cite @agents.md:"./AGENTS.md') == ["@agents.md:project"]
    assert completions(completer, "cite @agents.md:nothing") == []
    assert completions(completer, "cite @agents.md:") == [row.insert for row in s.agents.menu_rows()]

    long_body = agents_session(tmp_path / "long", project_text="# Short\n" + "x" * 120 + " distinctive tail\n")
    assert completions(CommandCompleter(agents_rows=long_body.agents.menu_rows), "cite @agents.md:distinctive") == ['@agents.md:"project/Short"']


# --- resolution ---


def test_the_bare_reference_expands_every_loaded_file_in_prefix_order(tmp_path):
    s = agents_session(tmp_path, global_text=GLOBAL_TEXT, project_text=PROJECT_TEXT)
    block = s.agents.resolve_mentions("please apply @agents.md:")

    assert block.startswith("--- AGENTS.MD REFERENCES ---\n")
    assert "the original text follows" in block
    assert block.index("House style") < block.index("Always run pytest.")  # global before project
    assert "[global · " + display_path(global_agents_md_path(s.config.data_dir)) + "]" in block
    assert "[project · ./AGENTS.md]" in block
    assert "Clipped" not in block


def test_file_and_section_references_attach_the_original_text(tmp_path):
    s = agents_session(tmp_path, global_text=GLOBAL_TEXT, project_text=PROJECT_TEXT)

    section = s.agents.resolve_mentions("cite @agents.md:project/Rules")
    assert "[project · ./AGENTS.md]" in section
    assert "# Rules\nAlways run pytest." in section
    assert "House style" not in section and "PR body goes here." not in section

    whole = s.agents.resolve_mentions("cite @agents.md:global")
    assert "House style" in whole and "Four spaces for indentation." in whole
    assert "Always run pytest." not in whole


def test_repeated_references_are_deduplicated(tmp_path):
    s = agents_session(tmp_path, project_text=PROJECT_TEXT)
    block = s.agents.resolve_mentions("a @agents.md:project/Rules b @agents.md:project/Rules c")

    assert block.count("[project · ./AGENTS.md]") == 1
    assert block.count("Always run pytest.") == 1


def test_text_without_a_reference_expands_to_nothing(tmp_path):
    s = agents_session(tmp_path, project_text=PROJECT_TEXT)

    assert s.agents.resolve_mentions("no mentions here") == ""
    assert s.agents.resolve_mentions("mail hit9@icloud.com") == ""
    assert s.agents.validation_error("no mentions here") is None


def test_unknown_scope_and_unknown_heading_raise_with_a_helpful_message(tmp_path):
    s = agents_session(tmp_path, global_text=GLOBAL_TEXT, project_text=PROJECT_TEXT)

    with pytest.raises(AgentsReferenceError) as error:
        s.agents.resolve_mentions("@agents.md:workspace")
    assert 'unknown @agents.md reference "workspace"' in str(error.value)
    assert "available sources: global, project" in str(error.value)

    with pytest.raises(AgentsReferenceError) as error:
        s.agents.resolve_mentions("@agents.md:project/Nonexistent")
    assert 'no section "Nonexistent"' in str(error.value)
    assert "project · ./AGENTS.md" in str(error.value)
    assert s.agents.validation_error("@agents.md:project/Nonexistent") == str(error.value)
    assert 'needs a section heading after "/"' in s.agents.validation_error("@agents.md:project/")


def test_a_reference_with_no_loaded_file_says_so(tmp_path):
    s = agents_session(tmp_path)

    assert s.agents.current_sources() == ()
    with pytest.raises(AgentsReferenceError, match="no AGENTS.md source"):
        s.agents.resolve_mentions("cite @agents.md:")
    assert s.agents.resolve_mentions("no reference") == ""
    assert s.agents.validation_error("@agents.md:global") == 'unknown @agents.md reference "global" (available sources: none is loaded)'


def test_duplicate_heading_paths_are_reported_as_ambiguous(tmp_path):
    s = agents_session(tmp_path, project_text="# Same\nfirst\n\n# Same\nsecond\n")

    with pytest.raises(AgentsReferenceError) as error:
        s.agents.resolve_mentions("@agents.md:project/Same")
    message = str(error.value)
    assert "ambiguous" in message and "2 sections share that heading path" in message
    assert s.agents.validation_error("@agents.md:project/Same") == message


def test_a_nested_heading_resolves_by_its_full_path(tmp_path):
    s = agents_session(tmp_path, project_text="# Contributing\nsteps\n\n## PR body\nthe body text\n")
    block = s.agents.resolve_mentions('@agents.md:"project/Contributing/PR body"')

    assert "the body text" in block
    assert "steps" not in block


def test_references_are_capped(tmp_path):
    s = agents_session(tmp_path, project_text="".join(f"# Heading{index}\nbody {index}\n" for index in range(MAX_REFERENCES + 2)))
    text = " ".join(f"@agents.md:project/Heading{index}" for index in range(MAX_REFERENCES + 2))
    with pytest.raises(AgentsReferenceError, match=f"maximum {MAX_REFERENCES}"):
        s.agents.resolve_mentions(text)
    assert f"maximum {MAX_REFERENCES}" in s.agents.validation_error(text)


def test_a_clipped_reference_keeps_the_head_and_tail_and_names_the_readable_path(tmp_path, monkeypatch):
    monkeypatch.setattr("wizolt.agentsmd.TOKEN_CAP", 200)  # 800 characters including labels and notices
    content = "# Global\n" + "g" * 1000 + "\nfooter rule\n"
    s = agents_session(tmp_path, global_text=content)
    block = s.agents.resolve_mentions("cite @agents.md:global")

    marker = "clipped to fit the shared reference budget; read the full file for the rest"
    assert f"... (global · {display_path(global_agents_md_path(s.config.data_dir))} {marker}) ..." in block
    assert global_agents_md_path(s.config.data_dir) in block  # the header names the path a Read can open
    assert "Clipped to fit the shared reference budget; read the full file: " in block
    assert block.startswith("--- AGENTS.MD REFERENCES ---")
    body = block.split("--- AGENTS.MD REFERENCES ---", 1)[1]
    assert body.index("# Global") < body.index(marker) < body.index("footer rule")  # head, marker, tail
    assert "g" * 500 not in block  # the omitted middle really is gone
    label = f"[global · {display_path(global_agents_md_path(s.config.data_dir))}]"
    cited = block.split(label, 1)[1].strip()
    assert len(cited) <= 800  # the shared budget, not the file, decides how much text is cited
    assert len(block) <= 800  # metadata also counts against the shared cap


def test_one_shared_cap_clips_oversized_references_with_a_marker_naming_the_file(tmp_path, monkeypatch):
    monkeypatch.setattr("wizolt.agentsmd.TOKEN_CAP", 200)  # 800 characters for every reference together
    global_text = "# Global\n" + "g" * 1000 + "\n"
    s = agents_session(tmp_path, global_text=global_text, project_text="# Project\n" + "p" * 1000 + "\n")
    block = s.agents.resolve_mentions("cite @agents.md:")

    assert "Clipped to fit the shared reference budget; read the full file: " in block
    # The header names both readable paths, and each clipped body says where the rest lives.
    assert f"read the full file: {global_agents_md_path(s.config.data_dir)}, {tmp_path / 'AGENTS.md'}" in block
    assert "global · " + display_path(global_agents_md_path(s.config.data_dir)) in block
    assert "[global · " in block and "[project · ./AGENTS.md]" in block  # the second file is not dropped
    assert "g" in block and "p" in block  # both files receive part of the shared budget
    assert len(block) <= 800  # labels and paths stay within the cap too


def test_a_reference_small_enough_to_fit_is_never_clipped(tmp_path, monkeypatch):
    monkeypatch.setattr("wizolt.agentsmd.TOKEN_CAP", 100)
    s = agents_session(tmp_path, project_text="# Rules\nshort\n")
    block = s.agents.resolve_mentions("@agents.md:project/Rules")

    assert "# Rules\nshort" in block
    assert "clipped" not in block.lower()


def test_chinese_reference_text_stays_within_the_shared_byte_budget(tmp_path, monkeypatch):
    monkeypatch.setattr("wizolt.agentsmd.TOKEN_CAP", 200)
    s = agents_session(tmp_path, global_text="# 规则\n" + "中文偏好" * 500 + "\n")

    block = s.agents.resolve_mentions("@agents.md:global")

    assert "Clipped to fit" in block
    assert len(block.encode("utf-8")) <= 800


# --- the session snapshot versus the disk ---


def test_the_menu_and_expansion_read_current_file_while_the_prefix_keeps_its_snapshot(tmp_path):
    s = agents_session(tmp_path, project_text="# Rules\nold body\n")
    (tmp_path / "AGENTS.md").write_text("# Rules\nnew body\n\n# Added later\nfresh\n", encoding="utf-8")

    assert "old body" in s.system_info.agents_md  # the fixed prefix keeps the session snapshot
    assert [row.display for row in s.agents.menu_rows()] == ["All applicable", "Project · ./AGENTS.md", "  # Rules", "  # Added later"]
    assert completions(CommandCompleter(agents_rows=s.agents.menu_rows), "@agents.md:Added") == ['@agents.md:"project/Added later"']
    assert "Added later" in s.agents.resolve_mentions("@agents.md:project")  # expansion reads the disk
    assert "fresh" in s.agents.resolve_mentions('@agents.md:"project/Added later"')  # a space needs the quoted form
    assert "old body" in s.system_info.agents_md


def test_a_global_file_created_after_session_start_appears_in_the_menu(tmp_path):
    s = agents_session(tmp_path)
    assert [row.insert for row in s.agents.menu_rows()] == ["@agents.md:"]
    (tmp_path / "data" / "AGENTS.md").write_text("# Global\nlate arrival\n", encoding="utf-8")

    assert [row.insert for row in s.agents.menu_rows()] == ["@agents.md:", "@agents.md:global", '@agents.md:"global/Global"']
    assert completions(CommandCompleter(agents_rows=s.agents.menu_rows), "@agents.md:late") == ['@agents.md:"global/Global"']
    assert "late arrival" in s.agents.resolve_mentions("@agents.md:global")
    assert "late arrival" in s.agents.resolve_mentions("@agents.md:")


def test_a_project_file_created_after_session_start_appears_in_the_menu(tmp_path):
    s = agents_session(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# New project rule\napply it\n", encoding="utf-8")

    assert [row.insert for row in s.agents.menu_rows()] == ["@agents.md:", "@agents.md:project", '@agents.md:"project/New project rule"']
    assert "apply it" in s.agents.resolve_mentions('@agents.md:"project/New project rule"')
    assert not s.system_info.agents_md  # the automatic prefix still waits for the next session


def test_a_deleted_source_stops_resolving(tmp_path):
    s = agents_session(tmp_path, project_text=PROJECT_TEXT)
    (tmp_path / "AGENTS.md").unlink()

    assert s.agents.validation_error("@agents.md:project") == 'unknown @agents.md reference "project" (available sources: none is loaded)'
    assert [row.insert for row in s.agents.menu_rows()] == ["@agents.md:"]
    assert s.system_info.agents_md  # the prefix still keeps its snapshot


# --- the fixed context prefix ---


def test_the_prefix_puts_global_before_project(tmp_path):
    s = agents_session(tmp_path, global_text=GLOBAL_TEXT, project_text=PROJECT_TEXT)
    env = ContextManager(s).environment()

    display = display_path(global_agents_md_path(s.config.data_dir))
    assert f"--- Global instructions ({display}) ---" in env
    assert "--- Project instructions (AGENTS.md) ---" in env
    assert env.index("--- Global instructions") < env.index("--- Project instructions")
    assert env.index("House style") < env.index("Always run pytest.")
    assert env.startswith("- cwd: ")  # the rows of the Environment body, not a second header


def test_the_prefix_clips_both_sources_under_one_shared_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("wizolt.context.MAX_AGENTS_MD_TOKENS", 200)
    s = agents_session(tmp_path, global_text="# Global\n" + "g" * 4000 + "\n", project_text="# Project\n" + "p" * 4000 + "\n")
    context = ContextManager(s)
    env = context.environment()
    global_part = env.split("--- Global instructions", 1)[1].split("--- Project instructions", 1)[0]
    global_body = global_part.split(") ---\n", 1)[1]
    project_body = env.split("--- Project instructions (AGENTS.md) ---", 1)[1].strip("\n")

    assert "truncated to fit the prefix" in global_body
    assert display_path(global_agents_md_path(s.config.data_dir)) in global_body  # the marker names the file
    # The project source is reserved its room first: the global source yields, not the project.
    assert "# Project" in project_body
    assert "(AGENTS.md truncated to fit the prefix;" in project_body  # the marker names its own file too
    # One shared budget: each source's rendered body stays within the cap.
    assert context.estimated_text_tokens(global_body) <= 200
    assert context.estimated_text_tokens(project_body) <= 200
    assert context.estimated_text_tokens(global_body) + context.estimated_text_tokens(project_body) <= 200
    assert "g" * 100 in global_body and "p" * 100 in project_body  # each source keeps a useful share


def test_chinese_instructions_respect_the_prefix_budget(tmp_path, monkeypatch):
    monkeypatch.setattr("wizolt.context.MAX_AGENTS_MD_TOKENS", 200)
    s = agents_session(tmp_path, global_text="# 规则\n" + "中文偏好" * 500 + "\n")
    body = ContextManager(s).environment().split("--- Global instructions", 1)[1].split(") ---\n", 1)[1]

    assert "truncated to fit the prefix" in body
    assert len(body.encode("utf-8")) <= 800


def test_absent_sources_add_no_rows(tmp_path):
    s = agents_session(tmp_path)
    context = ContextManager(s)

    enabled = context.environment()
    assert "instructions" not in enabled
    s.settings.agents_md = False
    assert context.environment() == enabled  # byte-identical with the feature on or off


def test_only_the_loaded_source_gets_a_prefix_row(tmp_path):
    global_only = agents_session(tmp_path, global_text=GLOBAL_TEXT, cwd_name="work")
    env = ContextManager(global_only).environment()
    assert "--- Global instructions" in env and "--- Project instructions" not in env

    project_only = agents_session(tmp_path / "other", project_text=PROJECT_TEXT)
    env = ContextManager(project_only).environment()
    assert "--- Global instructions" not in env and "--- Project instructions (AGENTS.md) ---" in env


# --- the agent turn ---


async def test_an_agents_reference_lands_as_a_session_event_block(tmp_path):
    s = agents_session(tmp_path, global_text=GLOBAL_TEXT, project_text=PROJECT_TEXT)
    agent = Agent(s, output_fn=lambda _text: None)

    class FakeModel:
        def __init__(self):
            self.requests = []

        async def request(self, messages, tools=None):
            self.requests.append(messages)
            return {"role": "assistant", "content": "done"}, [], "done"

    agent.model = FakeModel()
    assert await agent.run("apply @agents.md:project/Rules") == "done"

    contents = [str(message.get("content") or "") for message in agent.model.requests[0] if message["role"] == "user"]
    assert "apply @agents.md:project/Rules" in contents  # the user's text is never rewritten
    blocks = [message for message in agent.model.requests[0] if message.get(SESSION_EVENT_KEY) == "agents_mentions"]
    assert len(blocks) == 1
    assert "# Rules\nAlways run pytest." in blocks[0]["content"]
    assert "[project · ./AGENTS.md]" in blocks[0]["content"]


async def test_an_unresolvable_reference_becomes_an_explicit_error_block(tmp_path):
    s = agents_session(tmp_path, project_text=PROJECT_TEXT)
    agent = Agent(s, output_fn=lambda _text: None)

    class FakeModel:
        def __init__(self):
            self.requests = []

        async def request(self, messages, tools=None):
            self.requests.append(messages)
            return {"role": "assistant", "content": "done"}, [], "done"

    agent.model = FakeModel()
    assert await agent.run("cite @agents.md:project/Missing") == "done"

    blocks = [message for message in agent.model.requests[0] if message.get(SESSION_EVENT_KEY) == "agents_mentions"]
    assert len(blocks) == 1
    assert blocks[0]["content"].startswith("--- AGENTS.MD REFERENCES ---\n")
    assert "unknown @agents.md reference" in blocks[0]["content"]


# --- admission ---


async def test_a_submission_whose_reference_no_longer_resolves_is_refused(tmp_path):
    command_loop = command_loop_for(tmp_path)
    command_loop.tui = TuiApp()
    runtime = TuiRuntime(command_loop)
    draft = "cite @agents.md:project/Missing"

    assert await runtime._admit_input(draft) is None

    assert "unknown @agents.md reference" in command_loop.tui.input_error
    assert command_loop.tui.input_buffer.text == draft  # the draft goes back to the editor
    assert command_loop.session.pending_user_inputs == []


async def test_a_resolvable_reference_is_admitted_unchanged(tmp_path):
    (tmp_path / "AGENTS.md").write_text(PROJECT_TEXT, encoding="utf-8")
    command_loop = command_loop_for(tmp_path)
    command_loop.tui = TuiApp()
    runtime = TuiRuntime(command_loop)
    draft = "cite @agents.md:project"

    admitted = await runtime._admit_input(draft)

    assert admitted is not None and str(admitted) == draft  # expansion happens in the turn, not here
    assert command_loop.tui.input_error == ""


# --- Read and Edit on the global file ---


def test_read_of_the_global_agents_md_needs_no_confirmation(tmp_path):
    s = agents_session(tmp_path, global_text=GLOBAL_TEXT, cwd_name="work")
    global_path = global_agents_md_path(s.config.data_dir)

    assert not s.in_cwd(global_path)  # the data dir sits outside this workspace
    assert ReadTool(s, [{"path": global_path}]).needs_confirmation() is False
    other = tmp_path / "data" / "notes.txt"
    other.write_text("not instructions\n", encoding="utf-8")
    assert ReadTool(s, [{"path": str(other)}]).needs_confirmation() is True
    assert not s.is_global_agents_md(str(other))
    assert "House style" in ReadTool(s, [{"path": global_path}]).call().retained_text  # and it reads

    alias = tmp_path / "global-alias.md"
    alias.symlink_to(global_path)
    assert not s.is_global_agents_md(str(alias))
    assert ReadTool(s, [{"path": str(alias)}]).needs_confirmation() is True


def test_edit_recovery_shows_the_global_agents_md_but_not_other_outside_paths(tmp_path):
    s = agents_session(tmp_path, global_text=GLOBAL_TEXT, cwd_name="work")
    global_path = global_agents_md_path(s.config.data_dir)
    edits = [{"op": "replace", "start": 1, "end": 1, "content": "# Rewritten\n"}]

    with pytest.raises(ToolError) as error:
        EditTool(s, [global_path, "view.99", edits]).call()
    assert "use the fresh view below" in str(error.value)
    recovery = error.value.recovery
    assert recovery is not None and "House style" in recovery.retained_text  # the file's current lines

    outside = tmp_path / "data" / "notes.txt"
    outside.write_text("not instructions\n", encoding="utf-8")
    with pytest.raises(ToolError) as error:
        EditTool(s, [str(outside), "view.99", edits]).call()
    assert "Read or Search again" in str(error.value)
    assert error.value.recovery is None  # an outside path is not projected into a refusal


def test_edit_can_create_the_global_file_when_it_does_not_exist(tmp_path):
    s = agents_session(tmp_path, cwd_name="work")
    global_path = global_agents_md_path(s.config.data_dir)

    EditTool(s, [global_path, "", [{"op": "create", "content": "# Rule\nKeep it short.\n"}]]).call()

    assert (tmp_path / "data" / "AGENTS.md").read_text(encoding="utf-8") == "# Rule\nKeep it short.\n"


def test_edit_mixed_evidence_preflights_the_drop_source_repair_for_the_global_file(tmp_path):
    s = agents_session(tmp_path, global_text=GLOBAL_TEXT, cwd_name="work")
    edits = [{"op": "replace", "old": "Four spaces for indentation.\n", "content": "Two spaces.\n"}]

    with pytest.raises(ToolError, match="mixed edit evidence") as error:
        EditTool(s, [global_agents_md_path(s.config.data_dir), "view.99", edits]).parse()
    assert "dropping source and resending these edits as one direct call would succeed" in str(error.value)

    outside = tmp_path / "work" / "notes.txt"
    outside.write_text("Four spaces for indentation.\n", encoding="utf-8")
    with pytest.raises(ToolError, match="mixed edit evidence") as error:
        EditTool(s, [str(tmp_path / "data" / "notes.txt"), "view.99", edits]).parse()
    assert "dropping source" not in str(error.value)  # outside the workspace: the refusal stands alone


# --- the /status row ---


def test_status_names_both_instruction_sources(tmp_path):
    s = agents_session(tmp_path, global_text=GLOBAL_TEXT, project_text=PROJECT_TEXT)
    command_loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)

    result = status(command_loop, "")
    assert "agents.md on (./AGENTS.md; global active)" in result
    assert "| global AGENTS.md |" not in result


def test_status_names_the_one_source_it_has(tmp_path):
    s = agents_session(tmp_path, project_text=PROJECT_TEXT)
    command_loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)
    assert "agents.md on (./AGENTS.md; global missing)" in status(command_loop, "")

    s.settings.agents_md = False
    result = status(command_loop, "")
    assert "agents.md off (global missing)" in result


def test_status_distinguishes_a_new_global_file_from_one_loaded_at_session_start(tmp_path):
    s = agents_session(tmp_path)
    command_loop = CommandLoop(Agent(s, output_fn=lambda text: None), output_fn=lambda text: None)
    path = global_agents_md_path(s.config.data_dir)

    assert "agents.md on (global missing)" in status(command_loop, "")
    (tmp_path / "data" / "AGENTS.md").write_text("# Rules\n", encoding="utf-8")
    assert "agents.md on (global next session)" in status(command_loop, "")
    assert f"- wizolt_global_agents_md: {path}" in ContextManager(s).environment()
    assert "auto-injected in this session: no" in ContextManager(s).environment()

    next_session = agents_session(tmp_path, global_text="# Rules\n")
    next_loop = CommandLoop(Agent(next_session, output_fn=lambda text: None), output_fn=lambda text: None)
    assert "agents.md on (global active)" in status(next_loop, "")
    assert "auto-injected in this session: yes" in ContextManager(next_session).environment()


# --- completion ---


AGENTS_ROWS = [
    MenuRow("All applicable", "every instructions file below", "@agents.md:"),
    MenuRow("global · ~/.wizolt/AGENTS.md", "whole file", "@agents.md:global"),
    MenuRow("Contributing", "project · # Contributing PR body text", '@agents.md:"project/Contributing"'),
]


def test_the_bare_menu_offers_the_agents_namespace_after_the_other_kinds():
    completer = CommandCompleter(mcp_servers=lambda: ("github",), skills=lambda: ("release",), files=lambda: ())

    assert completions(completer, "use @") == ["@file:", "@mcp:", "@skill:", "@agents.md:"]


def test_agents_completions_come_from_the_rows_callable_including_all_applicable():
    completer = CommandCompleter(agents_rows=lambda: list(AGENTS_ROWS))

    assert completions(completer, "cite @agents.md:") == [row.insert for row in AGENTS_ROWS]
    assert completions(completer, "cite @agents.md:") == completions(completer, "cite @agents.md:")  # deterministic
    assert completions(CommandCompleter(), "cite @agents.md:") == []  # no session rows, no candidates


@pytest.mark.parametrize(
    ("typed", "expected"),
    [
        ("cite @agents.md:glob", ["@agents.md:global"]),  # by source
        ("cite @agents.md:contri", ['@agents.md:"project/Contributing"']),  # by heading
        ("cite @agents.md:body", ['@agents.md:"project/Contributing"']),  # by the original text in the row
        ('cite @agents.md:"PR body', ['@agents.md:"project/Contributing"']),  # the quoted form filters too
        ("cite @agents.md:nothing", []),
    ],
)
def test_agents_completions_filter_by_source_heading_and_excerpt(typed, expected):
    completer = CommandCompleter(agents_rows=lambda: list(AGENTS_ROWS))

    assert completions(completer, typed) == expected


def test_agents_completions_are_capped():
    rows = [MenuRow("All applicable", "every instructions file below", "@agents.md:")]
    rows += [MenuRow(f"Heading {index}", f"project · body {index}", f"@agents.md:project/Heading{index}") for index in range(60)]
    completer = CommandCompleter(agents_rows=lambda: rows)

    assert len(completions(completer, "cite @agents.md:")) == CommandCompleter.MAX_ROWS
