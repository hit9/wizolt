"""`[transcript] format`: one template per settled tool call.

The engine is pure -- a source string and the facts a record already computed go in, lines come
out -- so most of it is pinned without a session. The end-to-end half goes through
`toolblocks.finish_display`, which is where only the host can guarantee anything: the two red
lines hold whatever the template said (a failed call keeps its error row, a record that shows
output keeps its citation), an unset key stays the builtin assembly byte for byte, and a
per-tool override beats the global format.
"""

from types import SimpleNamespace

import pytest
from agent_harness import session

from wizolt.base import ToolCall
from wizolt.tools import Tool, toolblocks, transcript
from wizolt.tools.toolblocks import ToolDisplay

# Five lines, so a `|tail:2` loop has something to hide and `{elided}` has a value.
OUTPUT = "l1\nl2\nl3\nl4\nl5"


def facts(**overrides):
    """The facts one settled call hands the engine."""
    base = transcript.record_values(
        tool="Bash",
        args="rg -n export_rows src",
        output=OUTPUT,
        elapsed=0.42,
        citation="tr.3",
        elided=0,
        failed=False,
        exit_code="0",
    )
    base.update(overrides)
    # `error` is the failed call's result text, the way `record_values` derives it.
    base["error"] = OUTPUT if base["failed"] else ""
    return base


def template(source: str) -> transcript.RecordTemplate:
    parsed = transcript.builtin_or_template(source)
    assert parsed is not None, f"{source!r} must not be a builtin passthrough"
    return parsed


def render(source: str, **overrides) -> list[str]:
    return template(source).render(facts(**overrides))


def text(source: str, **overrides) -> str:
    return "\n".join(render(source, **overrides))


def bash_output(stdout: str = "", stderr: str = "", code: int = 0) -> str:
    return Tool.process_result("BashToolResult", code, stdout, stderr)


# --- the engine: fields, filters, blocks -------------------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{marker}", "●"),
        ("{tool}", "Bash"),
        ("{args}", "rg -n export_rows src"),
        ("{output}", OUTPUT),
        ("{elapsed}", "0.42"),
        ("{elapsed|duration}", "0.4s"),
        ("{exit}", "0"),
        ("{citation}", "tr.3"),
        ("{elided}", "0"),
        ("{error}", ""),  # a successful call has no error text to print
        ("{tool} {args}", "Bash rg -n export_rows src"),
    ],
)
def test_a_field_renders_the_fact_it_names(source, expected):
    assert text(source) == expected


def test_the_failure_facts_describe_the_failure():
    assert text("{error}", failed=True) == OUTPUT  # the whole result is the error text
    assert text("{error|firstline}", failed=True) == "l1"
    # The flag itself is a condition, not prose: printed bare it renders no text, and the
    # condition form is what reads it.
    assert text("{failed}", failed=True) == ""
    assert text("{failed}") == ""
    assert text("{failed}{% if failed %} failed{% endif %}", failed=True) == " failed"


def test_an_absent_elapsed_prints_nothing_rather_than_zero():
    assert text("{elapsed}") == "0.42"  # a measured time is printed as raw seconds
    assert text("{elapsed}", elapsed=None) == ""
    assert text("{elapsed|duration}", elapsed=None) == ""


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{tool|lower}", "bash"),
        ("{tool|upper}", "BASH"),
        ("{error|firstline}", "l1"),
        ("{elapsed|duration}", "0.4s"),
        ("{tool|lower|upper}", "BASH"),  # filters apply left to right
    ],
)
def test_filters_bound_a_field(source, expected):
    assert text(source, failed=True) == expected


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{% for line in output %}{line}|{% endfor %}", "l1|l2|l3|l4|l5|"),
        ("{% for line in output|tail:2 %}{line}|{% endfor %}", "l4|l5|"),
        ("{% for line in output|head:2 %}{line}|{% endfor %}", "l1|l2|"),
        ("{% for line in output|tail:2 %}{line}\n{% endfor %}", "l4\nl5"),
        ("{% for line in output|tail:1 %}[{line|upper}]{% endfor %}", "[L5]"),
        ("{% for line in output|tail:99 %}{line}|{% endfor %}", "l1|l2|l3|l4|l5|"),
    ],
)
def test_a_loop_over_output_is_bounded_by_its_filters(source, expected):
    assert text(source) == expected


