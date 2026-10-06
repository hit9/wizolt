"""`[transcript] format`: one format string per row of a settled tool call's record.

The engine is pure -- a source string and the facts a record already computed go in, lines come
out -- so most of it is pinned without a session. The end-to-end half goes through
`toolblocks.finish_display`, which is where only the host can guarantee anything: the two red
lines hold whatever the format said (a failed call keeps its error row, a record that shows
output keeps its citation), an unset key stays the builtin assembly byte for byte, and a
per-tool override beats the global format.
"""

from types import SimpleNamespace

import pytest
from agent_harness import session

from wizolt.base import LogLine, LogRole, ToolCall
from wizolt.tools import Tool, toolblocks, transcript
from wizolt.tools.toolblocks import ToolDisplay

# Five lines, so a `|tail:2` row has something to hide and `{elided}` has a value.
OUTPUT = "l1\nl2\nl3\nl4\nl5"


def facts(*, output: str = OUTPUT, failed: bool = False, elapsed: float | None = 0.42, **overrides):
    """The facts one settled call hands the engine, derived by the host's own `record_values`;
    `overrides` replace a derived fact outright."""
    base = transcript.record_values(tool="Bash", args="rg -n export_rows src", output=output, elapsed=elapsed, citation="tr.3", failed=failed, exit_code="0")
    return {**base, **overrides}


def template(source: str) -> transcript.RecordTemplate:
    parsed = transcript.parsed(source)
    assert parsed is not None, f"{source!r} must not be a builtin passthrough"
    return parsed


def render(source: str, output: str = OUTPUT, **overrides) -> list[str]:
    return template(source).render(facts(output=output, **overrides), transcript.output_lines(output))


def text(source: str, output: str = OUTPUT, **overrides) -> str:
    return "\n".join(render(source, output, **overrides))


def bash_output(stdout: str = "", stderr: str = "", code: int = 0) -> str:
    return Tool.process_result("BashToolResult", code, stdout, stderr)


# --- the engine: fields and conditions, one row per line ------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{marker}", "●"),
        ("{tool}", "Bash"),
        ("{name}", "bash"),  # the tool's name in lowercase, for a quieter row
        ("{args}", "rg -n export_rows src"),
        ("{elapsed:.2f}", "0.42"),
        ("{duration}", "0.4s"),
        ("{exit}", "0"),
        ("{citation}", "tr.3"),
        ("{elided}", "0"),
        ("{tool} {args}", "Bash rg -n export_rows src"),
    ],
)
def test_a_field_renders_the_fact_it_names(source, expected):
    assert text(source) == expected


def test_the_failure_facts_describe_the_failure():
    assert text("{error}", output="ToolError: no such file\nmore", failed=True) == "ToolError: no such file"
    assert text("{% if failed %}failed: {error}{% endif %}", output="boom", failed=True) == "failed: boom"
    assert text("{% if failed %}failed{% endif %}") == ""


def test_an_unknown_time_prints_nothing_rather_than_zero():
    assert text("{duration}", elapsed=None) == "" and text("{elapsed}", elapsed=None) == ""
    # A format spec cannot format a time nobody measured: the record keeps the builtin assembly.
    assert transcript.render_record(template("{elapsed:.1f}s"), facts(elapsed=None), []) is None


@pytest.mark.parametrize(
    ("source", "overrides", "expected"),
    [
        ("{% if failed %}err{% else %}ok{% endif %}", {}, "ok"),
        ("{% if failed %}err{% else %}ok{% endif %}", {"failed": True}, "err"),
        ("{% if not failed %}ok{% endif %}", {"failed": True}, ""),
        ("{% if elided > 2 %}many{% else %}few{% endif %}", {"elided": 3}, "many"),
        ("{% if exit != '0' %}exit {exit}{% endif %}", {"exit": "2"}, "exit 2"),
    ],
)
def test_conditions_are_expressions_over_the_facts(source, overrides, expected):
    assert text(source, **overrides) == expected


