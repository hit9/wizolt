"""Assemble isolated text requests for plugins using configured provider routes.

Reuse wire adapters, not the active client's request state. The temporary session has no tools,
history, hooks or persistence lease. Its usage is returned to the plugin, never merged into the
agent's context/cache counters. Credentials stay host-side and are not part of RPC payloads.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, replace

from wizolt.config import PROVIDER_API_CHOICES
from wizolt.model import ModelClient
from wizolt.model.interception import logical_request, route_check
from wizolt.sdk import PluginError, Usage
from wizolt.session import Session


class PluginModels:
    def __init__(self, session: Session):
        self.session = session

    async def call(self, operation: str, arguments: dict) -> dict:
        if operation != "model.complete":
            raise PluginError(f"Unknown host service: {operation}")
        names = {"prompt", "system", "provider", "model", "effort", "api"}
        if set(arguments) != names or any(not isinstance(value, str) for value in arguments.values()) or not arguments["prompt"].strip():
            raise PluginError("Model requests require a non-empty prompt and string routing fields")
        config = copy.deepcopy(self.session.config)
        entry = arguments["provider"] or config.active_provider
        if entry not in config.providers:
            raise PluginError(f"Unknown provider: {entry}")
        config.active_provider = entry
        provider = config.providers[entry]
        provider = replace(
            provider,
            model=arguments["model"] or provider.model,
            reasoning=arguments["effort"] or provider.reasoning,
            api=arguments["api"] or provider.api,
            builtin_tools=(),
        )
        if provider.api not in PROVIDER_API_CHOICES:
            raise PluginError(f"Unknown model API: {provider.api}")
        if missing := provider.missing_fields():
            raise PluginError(f"Provider {entry} is missing {', '.join(missing)}")
        config.providers[entry] = provider
        detached = Session(
            cwd=self.session.cwd,
            config=config,
            settings=copy.deepcopy(self.session.settings),
            system_info=self.session.system_info,
            catalog=self.session.catalog,
        )
        client = ModelClient(detached)
        messages = [{"role": "user", "content": arguments["prompt"]}]
        if arguments["system"]:
            messages.insert(0, {"role": "system", "content": arguments["system"]})
        used = {"model": provider.model}

        async def send(route):
            used["model"] = route.model
            return await client.api_request(messages, None, allow_stream=False, provider=route, response_timeout=route.response_timeout)

        try:
            # An auxiliary request: it enters model.request interception in the main session's
            # chain, and the host-owned scope of the calling handler skips that plugin's own
            # registration, so a request interceptor cannot recurse into itself.
            _, calls, text = await logical_request(
                self.session.plugins,
                config,
                purpose="plugin",
                messages=messages,
                tools=None,
                provider=provider,
                entry=entry,
                send=send,
                check_route=route_check(detached, client, messages, None),
                record=self.session.record_operation,
                checkpoint=self.session.save_snapshot,
            )
            if calls:
                raise PluginError("Plugin text request returned tool calls")
            usage = detached.usage
            return {
                "text": text,
                "model": used["model"],
                "usage": asdict(Usage(usage.calls, usage.prompt_tokens, usage.completion_tokens, usage.cached_prompt_tokens)),
            }
        finally:
            await client.close()