def test_a_multi_line_field_is_one_value_and_the_loop_is_how_you_split_it():
    """`{output}` outside a loop is the whole body as one element, so a template that wants one
    row per output line loops over it instead."""
    assert render("{output}") == [OUTPUT]
    assert render("{% for line in output %}{line}\n{% endfor %}") == ["l1", "l2", "l3", "l4", "l5"]


@pytest.mark.parametrize(
    ("source", "overrides", "expected"),
    [
        ("{% if failed %}err{% else %}ok{% endif %}", {}, "ok"),
        ("{% if failed %}err{% else %}ok{% endif %}", {"failed": True}, "err"),
        ("{% if not failed %}ok{% endif %}", {}, "ok"),
        ("{% if not failed %}ok{% endif %}", {"failed": True}, ""),
        ("{% if elided %}… +{elided} more lines{% endif %}", {"elided": 3}, "… +3 more lines"),
        ("{% if elided %}more{% endif %}", {}, ""),
        ("{% if exit %}ran{% endif %}", {"exit": ""}, ""),
    ],
)
def test_if_else_and_not_pick_one_branch(source, overrides, expected):
    assert text(source, **overrides) == expected


def test_a_template_renders_any_number_of_lines():
    assert render("{tool} {args}\n{% for line in output|tail:2 %}{line}\n{% endfor %}") == ["Bash rg -n export_rows src", "l4", "l5"]


# --- the engine: refusals ----------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "{nope}",  # unknown field
        "{tool|nope}",  # unknown filter
        "{output|tail:x}",  # a count filter needs a count
        "{% nope %}",  # unknown directive
        "{% if nope %}x{% endif %}",  # unknown condition
        "{% if failed %}x",  # unclosed if
        "{% if failed %}x{% else %}y",  # unclosed else branch
        "{% for line in output %}x",  # unclosed for
        "{% else %}",  # else outside an if
        "{% endif %}",
        "{% endfor %}",
        "{% for line in args %}{line}{% endfor %}",  # only `var in output` is supported
        "{% for tool in output %}{tool}{% endfor %}",  # the loop variable cannot shadow a field
    ],
)
def test_a_broken_template_is_a_parse_error(source):
    with pytest.raises(ValueError):
        transcript.builtin_or_template(source)


def test_an_unknown_preset_is_a_parse_error():
    with pytest.raises(ValueError, match="unknown preset"):
        transcript.builtin_or_template("preset:compact")


@pytest.mark.parametrize("source", ["", "preset:standard"])
def test_the_builtin_assembly_is_not_a_template(source):
    """Unset, empty, and `preset:standard` all name the builtin rendering; only a real template
    goes through the engine."""
    assert transcript.builtin_or_template(source) is None


def test_a_preset_other_than_standard_expands_to_its_template():
    assert template("preset:minimal").source == transcript.PRESETS["minimal"]


def test_render_record_keeps_the_builtin_path_when_there_is_no_template():
    assert transcript.render_record(None, facts()) is None
    assert transcript.render_record(template("{tool}"), facts()) == ["Bash"]


def test_a_record_that_cannot_render_its_facts_falls_back_to_builtin():
    """A bad fact never loses a record: the failure is reported as None, not raised."""
    assert transcript.render_record(template("{% for line in output|tail:2 %}{line}{% endfor %}"), {"output": None}) is None


def test_an_unknown_duration_is_rendered_as_nothing_not_as_an_error():
    """`|duration` on a fact the host has no value for says nothing, like the fact itself."""
    assert transcript.render_record(template("{elapsed|duration}"), {"elapsed": "not a number"}) == []
    assert text("{elapsed|duration}", elapsed=None) == ""