def test_each_line_is_a_row_and_a_row_that_renders_nothing_disappears():
    """A conditional row needs no syntax of its own: when it renders empty it is left out, rather
    than drawn as a blank row."""
    source = "{tool} {args}\n{% if failed %}failed{% endif %}\n{citation}"
    assert render(source) == ["Bash rg -n export_rows src", "tr.3"]
    assert render(source, failed=True) == ["Bash rg -n export_rows src", "failed", "tr.3"]


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ("{output}", ["l1", "l2", "l3", "l4", "l5"]),
        ("{output|tail:2}", ["l4", "l5"]),
        ("{output|head:2}", ["l1", "l2"]),
        ("{output|tail:0}", []),  # a zero count is no lines, not every line
        ("  {output|tail:1}  ", ["l5"]),  # the reserved row is the whole line, spaces aside
    ],
)
def test_the_output_row_picks_the_calls_output_lines(row, expected):
    assert render(row) == expected


def test_output_rows_take_the_real_tail_and_are_bounded():
    long = "\n".join(f"l{n}" for n in range(1, 101))
    assert render("{output|tail:2}", long) == ["l99", "l100"]
    assert len(render("{output}", long)) == transcript.MAX_OUTPUT_LINES


def test_elided_counts_what_the_output_rows_hide():
    assert template("{output|tail:2}").shown(5) == 2
    assert template("{output|tail:2}\n{output|head:4}").shown(5) == 4  # the widest row decides
    assert template("{tool} {args}").shown(5) == 5  # showing no output hides nothing to count


# --- the engine: refusals ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        ("{nope}", "unknown field"),
        ("{tool:x}", "unknown field or format"),
        ("{% nope %}", "unexpected directive"),
        ("{% if nope %}x{% endif %}", "unknown field 'nope'"),
        ("{% if failed %}x", "unclosed template block"),
        ("{% else %}", "unexpected directive"),
        ("{output|tail:x}", "unknown field or format"),  # not the reserved row: a broken field
        ("{output} and more", "unknown field or format"),  # output is a whole row, never inline
        ("[error]{tool}[/]", "plain text"),  # a record's colors are host-owned roles
        ("{tool}{>}{args}", "plain text"),  # rows are not width-driven
        ("{% optional priority=1 %}{tool}{% endoptional %}", "plain text"),
        ("x [y]", "plain text"),
        ("x {", "unmatched delimiter"),
    ],
)
def test_a_broken_format_is_refused_with_its_row(source, reason):
    with pytest.raises(ValueError, match=reason) as raised:
        transcript.parsed(source)
    assert str(raised.value).startswith("row 1")


def test_a_problem_names_its_row_and_column():
    with pytest.raises(ValueError, match=r"^row 2: line 1, column 5: unknown field or format 'nope'"):
        transcript.parsed("{tool}\nok: {nope}")


def test_an_unknown_preset_is_refused():
    with pytest.raises(ValueError, match="unknown preset 'compact'"):
        transcript.parsed("preset:compact")


@pytest.mark.parametrize("source", ["", "  ", "preset:standard"])
def test_the_builtin_assembly_is_not_a_format(source):
    """Unset, empty, and `preset:standard` all name the builtin rendering."""
    assert transcript.parsed(source) is None


def test_a_preset_renders_as_its_rows():
    assert render("preset:minimal") == render(transcript.PRESETS["minimal"]) == ["● bash rg -n export_rows src"]


def test_a_format_is_parsed_once_however_many_records_use_it():
    """The hot path: every settled call looks its format up; parsing it each time would cost
    every record. A broken format is remembered as broken too."""
    assert transcript.parsed("{tool} {name}") is transcript.parsed("{tool} {name}")
    with pytest.raises(ValueError) as first:
        transcript.parsed("{nope} once")
    with pytest.raises(ValueError) as second:
        transcript.parsed("{nope} once")
    assert first.value is second.value


