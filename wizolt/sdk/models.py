"""Text-only host model requests, independent of the agent's conversation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from wizolt.sdk import PluginError, Usage

HostCall = Callable[[str, dict], Awaitable[dict]]


@dataclass(frozen=True)
class ModelReply:
    text: str
    model: str
    usage: Usage


class Models:
    """The worker binds transport; credentials and provider clients stay in the host."""

    def __init__(self):
        self.call: HostCall | None = None

    async def complete(self, prompt: str, *, system: str = "", provider: str = "", model: str = "", effort: str = "", api: str = "") -> ModelReply:
        """Make one text request during an explicit action; no tools or implicit history.

        Empty routing fields inherit configured values. This is a paid request to the selected
        provider. Offline trials have no host model access and report an actionable error.
        """
        if self.call is None:
            raise PluginError("Host model access is unavailable")
        value = await self.call("model.complete", {"prompt": prompt, "system": system, "provider": provider, "model": model, "effort": effort, "api": api})
        return ModelReply(value["text"], value["model"], Usage(**value["usage"]))
