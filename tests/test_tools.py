import asyncio
import os

import pytest

from wizolt.base import (
    LogBlock,
    LogEdge,
    LogLine,
    LogRole,
    ToolCall,
    ToolError,
)
from wizolt.config import (
    Config,
)
from wizolt.context import ContextManager
from wizolt.render import Theme, UiPrinter
from wizolt.runner import ToolRunner
from wizolt.session import Session
from wizolt.tools import (
    TOOL_REGISTRY,
    TOOLS,
    BashTool,
    EditTool,
    MCPTool,
    NoteTool,
    ReadTool,
    SkillTool,
    Tool,
    ViewImageTool,
    tool_payload,
    toolblocks,
    tooloutput,
)


def session(tmp_path):
    return Session(cwd=str(tmp_path))


def _q(*items):
    """Wrap question item dicts into the Ask tool payload args."""
    return [{"questions": list(items)}]


def test_base_tool_helpers_validate_shared_argument_contracts(tmp_path):
    class DemoTool(Tool):
        NAME = "Demo"

    tool = DemoTool(session(tmp_path), ["one", "two"])

    assert tool.strings(min_count=1, max_count=2) == ["one", "two"]
    assert tool.preview() == "Demo(one, two)"
    assert Tool.line_range([1, 3]) == (1, 3)
    assert Tool.line_range(["1", "3"]) == (1, 3)
    assert Tool.line_range([1, "3"]) == (1, 3)
    assert Tool.line_range(["0", "0"]) == (0, 0)
    assert Tool.compact({"key": "a long value"}, 16) == '{"key":"a lon...'
    assert Tool.compile_regex("needle").search("NEEDLE")
    assert not Tool.compile_regex("needle", case_sensitive=True).search("NEEDLE")

    with pytest.raises(ToolError, match="requires 1 string args"):
        DemoTool(session(tmp_path), []).strings(min_count=1, max_count=1)
    with pytest.raises(ToolError, match="args must be strings"):
        DemoTool(session(tmp_path), [1]).strings()
    with pytest.raises(ToolError, match=r"range must be \[start,end\] integers"):
        Tool.line_range([True, 2])
    with pytest.raises(ToolError, match=r"range must be \[start,end\] integers"):
        Tool.line_range([1.5, 2])
    with pytest.raises(ToolError, match=r"range must be \[start,end\] integers"):
        Tool.line_range(["318x", 560])
    with pytest.raises(ToolError, match=r"range must be \[start,end\] integers"):
        Tool.line_range(["318:560", 1])
    with pytest.raises(ToolError, match=r"range must be \[start,end\] integers"):
        Tool.line_range(["-1", 2])
    with pytest.raises(ToolError, match=r"range must be \[start,end\] integers"):
        Tool.line_range(["9" * 5000, 2])
    with pytest.raises(ToolError, match="range values must be >= 0"):
        Tool.line_range([-1, 2])
    with pytest.raises(ToolError, match="invalid regex"):
        Tool.compile_regex("[")

    assert ViewImageTool in TOOLS
    assert TOOL_REGISTRY["ViewImage"] is ViewImageTool