# --- end to end: what a settled call prints -----------------------------------------------


def test_minimal_preset_renders_a_one_line_checklist(tmp_path):
    """`preset:minimal` is the whole record on its call line: marker, lowercased tool, args, and
    the citation -- the key the viewer needs stays reachable from that one row."""
    s = session(tmp_path)
    s.config.transcript = {"format": "preset:minimal"}

    block = toolblocks.finish_display(s, ToolCall("bash-1", "Bash", ["rg -n export_rows src"]), "tr.1", bash_output("hit"), failed=False, elapsed=0.4)
    read = toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}]), "tr.2", "body", failed=False, elapsed=0.05)

    assert str(block) == "  ● bash  rg -n export_rows src → tr.1"
    assert str(read) == "  ● read  src/db/rows.rs → tr.2"


@pytest.mark.parametrize(
    ("source", "label", "args"),
    [
        ("preset:minimal", "● bash", "rg -n export_rows src"),  # the marker, then the name
        ("{tool} {args}", "Bash", "rg -n export_rows src"),
        ("{marker} {tool} · {args}", "● Bash", "· rg -n export_rows src"),
        ("run: {args}", "run:", "rg -n export_rows src"),  # no name early: the first word, as before
    ],
)
def test_the_tool_name_in_a_call_row_is_drawn_as_the_tool(tmp_path, source, label, args):
    """The call row's label takes the tool's color: a format that leads with a marker must not
    leave the tool's own name in plain argument text (`/theme` showed `bash` uncolored)."""
    s = session(tmp_path)
    block = toolblocks.finish_display(s, ToolCall("bash-1", "Bash", ["rg -n export_rows src"]), "tr.1", bash_output("hit"), failed=False, source=source)
    assert not isinstance(block, str)
    root = block.items[0]
    assert isinstance(root, LogLine) and (root.label, root.text) == (label, args)
    assert root.role is LogRole.TOOL


def test_a_custom_format_renders_its_own_call_line_and_output_rows(tmp_path):
    """Its closing row reports what the record itself hid: `{elided}` comes from the format's own
    output rows, not from the caller's guess."""
    s = session(tmp_path)
    s.config.transcript = {"format": "{tool} {args}\n{output|tail:2}\n{% if elided %}… +{elided} more lines{% endif %}"}

    block = toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}]), "tr.2", OUTPUT, failed=False, elapsed=0.05)

    assert str(block) == ("  Read  src/db/rows.rs → tr.2\n    │ l4\n    │ l5\n    └ … +3 more lines")


def test_a_long_output_shows_its_real_tail_and_counts_everything_before_it(tmp_path):
    s = session(tmp_path)
    s.config.transcript = {"format": "{tool}\n{output|tail:2}\n… +{elided}"}
    long = "\n".join(f"l{n}" for n in range(1, 101))

    block = toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "a.rs"}]), "tr.1", long, failed=False)

    assert [row.split()[-1] for row in str(block).splitlines()[1:]] == ["l99", "l100", "+98"]


def test_a_bash_record_shows_its_streams_not_the_stored_envelope(tmp_path):
    s = session(tmp_path)
    s.config.transcript = {"format": "{tool} exit {exit}\n{output}"}

    block = toolblocks.finish_display(s, ToolCall("bash-1", "Bash", ["make"]), "tr.1", bash_output("built", code=0), failed=False)

    assert str(block) == "  Bash  exit 0 → tr.1\n    └ built"


@pytest.mark.parametrize("source", ["preset:minimal", "{tool} {citation}", "{elided}"])
def test_a_failed_call_keeps_an_error_row_whatever_the_format_said(tmp_path, source):
    """The narrowest format still cannot hide why a call failed: the error's first line is a
    host-owned row, appended after whatever the format rendered."""
    s = session(tmp_path)
    s.config.transcript = {"format": source}

    block = toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "missing.rs"}]), "tr.3", "ToolError: no such file", failed=True)

    rows = str(block).splitlines()
    assert rows[-1] == "    └ error ToolError: no such file"
    assert rows[0].endswith("[failed]")


