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