def test_read_accepts_ranges_in_both_array_forms_and_renders_a_view(tmp_path):
    """The old anchor string formats are gone; Read takes 1-based inclusive ranges either as one
    [start, end] pair or as a list of pairs, and its output carries a `view.N` id once rendered
    with the session's registered drafts."""
    (tmp_path / "sample.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    s = session(tmp_path)

    out = ReadTool(s, [{"path": "sample.py", "ranges": [[1, 2], [4, 4]]}]).call()
    assert "source=" not in out.retained_text  # retained text carries no view id
    keys = s.register_source_drafts(list(out.drafts))
    rendered = out.render(keys)
    assert 'source="view.1"' in rendered
    assert "1 | a" in rendered and "2 | b" in rendered and "4 | d" in rendered
    assert "3 | c" not in rendered

    # The single [start, end] form is accepted as one range.
    single = ReadTool(s, [{"path": "sample.py", "ranges": [2, 3]}]).call()
    assert "2 | b" in single.retained_text and "3 | c" in single.retained_text


def test_read_accepts_pure_decimal_string_endpoints_in_both_forms(tmp_path):
    """Some providers serialize numbers as strings; Read normalizes pure-decimal digit-string
    endpoints in both the nested and the flat range forms instead of rejecting the call."""
    (tmp_path / "sample.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    s = session(tmp_path)

    nested = ReadTool(s, [{"path": "sample.py", "ranges": [["1", "2"]]}]).call()
    assert "1 | a" in nested.retained_text and "2 | b" in nested.retained_text

    flat = ReadTool(s, [{"path": "sample.py", "ranges": ["3", "4"]}]).call()
    assert "3 | c" in flat.retained_text and "4 | d" in flat.retained_text


def test_strict_schema_handles_optional_enum_union_and_container_without_mutation():
    original = {
        "type": "object",
        "properties": {
            "required": {"type": "integer"},
            "enum": {"type": "string", "enum": ["a"]},
            "union": {"type": ["string", "null"]},
            "multi": {"type": ["string", "number"]},
            "items": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        },
        "required": ["required"],
    }

    strict = Tool._strict_schema(original)

    assert original["properties"]["enum"] == {"type": "string", "enum": ["a"]}
    assert strict["required"] == ["required", "enum", "union", "multi", "items"]
    assert strict["additionalProperties"] is False
    assert strict["properties"]["required"] == {"type": "integer"}
    assert strict["properties"]["enum"] == {"type": ["string", "null"], "enum": ["a", None]}
    assert strict["properties"]["union"] == {"type": ["string", "null"]}
    assert strict["properties"]["multi"] == {"type": ["string", "number", "null"]}
    assert strict["properties"]["items"] == {"anyOf": [{"type": "array", "items": {"type": "string"}}, {"type": "null"}]}


def test_skill_tool_without_library_reports_missing_capability(tmp_path):
    s = session(tmp_path)
    s.skills = None

    with pytest.raises(ToolError, match="no skills are installed"):
        SkillTool(s, ["missing"]).call()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ([], "at least one"),
        (["file.py"], "must be .* objects"),
        ([{"path": "file.py", "extra": True}], "unexpected field"),
        ([{"path": ""}], "non-empty path"),
        ([{"path": "file.py", "ranges": []}], "non-empty ranges"),
        ([{"path": "file.py", "ranges": [[1, "x"]]}], r"must be \[start,end\] integers"),
        ([{"path": "file.py", "ranges": [[1, 2.5]]}], r"must be \[start,end\] integers"),
        ([{"path": "file.py", "ranges": [["1x", "2"]]}], r"must be \[start,end\] integers"),
    ],
)
def test_read_target_validation_is_actionable(tmp_path, args, message):
    with pytest.raises(ToolError, match=message):
        ReadTool(session(tmp_path), args).targets()


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"unknown": True}, "unexpected field"),
        ({"append_known": "fact"}, "append_known must be an array"),
        ({"replace_known": "fact"}, "replace_known must be an array"),
        ({"action": "update"}, "update requires"),
    ],
)
def test_note_validation_errors_are_actionable(tmp_path, payload, message):
    with pytest.raises(ToolError, match=message):
        NoteTool(session(tmp_path), [payload]).call()


