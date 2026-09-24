"""The built-in JSON repair is json_repair (the PyPI package) in miniature.

It exists for one path -- a compaction summary json.loads rejects -- and keeps only what that
path reads, so the guarantee that matters is agreement: on every malformed shape it claims to
read, it returns what the original returns. json_repair is a dev-only dependency, imported here
as the reference and never by wizolt itself.
"""

import pytest
from json_repair import repair_json as reference

from wizolt.utils.json_repair import repair_json_object

AGREEMENT_CASES = [
    '{"summary": "kept"',
    'summary follows: {"summary": "kept"} trailing words',
    "{'summary': 'kept'}",
    '{summary: "kept"}',
    '{"summary": kept}',
    '{"summary": "kept",}',
    '{"a": [1, 2, {"b": "c"',
    '{"a": True, "b": None, "c": false}',
    '{"n": -3.5e2, "m": 7, "x": 0.5',
    '{\n  "summary": "multi",  # the notes\n  "topics": ["a", "b"],\n}',
    '{"summary": “wide quotes” and „low“ ones"}',
    '{"a":}',
    '{"a": "x" "b": "y"}',
    '{"a": "uni \\u00e9 scal", "b": "bad \\uZZ esc", "c": "cut \\u12", "d": "stray \\q mark"}',
    '{"a": "one\\ntwo", "b": "quote \\" inside"',
    '{"summary": "edge \\ trailing"',
    "{",  # a summary truncated to its opening brace: an empty object, as the reference reads it
    "Here is your JSON: {",
    "{ }",
]


@pytest.mark.parametrize("text", AGREEMENT_CASES)
def test_agrees_with_the_reference_reader(text):
    """On every malformed shape the built-in reader claims to read, it returns the original's object."""
    assert repair_json_object(text) == reference(text, return_objects=True)


@pytest.mark.parametrize("text", ["not json at all", "[1, 2]", "options {a, b}", '{"a"}', "{a, b}", "x { y"])
def test_recovers_no_object_where_the_reference_yields_none_either(text):
    """Prose, a bare array, and braces with no key-value pair in them (code samples, stray
    braces): neither reader yields an object, so the compaction parse rejects all of them."""
    assert repair_json_object(text) is None
    assert not isinstance(reference(text, return_objects=True), dict)


def test_pathological_nesting_is_not_a_crash():
    """An output that is nothing but open braces is not a summary; the reader walks past it
    instead of raising through the compaction, however deep the stack allows."""
    repair_json_object('{"a": ' * 600)


def test_nesting_past_the_depth_cap_stops_descending():
    """The reference refuses nesting past its own supported depth outright; the built-in reader
    stops descending at its cap, so pathological output never spends a real stack or arrives as
    a four-thousand-deep summary -- and the brace it steps past leaves nothing glued to a key."""
    result = repair_json_object('{"a": ' * 4000 + "1" + "}" * 4000)
    assert isinstance(result, dict)
    nested, depth = result, 0
    while isinstance(nested, dict):
        nested = nested["a"]
        depth += 1
    assert depth < 100  # the input nests 4000 deep; the cap stopped the descent far above

    keys = set()
    stack = [result]
    while stack:
        for key, value in stack.pop().items():
            keys.add(key)
            if isinstance(value, dict):
                stack.append(value)
    assert keys == {"a"}  # the brace the cap stepped past left nothing glued to a key


def test_depth_cap_leftovers_never_touch_the_shallow_keys():
    """Objects and arrays alternating past the cap leave braces glued to keys in the garbage
    below -- accepted: that text is nobody's summary, and the shallow keys a compaction reads
    ("summary" and its siblings) stay clean above it."""
    result = repair_json_object('{"summary": "kept", "a": ' + '["b": ' * 2000 + "1" + "]" * 2000 + "}")
    assert result["summary"] == "kept"


def test_brace_soup_costs_no_more_than_a_capped_number_of_passes():
    """Every open brace once cost a pass over the whole text -- quadratic on malformed output;
    the attempt cap keeps it linear, and the answer is still that there is no object."""
    assert repair_json_object("{a" * 4096) is None


def test_a_real_object_behind_many_stray_braces_still_wins():
    """The cap leaves room for the prose-and-code shapes a compaction reply actually has; the
    summary after them is still the object that comes back."""
    text = "options {a, b} and {c} — " * 8 + 'the summary: {"summary": "kept"}'
    assert repair_json_object(text) == {"summary": "kept"}