def test_a_refused_call_under_a_format_is_labelled_refused_like_the_builtin(tmp_path):
    s = session(tmp_path)
    s.config.transcript = {"format": "preset:minimal"}

    block = toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "a.rs"}]), "", "user refused this call", failed=True)

    rows = str(block).splitlines()
    assert rows[-1] == "    └ refused user refused this call"
    assert rows[0].endswith("[refused]")


def test_a_record_that_shows_output_keeps_its_citation(tmp_path):
    """A format that drops `{citation}` cannot make a stored result unreachable."""
    s = session(tmp_path)
    s.config.transcript = {"format": "{tool} {args}\n{output|tail:1}"}
    call = ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}])

    plain = str(toolblocks.finish_display(s, call, "tr.2", "line one\nline two", failed=False))
    nested = str(toolblocks.finish_display(s, call, "tr.2", "line one\nline two", failed=False, d=ToolDisplay(nested_display=True)))

    assert plain.splitlines()[0] == "  Read  src/db/rows.rs → tr.2"
    # Nested, the runner already drew the call line, so the record's own last row has to carry it.
    rows = nested.splitlines()
    assert rows[-1].startswith("    └ line two") and rows[-1].endswith("tr.2")


def test_a_per_tool_override_beats_the_global_format(tmp_path):
    s = session(tmp_path)
    s.config.transcript = {"format": "preset:minimal", "tool": {"Bash": {"format": "{tool} {args}"}}}

    bash = str(toolblocks.finish_display(s, ToolCall("bash-1", "Bash", ["rg -n export_rows src"]), "tr.1", bash_output("hit"), failed=False, elapsed=0.4))
    read = str(toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}]), "tr.2", "body", failed=False, elapsed=0.05))

    assert bash == "  Bash  rg -n export_rows src → tr.1"
    assert read == "  ● read  src/db/rows.rs → tr.2"  # unset tools inherit the global format


def test_engine_owned_rows_are_the_same_under_any_format(tmp_path):
    """A record's shape never owns the host's structure: under the narrowest format an Ask call
    and a ToolScript draw exactly the rows the builtin assembly draws for them."""
    s = session(tmp_path)
    ask = (ToolCall("ask-1", "Ask", [{"questions": [{"question": "Which?"}]}]), "typed answer")
    script = (ToolCall("ts-1", "ToolScript", [{"code": "print(1)"}]), '{"calls": 2, "stdout": "one\\ntwo", "error": ""}')

    def body(call, output):
        rows = str(toolblocks.finish_display(s, call, "tr.4", output, failed=False, elapsed=1.0)).splitlines()
        return rows[1:]

    builtin = [body(*ask), body(*script)]
    s.config.transcript = {"format": "preset:minimal"}
    formatted = [body(*ask), body(*script)]

    assert formatted == builtin and builtin[0] == ["    └ answer typed answer"]


def test_an_unparseable_format_never_costs_a_record(tmp_path):
    """Validation already reported it; the record still prints, through the builtin assembly."""
    s = session(tmp_path)
    s.config.transcript = {"format": "preset:compact"}

    display = str(toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}]), "tr.2", "body", failed=False))

    assert display == "  Read  src/db/rows.rs → tr.2"


