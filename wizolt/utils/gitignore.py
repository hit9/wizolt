"""Nested .gitignore matching, for the walk that has no Git and no ripgrep.

The third-priority @-mention source (wizolt/mentions.py) walks the workspace itself when there
is no `git ls-files` and no `rg --files`, and honors the .gitignore files it meets on the way
down. That needs one narrow slice of Git's pattern language: blank lines and # comments, !
negation, trailing-slash directory rules, root anchoring by a leading or inner slash, * and ?
within a segment, ** across segments, and last-match-wins. Each line becomes one compiled
regular expression; a rule that matches nothing is skipped by the walk, exactly as before.

What is deliberately absent: POSIX character classes, bytes patterns, and every other
pathspec feature the walk cannot reach. The reference implementation stays in the dev extras,
and the tests check this reader agrees with it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable


class GitIgnore:
    """One directory's .gitignore, checked against paths relative to that directory."""

    def __init__(self, rules: tuple[tuple[re.Pattern[str], bool], ...]):
        self.rules = rules  # each rule: (compiled regex, True to ignore what matches, False to un-ignore)

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> GitIgnore:
        rules: list[tuple[re.Pattern[str], bool]] = []
        for line in lines:
            rule = cls._compile(line.rstrip("\n").rstrip(" \t"))
            if rule is not None:
                rules.append(rule)
        return cls(tuple(rules))

    def matches(self, rel: str, is_dir: bool) -> bool | None:
        """True/False when a rule decides the path, None when no rule mentions it.

        Later rules win over earlier ones, a directory rule also decides everything under the
        directory, and negation (!) un-ignores what an earlier rule ignored."""
        path = rel + ("/" if is_dir else "")
        ignored: bool | None = None
        for regex, ignores in self.rules:
            if regex.match(path):
                ignored = ignores
        return ignored

    @staticmethod
    def _compile(line: str) -> tuple[re.Pattern[str], bool] | None:
        if not line or line.startswith("#"):
            return None
        ignores = not line.startswith("!")
        if not ignores:
            line = line[1:]
        if line[:1] == "\\" and line[1:2] in ("#", "!"):
            line = line[1:]
        dir_only = line.endswith("/")
        line = line.rstrip("/")
        if not line:
            return None
        segments = line.split("/")
        # A leading slash anchors to the root; so does any inner slash ("dir/file" names a path
        # from the root). Only a lone segment matches at any depth.
        rooted = not segments[0] or len(segments) > 1
        segments = [segment for segment in segments if segment]
        if not segments:
            return None
        # ** emits its own trailing slash; a plain segment needs one only after another plain
        # segment (never after **), so "**/deep.log" does not require a doubled slash.
        body = ""
        after_segment = False
        for segment in segments:
            if segment == "**":
                body += ("/" if after_segment else "") + "(?:[^/]+/)*"
                after_segment = False
            else:
                body += ("/" if after_segment else "") + GitIgnore._segment(segment)
                after_segment = True
        if not dir_only:
            if segments[-1] == "**":
                # Everything below the prefix, at least one segment deep; the directory itself
                # stays matchable only as the prefix of what is under it.
                body += "[^/]+"
            else:
                # A file rule also matches a directory of that name, so the walk prunes the subtree.
                body += "(?:/|$)"
        else:
            # A directory rule matches the directory itself -- and so everything under it, since
            # the walk never descends into an ignored directory -- but nothing else: matches()
            # already appends the slash of a directory, so requiring one here keeps "bin/" from
            # swallowing "bin.py", "binary.py", or a plain file that happens to be named "bin".
            if segments[-1] == "**":
                # `dir/**/` names the directories under dir, at least one segment deep: never dir
                # itself, never a file sitting directly inside it.
                body += "[^/]+/"
            else:
                body += "/"
        prefix = "" if rooted or segments[0] == "**" else "(?:[^/]+/)*"
        return re.compile("^" + prefix + body), ignores

    @staticmethod
    def _segment(segment: str) -> str:
        """One path segment: * is anything but a slash, ? is one such character, [...] is a
        character range, and a backslash escapes any of them."""
        parts: list[str] = []
        index = 0
        while index < len(segment):
            char = segment[index]
            if char == "\\" and index + 1 < len(segment):
                parts.append(re.escape(segment[index + 1]))
                index += 2
                continue
            if char == "*":
                # A lone * names what is inside the directory ("dir/*"), and an empty match there
                # would also decide the directory itself, pruning the subtree before a later
                # negation could reach its files. A * with literal neighbors ("venv*") matches
                # the empty tail, as in Git: it still decides the bare literal.
                parts.append("[^/]+" if segment == "*" else "[^/]*")
            elif char == "?":
                parts.append("[^/]")
            elif char == "[":
                end = segment.find("]", index + 2)  # a leading ] closes nothing
                if end < 0:
                    parts.append(re.escape("["))
                else:
                    body = segment[index + 1 : end].replace("\\", "\\\\")
                    parts.append("[^" + body[1:] + "]" if body.startswith("!") else "[" + body + "]")
                index = end + 1 if end >= 0 else index + 1
                continue
            else:
                parts.append(re.escape(char))
            index += 1
        return "".join(parts)