# --- `{elided}` counts what the template itself hid ---------------------------------------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("{% for line in output|tail:2 %}{line}{% endfor %}", 3),
        ("{% for line in output|head:1 %}{line}{% endfor %}", 4),
        ("{% for line in output %}{line}{% endfor %}", 0),
        ("{output}", 0),  # no loop shows the whole body, so nothing was dropped
        ("{tool} {args}", 0),
        ("{% for line in output|tail:2 %}{line}{% endfor %}{% for line in output|head:1 %}{line}{% endfor %}", 3),
    ],
)
def test_elided_counts_the_lines_the_templates_loops_dropped(source, expected):
    assert transcript.elided_count(template(source), facts()) == expected


def test_the_trailer_reports_what_the_record_really_hid(tmp_path):
    """`{elided}` is computed from the template's own loops, so the count it prints is the count
    the record dropped -- not the caller's guess."""
    s = session(tmp_path)
    s.config.transcript = {"format": "{tool} {args}\n{% for line in output|tail:2 %}{line}\n{% endfor %}{% if elided %}… +{elided} more lines{% endif %}"}

    block = toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}]), "tr.2", OUTPUT, failed=False, elapsed=0.05)

    assert str(block).splitlines()[-1] == "    └ … +3 more lines"


# --- end to end: what a settled call prints -----------------------------------------------


def test_minimal_preset_renders_a_one_line_checklist(tmp_path):
    """`preset:minimal` is the whole record on its call line: marker, lowercased tool, args, and
    the citation -- the key the viewer needs stays reachable from that one row."""
    s = session(tmp_path)
    s.config.transcript = {"format": "preset:minimal"}

    block = toolblocks.finish_display(s, ToolCall("bash-1", "Bash", ["rg -n export_rows src"]), "tr.1", bash_output("hit"), failed=False, elapsed=0.4)
    read = toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}]), "tr.2", "body", failed=False, elapsed=0.05)

    assert str(block) == "  ●  bash rg -n export_rows src → tr.1"
    assert str(read) == "  ●  read src/db/rows.rs → tr.2"


def test_a_custom_template_renders_its_own_call_line_and_output_rows(tmp_path):
    s = session(tmp_path)
    s.config.transcript = {"format": "{tool} {args}\n{% for line in output|tail:2 %}{line}\n{% endfor %}{% if elided %}… +{elided} more lines{% endif %}"}

    block = toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}]), "tr.2", OUTPUT, failed=False, elapsed=0.05)

    assert str(block) == ("  Read  src/db/rows.rs → tr.2\n    │ l4\n    │ l5\n    └ … +3 more lines")


@pytest.mark.parametrize("source", ["preset:minimal", "{tool} {citation}", "{elided}"])
def test_a_failed_call_keeps_an_error_row_whatever_the_template_said(tmp_path, source):
    """The narrowest template still cannot hide why a call failed: the error's first line is a
    host-owned row, appended after whatever the template rendered."""
    s = session(tmp_path)
    s.config.transcript = {"format": source}

    block = toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "missing.rs"}]), "tr.3", "ToolError: no such file", failed=True)

    rows = str(block).splitlines()
    assert rows[-1] == "    └ error ToolError: no such file"
    assert rows[0].endswith("[failed]")


def test_a_record_that_shows_output_keeps_its_citation(tmp_path):
    """A template that drops `{citation}` cannot make a stored result unreachable."""
    s = session(tmp_path)
    s.config.transcript = {"format": "{tool} {args}\n{% for line in output|tail:1 %}{line}{% endfor %}"}

    plain = str(toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}]), "tr.2", "line one\nline two", failed=False))
    nested = str(
        toolblocks.finish_display(
            s,
            ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}]),
            "tr.2",
            "line one\nline two",
            failed=False,
            d=ToolDisplay(nested_display=True),
        )
    )

    assert plain.splitlines()[0] == "  Read  src/db/rows.rs → tr.2"
    # Nested, the runner already drew the call line, so the record's own last row has to carry it.
    rows = nested.splitlines()
    assert rows[-1].startswith("    └ line two")
    assert rows[-1].endswith("tr.2")


