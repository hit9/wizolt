"""Named-site presentation: one chosen presenter per site, with builtin fallback.

Presenters do not compose: a site gets exactly one renderer, so overlapping matches are a
user decision, not a race -- the builtin rendering stays until the user picks one. A failed,
slow or unmatched presentation falls back to the builtin rendering without failing anything:
presentation is never worth a turn.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

from wizolt.plugins.preferences import PluginPreferences
from wizolt.plugins.protocol import PresenterSpec, Snapshot, decode_panel
from wizolt.sdk import Panel, PluginError, presentation
from wizolt.sdk.presentation import ActivityStatus, ToolCard, ToolSummary

if TYPE_CHECKING:
    from wizolt.plugins.runtime import Generation, PluginRuntime


@dataclass(frozen=True)
class Link:
    owner: str
    generation: Generation
    spec: PresenterSpec
    site: str

    @property
    def health(self) -> str:
        return f"presenter:{self.site}"


class PresenterChoices:
    """One user-level choice per site, stored as ``[plugin_manager.presenters]``.

    The saved choice names the plugin that owns the site; unlisted sites resolve to a sole
    matching presenter, or stay builtin while two match.
    """

    def __init__(self, preferences: PluginPreferences | None = None):
        self.preferences = preferences
        self.sites: dict[str, str] = {}

    def load(self) -> None:
        if self.preferences is None:
            return
        choice = self.preferences.read("presenters").get("choice", {})
        if not isinstance(choice, dict) or any(
            site not in presentation.SITES or not isinstance(name, str) or not name.isidentifier() for site, name in choice.items()
        ):
            raise PluginError("plugin_manager.presenters.choice must map sites to plugin names")
        self.sites = dict(choice)

    def save(self, sites: dict[str, str]) -> None:
        if self.preferences is not None:
            self.preferences.save("presenters", "choice", sites)
        self.sites = dict(sites)


class Presenters:
    SITE_SECONDS = 0.5  # Tool sites wait briefly; a slow panel is worth less than the turn.

    def __init__(self, runtime: PluginRuntime):
        self.runtime = runtime
        self.choices = PresenterChoices()
        # Sites whose registrations last matched more than one plugin: builtin until the user picks.
        self.conflicts: dict[str, tuple[str, ...]] = {}

    def registered(self, site: str) -> bool:
        """Whether any live generation registers the site; callers use it for the fast path."""
        return any(site in entry.active.plugin.presenters and not entry.disabling for entry in self.runtime.entries.values())

    def chain(self, site: str) -> list[Link]:
        """Live registrations for one site in name order; presenters do not compose."""
        entries = {name: entry for name, entry in self.runtime.entries.items() if site in entry.active.plugin.presenters and not entry.disabling}
        return [Link(name, entries[name].active, entries[name].active.plugin.presenters[site], site) for name in sorted(entries)]

    def pick(self, site: str, view: object) -> Link | None:
        """The one presenter that renders this view, or None for the builtin rendering."""
        matching = [link for link in self.chain(site) if link.spec.matches(view)]
        if not matching:
            return None
        if (chosen := self.choices.sites.get(site)) is not None:
            return next((link for link in matching if link.owner == chosen), None)
        if len(matching) == 1:
            return matching[0]
        self.conflicts[site] = tuple(link.owner for link in matching)
        return None

    async def render(self, site: str, view: ToolCard | ToolSummary | ActivityStatus, *, context=None, timeout: float | None = None) -> Panel | None:
        """Present one view, or None when the builtin rendering applies. Never raises."""
        link = self.pick(site, view)
        if link is None or link.generation.failure(link.health):
            return None
        try:
            raw = await link.generation.worker.request(
                "present", timeout=timeout or self.SITE_SECONDS, site=site, view=presentation.encode(view), context=asdict(context or self.runtime.facts())
            )
            panel = decode_panel(raw)
            Snapshot.check_panel(panel)
            return panel
        except Exception as error:  # noqa: BLE001 - presentation failures fall back to builtin.
            link.generation.fail(link.health, str(error))
            return None
