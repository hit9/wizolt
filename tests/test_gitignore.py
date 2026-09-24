"""The built-in .gitignore reader is pathspec's GitIgnoreSpec in miniature.

It exists for one walk -- @-mention completion in a workspace with no Git and no ripgrep -- so
the guarantee that matters is agreement: on every pattern shape that walk reads, it decides
each path the same. pathspec is a dev-only dependency, imported here as the reference and never
by wizolt itself. Three declared divergences are asserted below: `a/**` leaves the directory
`a` itself undecided, where the reference marks it ignored; `dir/*` does the same so that a
later `!dir/keep.txt` can still reach its file; and `dir/**/` stops one segment short of `dir`
itself and the files directly inside it. Git agrees with all three, and the walk's resulting
file set matches Git either way.
"""

import pytest
from pathspec import GitIgnoreSpec

from wizolt.utils.gitignore import GitIgnore

CORPUS_ONE_RULES = [
    "# a comment",
    "",
    "  ",
    "*.tmp",
    "!keep.tmp",
    "*.py[cod]",
    "build/",
    "/rooted.txt",
    "docs/*.md",
    "**/deep.log",
    "a/**",
    "**/anywhere.cfg",
    "temp?",
    "sub/dir/file.txt",
    "!docs/keep.md",
    "\\#hash.txt",
    "spaces   ",
    "node_modules/",
    "!.gitkeep",
    "q?est/[a-c].txt",
    "**/logs/",
    "!**/trace.log",
    "*",
    "!root.py",
]

CORPUS_ONE_PATHS = [
    ("x.tmp", False),
    ("keep.tmp", False),
    ("d/x.tmp", False),
    ("d/keep.tmp", False),
    ("a.pyc", False),
    ("a.pyo", False),
    ("build", True),
    ("build/x.o", False),
    ("sub/build", True),
    ("sub/build/y", False),
    ("rooted.txt", False),
    ("d/rooted.txt", False),
    ("docs/readme.md", False),
    ("docs/sub/readme.md", False),
    ("docs/keep.md", False),
    ("deep.log", False),
    ("x/deep.log", False),
    ("x/y/deep.log", False),
    ("a", True),
    ("a/x", False),
    ("a/x/y", False),
    ("anywhere.cfg", False),
    ("z/anywhere.cfg", False),
    ("temp1", False),
    ("temp", False),
    ("sub/dir/file.txt", False),
    ("dir/file.txt", False),
    ("#hash.txt", False),
    (".gitkeep", False),
    ("q1est/a.txt", False),
    ("quest/b.txt", False),
    ("spaces", False),
    ("plain.py", False),
    ("中文.txt", False),
    ("logs", True),
    ("x/logs", True),
    ("x/logs/y", False),
    ("trace.log", False),
    ("x/trace.log", False),
    ("root.py", False),
    ("other.py", False),
    ("d/other.py", False),
    ("d/root.py", False),
]

CORPUS_TWO_RULES = [
    "foo/*",
    "!foo/keep.txt",
    "bin/",
    "mid/**/end.log",
    "[!a-c].txt",
    "ab[bx]z",
    "*.log",
    "!keep",
    "keep/**",
    "x/?/z",
    "a.b",
    "**/b/",
    "onlyroot/",
    "!onlyroot/keep/",
]

CORPUS_TWO_PATHS = [
    ("foo/x", False),
    ("foo/x", True),
    ("foo/x/y", False),
    ("foo/keep.txt", False),
    ("mid/end.log", False),
    ("mid/a/end.log", False),
    ("mid/a/b/end.log", False),
    ("d.txt", False),
    ("a.txt", False),
    ("c.txt", False),
    ("e.txt", False),
    ("abaz", False),
    ("abbz", False),
    ("abxz", False),
    ("abcz", False),
    ("x.log", False),
    ("keep", False),
    ("d/keep", False),
    ("keep/a", False),
    ("keep/a/b", False),
    ("x/1/z", False),
    ("x/1/2/z", False),
    ("a.b", False),
    ("b", True),
    ("q/b", True),
    ("q/b/x", False),
    ("onlyroot", True),
    ("onlyroot/keep", True),
    ("onlyroot/keep/x", False),
    ("onlyroot/x", False),
    ("d/onlyroot", True),
    ("onlyroot", False),
    ("bin.py", False),
    ("binary.py", False),
    ("bin", False),
    ("bin", True),
    ("b", False),
]