def test_a_per_tool_override_beats_the_global_format(tmp_path):
    s = session(tmp_path)
    s.config.transcript = {"format": "preset:minimal", "tool": {"Bash": {"format": "{tool} {args}"}}}

    bash = str(toolblocks.finish_display(s, ToolCall("bash-1", "Bash", ["rg -n export_rows src"]), "tr.1", bash_output("hit"), failed=False, elapsed=0.4))
    read = str(toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}]), "tr.2", "body", failed=False, elapsed=0.05))

    assert bash == "  Bash  rg -n export_rows src → tr.1"
    assert read == "  ●  read src/db/rows.rs → tr.2"  # unset tools inherit the global format


def test_engine_owned_rows_survive_a_custom_template(tmp_path):
    """A record's shape never owns the host's structure: under the narrowest template an Ask call
    still shows the answer the builtin assembly draws."""
    s = session(tmp_path)
    s.config.transcript = {"format": "preset:minimal"}

    block = toolblocks.finish_display(s, ToolCall("ask-1", "Ask", [{"questions": [{"question": "Which?"}]}]), "tr.4", "typed answer", failed=False)

    rows = str(block).splitlines()
    assert rows[0].startswith("  ●  ask")
    assert rows[1] == "    └ answer typed answer"


def test_an_unparseable_format_never_costs_a_record(tmp_path):
    """Validation already reported it; the record still prints, through the builtin assembly."""
    s = session(tmp_path)
    s.config.transcript = {"format": "preset:compact"}

    display = str(toolblocks.finish_display(s, ToolCall("read-1", "Read", [{"path": "src/db/rows.rs"}]), "tr.2", "body", failed=False))

    assert display == "  Read  src/db/rows.rs → tr.2"


def test_an_unset_format_is_the_builtin_assembly_byte_for_byte(tmp_path):
    """The golden-equivalence requirement: unset, empty, and `preset:standard` all render exactly
    as the builtin assembly does -- `preset:standard` must not go near the engine."""
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


# --- the config table ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"format": "preset:standard"},
        {"format": "preset:minimal"},
        {"format": "{tool} {args}\n{% if failed %}{error|firstline}{% endif %}"},
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
        ({"format": "{% if failed %}x"}, "missing endif"),
        ({"format": "{nope}"}, "unknown field"),
        ({"format": "preset:compact"}, "unknown preset"),
        ({"tool": {"Bash": {"format": "{nope}"}}}, "transcript.tool.Bash"),
    ],
)
def test_a_broken_transcript_table_reports_the_problem(raw, message):
    problems = transcript.validate(raw)

    assert any(message in problem for problem in problems), problems


def test_effective_format_resolves_the_override_then_the_global_key():
    config = SimpleNamespace(transcript={"format": "preset:minimal", "tool": {"Bash": {"format": "{tool} {args}"}}})

    assert transcript.effective_format(config, "Bash") == "{tool} {args}"
    assert transcript.effective_format(config, "Read") == "preset:minimal"


@pytest.mark.parametrize(
    "table",
    [
        {},
        {"tool": {"Bash": {}}},
        {"tool": {"Bash": {"format": 3}}},
        {"format": 3},
        "not a table",
        None,
    ],
)
def test_effective_format_is_empty_when_nothing_usable_is_set(table):
    """An empty result is the builtin assembly, so a malformed table degrades to today's
    transcript instead of an empty one."""
    assert transcript.effective_format(SimpleNamespace(transcript=table), "Bash") == ""


def test_effective_format_tolerates_a_config_without_the_table():
    assert transcript.effective_format(SimpleNamespace(), "Bash") == ""
