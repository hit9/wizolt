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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wizolt.session import Session
    from wizolt.skill.library import SkillLibrary


@dataclass
class SkillListing:
    index: str  # the frozen SKILLS block
    tool: bool  # whether the Skill tool is in this session's tool block
    epoch: int  # the Session.context_epoch the index was frozen in
    told: set[str] = field(default_factory=set)  # names the model already knows exist

    @classmethod
    def of(cls, session: Session, library: SkillLibrary) -> SkillListing:
        """The session's listing, frozen on first use and refrozen when its epoch has moved on."""
        listing = session.skill_listing
        if listing is None:
            listing = session.skill_listing = cls("", bool(library.model_visible()), -1)
        if listing.epoch != session.context_epoch:
            listing.index = library.index()
            listing.epoch = session.context_epoch
            # Everything visible now is in the index or its "N more" line; nothing to announce.
            listing.told = {skill.name for skill in library.model_visible()}
        return listing

    def announcement(self, library: SkillLibrary) -> str:
        """A one-time notice of skills the model has not been told about, or "" when there are
        none -- or when this session has no Skill tool to load them with (`/name` still works)."""
        new = [skill for skill in library.model_visible() if skill.name not in self.told]
        if not new or not self.tool:
            return ""
        self.told.update(skill.name for skill in new)
        header = "Now available, in addition to the SKILLS index above; load one with Skill(name):"
        return "\n".join(["--- NEW SKILLS ---", header, "", *(library.row(skill) for skill in new)])