def reference(rules, path, is_dir):
    return GitIgnoreSpec.from_lines(rules).check_file(path + ("/" if is_dir else "")).include


STAR_SUFFIX_CASES = [
    (["venv*"], "venv", False),
    (["venv*"], "venv", True),
    (["venv*"], "venv/x", False),
    (["venv*"], "venv2", False),
    (["foo*/"], "foo", True),
    (["foo*/"], "foo/z", False),
    (["foo*/"], "foobar", False),
    (["**/"], "a", True),
    (["**/"], "a/x", True),
    (["**/"], "a/x/y", False),
    (["**/"], "top.md", False),
]


@pytest.mark.parametrize("rules,path,is_dir", STAR_SUFFIX_CASES)
def test_star_suffixes_and_double_star_directory_rules_agree_with_the_reference(rules, path, is_dir):
    """A star with literal neighbors matches the empty tail (`venv*` decides `venv` itself), a
    lone star does not (`dir/*` never decides `dir`), and a rule of only `**/` decides every
    directory and nothing else."""
    reader = GitIgnore.from_lines(rules)
    assert reader.matches(path, is_dir) == reference(rules, path, is_dir), (rules, path, is_dir)


@pytest.mark.parametrize("rules,paths", [(CORPUS_ONE_RULES, CORPUS_ONE_PATHS), (CORPUS_TWO_RULES, CORPUS_TWO_PATHS)])
def test_agrees_with_the_reference_reader(rules, paths):
    """On every pattern shape the walk reads, the built-in reader decides each path as the
    reference does: ignored, un-ignored by a later negation, or untouched."""
    reader = GitIgnore.from_lines(rules)
    for path, is_dir in paths:
        assert reader.matches(path, is_dir) == reference(rules, path, is_dir), (path, is_dir)


def test_a_trailing_double_star_leaves_the_directory_itself_undecided():
    """`a/**` matches everything below `a`, not `a` itself -- the documented behavior, where the
    reference marks the directory ignored. The walk's file set is the same either way: it
    descends into `a` and every child matches."""
    reader = GitIgnore.from_lines(["a/**"])
    assert reader.matches("a", True) is None
    assert reader.matches("a/x", False) is True
    assert reader.matches("a/x/y", False) is True


def test_a_star_rule_leaves_the_directory_itself_undecided():
    """`dir/*` matches only what is inside `dir`, never the directory -- where the reference and
    the old reader marked it ignored, pruning the subtree before `!dir/keep.txt` could reach its
    file. Leaving it undecided matches Git: the walk descends, and the negation decides."""
    reader = GitIgnore.from_lines(["dir/*", "!dir/keep.txt"])
    assert reader.matches("dir", True) is None
    assert reader.matches("dir/other.txt", False) is True
    assert reader.matches("dir/keep.txt", False) is False


def test_a_directory_rule_decides_no_file_of_a_shared_prefix():
    """`bin/` decides the directory and what is under it, never `bin.py` or `binary.py` -- the
    overshoot a prefix match without a boundary produces."""
    reader = GitIgnore.from_lines(["bin/"])
    assert reader.matches("bin", True) is True
    assert reader.matches("bin/x.py", False) is True
    assert reader.matches("sub/bin/x.py", False) is True
    assert reader.matches("bin", False) is None
    assert reader.matches("bin.py", False) is None
    assert reader.matches("binary.py", False) is None


def test_a_negated_directory_rule_decides_no_file_either():
    """`!dir/` un-ignores the directory, never a plain file that happens to be named `dir`."""
    reader = GitIgnore.from_lines(["build/", "!dir/"])
    assert reader.matches("dir", True) is False
    assert reader.matches("dir", False) is None


def test_a_double_star_directory_rule_stops_at_the_directory_itself():
    """`dir/**/` names the directories under `dir`, never `dir` itself and never a file directly
    inside it -- where the reference marks all of them ignored. Git keeps `dir/other.py`, and so
    does the walk: it descends and decides each child."""
    reader = GitIgnore.from_lines(["dir/**/"])
    assert reader.matches("dir", True) is None
    assert reader.matches("dir/sub", True) is True
    assert reader.matches("dir/other.py", False) is None


def test_comments_and_blank_lines_are_not_rules():
    """Nothing in a comment or blank line decides any path."""
    reader = GitIgnore.from_lines(["# not a rule", "", "   "])
    assert reader.rules == ()
    assert reader.matches("anything.txt", False) is None