async def test_reading_a_materialized_tool_output_needs_no_confirmation(tmp_path):
    """The marker of a truncated result hands the model an absolute path outside the workspace, so
    the out-of-workspace prompt would stop the model from reading a file wizolt itself wrote and
    told it to read. Assets are exempt; anything else outside the workspace still asks."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    s = Session(cwd=str(workspace), config=Config(data_dir=str(tmp_path / "data")))
    large = "\n".join(f"line {index}" for index in range(20000))
    key = s.store_tool_result("Bash", ["big"], large)
    ContextManager(s).bound_output(large, path=await ContextManager(s).materialize_output(key, large))
    asset = os.path.join(s.images.assets_dir(), key + ".txt")
    outside = tmp_path / "elsewhere.txt"
    outside.write_text("private\n", encoding="utf-8")

    assert ReadTool(s, [{"path": asset}]).needs_confirmation() is False
    assert ReadTool(s, [{"path": str(outside)}]).needs_confirmation() is True
    # Reading it really does return the full output the marker promised.
    assert "line 19999" in ReadTool(s, [{"path": asset}]).call().retained_text


async def test_mcp_tool_handles_missing_manager_and_invalid_arguments(tmp_path):
    s = session(tmp_path)
    s.mcp = None
    tool = MCPTool(s, [{"action": "call", "server": "docs", "tool": "read", "arguments": {}}])

    assert tool.needs_confirmation() is False
    with pytest.raises(ToolError, match="MCP not configured"):
        await tool.call()
    with pytest.raises(ToolError, match="arguments must be an object"):
        await MCPTool(s, [{"action": "call", "server": "docs", "tool": "read", "arguments": []}]).call()


async def test_read_success_paths(tmp_path):
    (tmp_path / "sample.py").write_text("alpha\nNeedle\nomega\n", encoding="utf-8")
    (tmp_path / "blob.bin").write_bytes(b"a\0b")
    s = session(tmp_path)

    read = ReadTool(s, [{"path": "sample.py", "ranges": [[1, 2], [3, 0]]}]).call()
    single_range = ReadTool(s, [{"path": "sample.py", "ranges": [1, 2]}]).call()
    full_default = ReadTool(s, [{"path": "sample.py"}]).call()
    assert "<Read path=" in read.retained_text
    assert "1 | alpha" in read.retained_text
    assert "2 | Needle" in read.retained_text
    assert "3 | omega" in read.retained_text
    assert "1 | alpha" in single_range.retained_text
    assert "2 | Needle" in single_range.retained_text
    assert "3 | omega" in full_default.retained_text
    assert "total_lines=3" in full_default.retained_text  # Read reports the line count


async def test_read_and_edit_report_one_based_line_numbers(tmp_path):
    """Read and Edit must number lines the way `grep -n`, tracebacks, and diffs do, so a line
    number seen in one place can be used in another without adjustment."""
    s = session(tmp_path)
    lines = ["alpha", "beta", "Needle", "omega"]  # grep -n numbers these 1..4
    (tmp_path / "sample.py").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # A 1-based inclusive range returns exactly the requested lines, and no others.
    read = ReadTool(s, [{"path": "sample.py", "ranges": [[2, 3]]}]).call()
    assert 'lines="2:3"' in read.retained_text
    assert "2 | beta" in read.retained_text
    assert "3 | Needle" in read.retained_text
    assert "alpha" not in read.retained_text and "omega" not in read.retained_text

    # A view taken from that output edits the line it names, and nothing shifts by one.
    key = s.register_source_drafts(list(read.drafts))[0]
    EditTool(s, ["sample.py", key, [{"op": "replace", "start": 3, "end": 3, "content": "FOUND\n"}]]).call()
    assert (tmp_path / "sample.py").read_text(encoding="utf-8") == "alpha\nbeta\nFOUND\nomega\n"


def test_read_echoes_ranges_the_model_could_have_written(tmp_path):
    """short_args is echoed back to the model in the tool message, not just printed in the
    terminal, so the "read to the end of the file" sentinel must not surface as a literal 0."""
    s = session(tmp_path)
    (tmp_path / "a.py").write_text("one\ntwo\nthree\n", encoding="utf-8")

    def echo(args):
        return ReadTool(s, args).short_args()

    assert echo([{"path": "a.py"}]) == ["a.py"]  # whole file: no range, matching the omitted input
    assert echo([{"path": "a.py", "ranges": [[1, 0]]}]) == ["a.py"]
    assert echo([{"path": "a.py", "ranges": [[2, 0]]}]) == ["a.py 2:"]  # line 2 to the end
    assert echo([{"path": "a.py", "ranges": [[2, 3]]}]) == ["a.py 2:3"]
    assert echo([{"path": "a.py", "ranges": [[1, 1], [3, 0]]}]) == ["a.py 1:1,3:"]

    # Omitting ranges and passing null (what strict-schema providers send) mean the same thing.
    assert ReadTool.payload_args({"path": "a.py"}) == ReadTool.payload_args({"path": "a.py", "ranges": None})


def test_one_based_edit_targets_never_shift_the_wrong_line(tmp_path):
    """View line numbers are 1-based and Edit uses the same 1-based inclusive start/end, so a line
    number seen in one place edits exactly that line -- nothing is one off."""
    s = session(tmp_path)
    (tmp_path / "code.py").write_text("first\nsecond\nthird\n", encoding="utf-8")

    out = ReadTool(s, [{"path": "code.py"}]).call()
    key = s.register_source_drafts(list(out.drafts))[0]

    # 1-based line 3 names "third", not the second line a 0-based scheme would hit.
    EditTool(s, ["code.py", key, [{"op": "replace", "start": 3, "end": 3, "content": "THIRD\n"}]]).call()
    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "first\nsecond\nTHIRD\n"

    # A duplicated line is still targeted by its explicit line number; no guessing is involved.
    (tmp_path / "dup.py").write_text("head\nsame\nsame\n", encoding="utf-8")
    dup = ReadTool(s, [{"path": "dup.py"}]).call()
    dup_key = s.register_source_drafts(list(dup.drafts))[0]
    EditTool(s, ["dup.py", dup_key, [{"op": "replace", "start": 2, "end": 2, "content": "ONE\n"}]]).call()
    assert (tmp_path / "dup.py").read_text(encoding="utf-8") == "head\nONE\nsame\n"


def test_reject_collapses_display(tmp_path):
    s = session(tmp_path)
    out = []
    runner = ToolRunner(s, ContextManager(s), output_fn=lambda text: out.append(str(text)))

    msg = runner.reject(ToolCall("c", "Read", [{"path": "x"}]), "ToolError: Read requires non-empty ranges")

    # display collapses to one quiet line, no full [failed]/error block
    assert any("· rejected: Read requires non-empty ranges" in t for t in out)
    assert not any("[failed]" in t or t.startswith("  error ") for t in out)
    # model still receives the full error
    assert "Read requires non-empty ranges" in msg


def test_read_missing_file_is_a_rejection_not_a_failure(tmp_path):
    # A missing file is a usage-level error the model self-corrects, like Edit's "file does
    # not exist": it must raise ToolError so the runner renders the quiet dim one-liner,
    # not the red [failed] block reserved for execution failures.
    with pytest.raises(ToolError, match="no such file"):
        ReadTool(session(tmp_path), [{"path": "missing.py"}]).call()
    with pytest.raises(ToolError, match="cannot read"):
        ReadTool(session(tmp_path), [{"path": "."}]).call()

    s = session(tmp_path)
    out = []
    runner = ToolRunner(s, ContextManager(s), output_fn=lambda text: out.append(str(text)))
    messages = asyncio.run(runner.run([ToolCall("c", "Read", [{"path": "missing.py"}])]))

    assert "no such file" in str(messages[0]["content"])
    assert any("· rejected: no such file" in t for t in out)
    assert not any("[failed]" in t for t in out)


def test_single_and_batch_payload_shapes_are_supported():
    assert tool_payload("Read", {"path": "a.py"}) == [{"path": "a.py", "ranges": [[1, 0]]}]
    assert tool_payload("Read", {"path": "a.py", "ranges": [0, 2]}) == [{"path": "a.py", "ranges": [[0, 2]]}]
    assert tool_payload("Read", {"files": [{"path": "a.py", "ranges": [[0, 1]]}]}) == [{"path": "a.py", "ranges": [[0, 1]]}]
    assert ReadTool(Session(cwd="."), [{"path": "wizolt.py"}]).targets()[0][1] == [(1, 0)]
    assert tool_payload("Note", {"set_goal": "ship"}) == [{"set_goal": "ship"}]


def test_tool_runner_finish_display_keeps_ask_answer(tmp_path):
    s = session(tmp_path)

    display = str(toolblocks.finish_display(s, ToolCall("ask", "Ask", _q({"question": "Which?"})), "tr.1", "typed answer", failed=False))

    assert display.startswith("  Ask  Which? → tr.1\n")
    assert display.endswith("    └ answer typed answer")


def test_tool_runner_reject_records_error_and_returns_rejected_message(tmp_path):
    s = Session(cwd=str(tmp_path))
    runner = ToolRunner(s, ContextManager(s))
    call = ToolCall("e1", "Bash", ["bad cmd"])
    out = []
    runner.output_fn = out.append
    result = runner.reject(call, "ToolError: command not found")
    assert len(out) == 1
    assert isinstance(out[0], LogBlock)
    assert "command not found" in str(out[0])
    # Should record the error
    assert len(s.tool_errors) == 1
    assert s.tool_errors[0].name == "Bash"
    assert "command not found" in s.tool_errors[0].error
    # reject returns a plain-text tool-message representation
    assert "status: rejected" in result
    assert "command not found" in result


async def test_run_one_rejects_tools_outside_session_whitelist(tmp_path):
    s = session(tmp_path)
    runner = ToolRunner(s, ContextManager(s), input_fn=lambda prompt: "y")

    s.tool_names = ("Read",)
    (message,) = await runner.run([ToolCall("c1", "Bash", ["echo hi"])])
    content = str(message["content"])
    assert "status: rejected" in content
    assert "ToolError: Bash is not available in this session" in content

    # Empty tuple = no filtering (parent behavior): the same call executes.
    s.tool_names = ()
    (message,) = await runner.run([ToolCall("c2", "Bash", ["echo hi"])])
    assert "hi" in str(message["content"])


def test_parallel_safe_false_for_tools_outside_whitelist(tmp_path):
    s = session(tmp_path)
    runner = ToolRunner(s, ContextManager(s))

    # Read is normally parallel-safe, but the whitelist excludes it, so it falls back to serial
    # run_one (where the whitelist gate rejects it) instead of execute_readonly.
    s.tool_names = ("Bash",)
    assert not runner.parallel_safe(ToolCall("c1", "Read", [{"path": "a.txt"}]))

    # Empty tuple = no filtering: parallel-safety is decided as before.
    s.tool_names = ()
    assert runner.parallel_safe(ToolCall("c2", "Read", [{"path": "a.txt"}]))


def test_tool_runner_short_call_formats_note(tmp_path):
    s = session(tmp_path)
    runner = ToolRunner(s, ContextManager(s), output_fn=lambda text: None)

    s.state.known = ["existing"]
    note = tooloutput.short_call(
        runner.session,
        ToolCall(
            "m",
            "Note",
            [
                {
                    "set_goal": "ship",
                    "replace_plan": [{"status": "doing", "text": "inspect"}, {"status": "todo", "text": "patch"}],
                    "append_known": ["existing", "new fact"],
                }
            ],
        ),
    )
    assert note == "Note goal: ship\nplan:\n  - [~] inspect\n  - [ ] patch\nknown:\n  + new fact"


def test_tool_schemas_are_strict_for_high_risk_tools():
    bash_params = BashTool.schema()["function"]["parameters"]
    assert bash_params["required"] == ["command"]
    assert bash_params["properties"]["command"]["pattern"] == r"^[\s\S]*\S[\s\S]*$"
    bash_description = BashTool.schema()["function"]["description"]
    assert "conditionals, loops, functions, pipelines, and multiline scripts" in bash_description
    # Said once. The parameter used to repeat that same list and the tool's "Bound noisy output",
    # which is a whole sentence of the schema budget spent to tell the model what it just read.
    assert "loops" not in bash_params["properties"]["command"]["description"]
    # Where the command runs is part of the call, not remembered from an earlier one.
    assert "workdir" in bash_params["properties"]

    edit_params = EditTool.schema()["function"]["parameters"]
    assert edit_params["required"] == ["path", "edits"]
    assert set(edit_params["properties"]) == {"edits", "path", "source"}
    assert "source=view.N from Read " in EditTool.schema()["function"]["description"]
    edits_schema = edit_params["properties"]["edits"]
    assert edits_schema["items"]["required"] == ["op"]
    assert edits_schema["items"]["properties"]["op"]["enum"] == ["create", "replace", "delete"]
    assert "never omit" in edits_schema["items"]["properties"]["op"]["description"]
    assert "every item requires op" in edits_schema["description"]

    read_params = ReadTool.schema()["function"]["parameters"]
    assert {"path", "ranges", "files"} <= set(read_params["properties"])

    note_params = NoteTool.schema()["function"]["parameters"]
    assert "across context compaction" in NoteTool.schema()["function"]["description"]
    note_description = NoteTool.schema()["function"]["description"]
    assert "non-trivial work" in note_description
    # Both halves of the threshold, because only the negative half suppresses the reflex to open
    # every task with a plan. Quantified rather than "when appropriate", which reads as "always".
    assert "easiest quarter" in note_description and "single-step plan" in note_description
    assert "minItems" not in note_params["properties"]["replace_plan"]
    status = note_params["properties"]["replace_plan"]["items"]["properties"]["status"]
    assert status["enum"] == ["todo", "doing", "done", "blocked"]
    assert "minItems" not in note_params["properties"]["replace_known"]

    def walk(value):
        if isinstance(value, dict):
            assert "anyOf" not in value
            assert "prefixItems" not in value
            if isinstance(value.get("pattern"), str):
                assert value["pattern"].startswith("^")
                assert value["pattern"].endswith("$")
            if "items" in value:
                assert isinstance(value["items"], dict)
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for tool in TOOLS:
        params = tool.schema()["function"]["parameters"]
        assert "args" not in params.get("properties", {})
        walk(tool.schema())


async def test_tool_validation_rejects_bad_shapes_without_side_effects(tmp_path):
    s = session(tmp_path)
    (tmp_path / "sample.py").write_text("alpha\n", encoding="utf-8")

    with pytest.raises(ToolError):
        ReadTool(s, [{"path": "sample.py", "ranges": []}]).call()
    with pytest.raises(ToolError):
        EditTool(s, ["a.txt", [{"op": "bogus", "content": "a\n"}]]).call()
    with pytest.raises(ToolError):
        await BashTool(s, []).call()

    assert not (tmp_path / "a.txt").exists()
    assert not (tmp_path / "b.txt").exists()


def test_prose_call_lines_are_not_lexed_as_arguments(tmp_path):
    """Note and Ask carry prose, so its commas and words keep the plain text style."""
    session(tmp_path)
    for display in ("Note check: HEAD = 878d86a, tag created; not pushed", "Ask Push now, or wait?"):
        line = toolblocks.log_root(display)
        segments = UiPrinter(output_fn=lambda text: None).log_segments(LogBlock([line]))

        assert line.syntax == ""
        assert (Theme.fg("text"), "  " + display.partition(" ")[2]) in segments


def test_uiprinter_highlights_generic_tool_arguments(tmp_path):
    session(tmp_path)
    line = toolblocks.log_root('Read "done in" glob=*.py C=2')

    assert line.syntax == "tool-args"
    segments = UiPrinter(output_fn=lambda text: None).log_segments(LogBlock([line]))
    assert (Theme.fg("syntax_string"), '"done in"') in segments
    assert (Theme.fg("syntax_assign"), "glob=") in segments
    assert (Theme.fg("syntax_number"), "2") in segments


def test_uiprinter_renders_note_memory_status_colors():
    ui = UiPrinter(output_fn=lambda text: None)
    segs = ui.segments("goal: ship\ncheck: passed\nplan:\n  - [~] inspect\n  - [x] patch\nknown:\n  + pytest")

    assert (Theme.fg("accent_secondary"), "goal: ship") in segs
    assert (Theme.fg("accent_secondary"), "check: passed") in segs
    assert (Theme.fg("accent"), "plan:") in segs
    assert (Theme.fg("warning"), "  - [~] inspect") in segs
    assert (Theme.fg("success"), "  - [x] patch") in segs
    assert (Theme.fg("success"), "  + pytest") in segs


def test_uiprinter_renders_rejected_line_dim():
    ui = UiPrinter(output_fn=lambda text: None)
    segs = ui.log_segments(LogBlock([LogLine("Read", "· rejected: needs ranges", LogRole.MUTED)]))

    assert any(style == Theme.fg("muted") and "rejected" in text for style, text in segs)
    assert not any(style in (Theme.fg("error"), Theme.fg("success")) for style, text in segs)


def test_uiprinter_renders_stored_result_dim():
    ui = UiPrinter(output_fn=lambda text: None)
    block = LogBlock.hierarchy(None, [LogLine("stored", "tr.50 [approved]", LogRole.META, LogEdge.END)])

    assert ui.log_segments(block) == [
        ("", "    "),
        (Theme.fg("subtle"), "└ "),
        (Theme.fg("muted"), "stored"),
        (Theme.fg("muted"), " tr.50 [approved]"),
        ("", "\n"),
    ]


def test_uiprinter_renders_tool_root_without_generic_prefix():
    block = LogBlock([LogLine("Read", "wizolt.py 0:100 → tr.6 [auto]", LogRole.TOOL)])
    segments = UiPrinter(output_fn=lambda text: None).log_segments(block)
    text = "".join(value for _, value in segments)

    assert text == "  Read  wizolt.py 0:100 → tr.6 [auto]\n"
    assert any(style == "fg:default" and "wizolt.py 0:100 → tr.6 [auto]" in value for style, value in segments)


async def test_mixed_batch_whitelisted_tool_runs_and_excluded_rejected(tmp_path):
    """A batch mixing an excluded parallel-safe tool with a whitelisted one: the segment router
    never hands the excluded name to execute_readonly, so each call gets its own verdict."""
    s = session(tmp_path)
    s.settings.max_parallel_tools = 4
    s.tool_names = ("Read",)
    (tmp_path / "a.txt").write_text("hello\n", encoding="utf-8")
    runner = ToolRunner(s, ContextManager(s), input_fn=lambda prompt: "y")

    calls = [
        ToolCall("s1", "ViewImage", [{"path": "a.png"}]),
        ToolCall("r1", "Read", [{"path": "a.txt"}]),
        ToolCall("s2", "ViewImage", [{"path": "a.png"}]),
    ]
    contents = {message["tool_call_id"]: str(message["content"]) for message in await runner.run(calls)}
    assert "hello" in contents["r1"]
    assert "ViewImage is not available in this session" in contents["s1"]
    assert "ViewImage is not available in this session" in contents["s2"]


def test_read_merges_one_view_per_path_across_request_items(tmp_path):
    """A batched Read emits one block and one view per file, not one per requested range. Ranges
    for the same path are unioned, sorted, and merged when they overlap or touch, so the `lines`
    label the model sees is exactly the set of spans the view can validate an edit against."""
    (tmp_path / "a.py").write_text("".join(f"a{index}\n" for index in range(1, 11)), encoding="utf-8")
    (tmp_path / "b.py").write_text("b1\n", encoding="utf-8")
    s = session(tmp_path)

    out = ReadTool(
        s,
        [
            {"path": "a.py", "ranges": [[5, 6]]},
            {"path": "b.py", "ranges": [[1, 1]]},
            {"path": "a.py", "ranges": [[1, 2], [3, 4], [9, 10]]},  # touching 1:2+3:4, disjoint 9:10
        ],
    ).call()
    keys = s.register_source_drafts(list(out.drafts))

    assert len(keys) == 2
    a_view = s.get_source_view(keys[0])
    assert [(span.start, span.end) for span in a_view.spans] == [(1, 6), (9, 10)]
    assert 'lines="1:6,9:10"' in out.retained_text
    assert out.retained_text.count("<Read ") == 2

    # A range inside a span resolves; one crossing the gap between spans is refused as unseen.
    assert a_view.range_lines(5, 6) == ("a5\n", "a6\n")
    with pytest.raises(ToolError, match="source range unseen"):
        a_view.range_lines(6, 9)

