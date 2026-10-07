"""What one session's model has been told about the skills: a record that only grows by appending.

The SKILLS index rides the cache-stable prefix and the Skill tool rides the tool block; rewriting
either mid-conversation would re-price everything after it (design/DESIGN.md, "Context is a projection").
So both are frozen at the session's first request. A skill that appears later -- installed,
trusted, or found in a subdirectory the agent worked in -- is announced once as an appended
message, and the index is rebuilt only where the prefix is rebuilt anyway: after a compaction or
a context reset. The tool stays as it was for the whole session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from wizolt.mentions import scan_mentions

if TYPE_CHECKING:
    from wizolt.session import Session
    from wizolt.skill.library import SkillLibrary


@dataclass
class SkillListing:
    index: str = ""  # the frozen SKILLS block
    tool: bool = False  # whether the Skill tool is in this session's tool block
    epoch: int = -1  # the Session.context_epoch the index was frozen in; -1 until first frozen
    told: set[str] = field(default_factory=set)  # names the model already knows exist
    # A message that names a hundred skills is spam, not a request; the first blocks ride.
    MAX_MENTION_BLOCKS: ClassVar[int] = 50
    # Names the user has written as a mention. A disable-model-invocation skill is closed to the
    # model until its user names it; the mention is the authorization (see resolve_mentions).
    authorized: set[str] = field(default_factory=set)

    @classmethod
    def of(cls, session: Session, library: SkillLibrary) -> SkillListing:
        """The session's listing, frozen on first use and refrozen when its epoch has moved on.

        A mention may create the listing before the turn's rescan has run; that placeholder is
        never frozen (epoch -1), so the first freeze still sees the scanned library, and what
        the mention authorized survives it."""
        listing = session.skill_listing
        if listing is None:
            listing = session.skill_listing = cls()
        if listing.epoch != session.context_epoch:
            if listing.epoch >= 0:
                # The context that held the mentions was rebuilt; what it authorized left with it.
                listing.authorized.clear()
            listing.index = library.index()
            listing.tool = bool(library.model_visible())
            listing.epoch = session.context_epoch
            # Everything visible now is in the index or its "N more" line; nothing to announce.
            listing.told = {skill.name for skill in library.model_visible()}
        return listing

    def announcement(self, library: SkillLibrary) -> str:
        """A one-time notice of skills the model has not been told about, or "" when there are
        none -- or when this session has no Skill tool to load them with (a mention still names one)."""
        new = [skill for skill in library.model_visible() if skill.name not in self.told]
        if not new or not self.tool:
            return ""
        self.told.update(skill.name for skill in new)
        header = "Now available, in addition to the SKILLS index above; load one with Skill(name):"
        return "\n".join(["--- NEW SKILLS ---", header, "", *(library.row(skill) for skill in new)])

    def resolve_mentions(self, library: SkillLibrary, text: str) -> str:
        """The SKILL MENTIONS block for one user message. Naming a skill also opens it: a
        disable-model-invocation skill is loadable from the moment its user names it."""
        seen: set[str] = set()
        blocks: list[str] = []
        for span in scan_mentions(text):
            if span.kind != "skill" or not span.complete or not span.payload:
                continue
            raw = span.payload
            skill = library.get(raw)
            if skill is None or skill.name in seen:
                continue
            seen.add(skill.name)
            blocks.append(library.row(skill))
            if len(blocks) >= self.MAX_MENTION_BLOCKS:
                break
        if not blocks:
            return ""
        self.authorized.update(seen)
        header = [
            "--- SKILL MENTIONS ---",
            "The user referenced these skills. Load the one the request needs with Skill(name); the instructions are not inlined.",
            "",
        ]
        return "\n".join([*header, *blocks])
