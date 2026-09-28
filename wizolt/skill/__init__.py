"""wizolt skills: Markdown instruction packs loaded on demand.

`skillfile` reads one SKILL.md into a `Skill`; `discovery` finds them on disk; `library` is the
session's collection and what the model is told about it.
"""

from wizolt.skill.library import SkillLibrary
from wizolt.skill.skillfile import Skill, SkillFormatError

__all__ = ["Skill", "SkillFormatError", "SkillLibrary"]