def test_an_unset_format_is_the_builtin_assembly_byte_for_byte(tmp_path):
    """Unset, empty, and `preset:standard` all render exactly as the builtin assembly does --
    `preset:standard` must not go near the engine."""
    s = session(tmp_path)
    records = [
        (ToolCall("bash-1", "Bash", ["rg -n export_rows src"]), bash_output("src/db/rows.rs:12: export_rows"), False, False),
        (ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}]), "line one\nline two", False, False),
        (ToolCall("read-2", "Read", [{"path": "missing.rs"}]), "ToolError: no such file", True, False),
        (ToolCall("bash-2", "Bash", ["printf live"]), bash_output("live"), False, True),
    ]

    rendered = {}
    for label, table in (("unset", {}), ("standard", {"format": "preset:standard"}), ("blank", {"format": ""})):
        s.config.transcript = table
        rendered[label] = [
            str(toolblocks.finish_display(s, call, f"tr.{index}", output, failed=failed, elapsed=0.4, d=ToolDisplay(nested_display=nested)))
            for index, (call, output, failed, nested) in enumerate(records, start=1)
        ]

    assert rendered["unset"] == rendered["standard"] == rendered["blank"]
    assert rendered["unset"][0] == "  Bash  rg -n export_rows src → tr.1\n    └ src/db/rows.rs:12: export_rows"
    assert rendered["unset"][2] == "  Read  missing.rs → tr.3 [failed]\n    └ error ToolError: no such file"


def test_an_explicit_source_renders_without_touching_the_config(tmp_path):
    """The `/theme` sample settles its call through this same path with a draft format."""
    s = session(tmp_path)
    call = ToolCall("read-1", "Read", [{"path": "a.rs"}])

    assert str(toolblocks.finish_display(s, call, "tr.1", "body", failed=False, source="preset:minimal")) == "  ● read  a.rs → tr.1"
    assert s.config.transcript == {}


# --- the config table ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"format": "preset:standard"},
        {"format": "preset:minimal"},
        {"format": "{tool} {args}\n{% if failed %}{error}{% endif %}\n{output|tail:3}"},
        {"tool": {"Bash": {"format": "{tool} {args}"}, "Read": {"format": "preset:standard"}}},
        {"format": "preset:minimal", "tool": {"Bash": {"format": "{tool} {args}"}}},
    ],
)
def test_a_good_transcript_table_validates_clean(raw):
    assert transcript.validate(raw) == []


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not a table", "transcript must be a table"),
        ({"density": "minimal"}, "transcript: unknown settings: density"),
        ({"format": 3}, "transcript.format must be a string"),
        ({"tool": "Bash"}, "transcript.tool must be a table"),
        ({"tool": {"Bash": "preset:minimal"}}, "transcript.tool.Bash must be a table with a string format"),
        ({"tool": {"Bash": {"format": 3}}}, "transcript.tool.Bash must be a table with a string format"),
        ({"tool": {"Bash": {"density": "minimal"}}}, "transcript.tool.Bash must be a table with a string format"),
        ({"format": "{% if failed %}x"}, "transcript.format: row 1: unclosed template block"),
        ({"format": "{nope}"}, "unknown field"),
        ({"format": "preset:compact"}, "unknown preset"),
        ({"tool": {"Bash": {"format": "{nope}"}}}, "transcript.tool.Bash: row 1"),
    ],
)
def test_a_broken_transcript_table_reports_the_problem(raw, message):
    problems = transcript.validate(raw)

    assert any(message in problem for problem in problems), problems


def test_effective_format_resolves_the_override_then_the_global_key():
    config = SimpleNamespace(transcript={"format": "preset:minimal", "tool": {"Bash": {"format": "{tool} {args}"}}})

    assert transcript.effective_format(config, "Bash") == "{tool} {args}"
    assert transcript.effective_format(config, "Read") == "preset:minimal"


@pytest.mark.parametrize("table", [{}, {"tool": {"Bash": {}}}, {"tool": {"Bash": {"format": 3}}}, {"format": 3}, "not a table", None])
def test_effective_format_is_empty_when_nothing_usable_is_set(table):
    """An empty result is the builtin assembly, so a malformed table degrades to today's
    transcript instead of an empty one."""
    assert transcript.effective_format(SimpleNamespace(transcript=table), "Bash") == ""
    assert transcript.effective_format(SimpleNamespace(), "Bash") == ""
