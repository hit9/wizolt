"""wizolt configuration: provider entries, runtime settings, and the config file."""

from __future__ import annotations

import contextlib
import fcntl
import fnmatch
import hashlib
import os
import platform
import re
import shutil
import stat
import sys
import tomllib
from collections.abc import MutableMapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, ClassVar

from wizolt.base import MAX_TOOL_OUTPUT_TOKENS, ConfigError, Json, builtin_function_names
from wizolt.providers.compat import bundled_policy
from wizolt.utils.workspace import Workspace

if TYPE_CHECKING:
    from tomlkit import TOMLDocument

    from wizolt.agentsmd import AgentsFile
    from wizolt.providers.compat import ProviderPolicy

DEFAULT_MAX_CONTEXT_TOKENS = 256 * 1024
MAX_SUBAGENTS = 32
MIN_BASH_OUTPUT_TOKENS = 1_000  # Below this, error tails start to lose the line that matters.
PROVIDER_API_CHOICES = ("auto", "chat", "responses", "anthropic")
REASONING_HISTORY_CHOICES = ("auto", "all", "current_turn", "tool_calls")


class UserPaths:
    """Own the default and legacy locations for user state.

    Legacy directories are read in place, never copied or moved. Keeping the precedence here
    prevents config loading and early catalog loading from selecting different state roots.
    """

    DEFAULT_DATA_DIR = "~/.wizolt"
    LEGACY_DATA_DIRS = ("~/.minacode", "~/.nanocode")

    @classmethod
    def resolve_data_dir(cls, value: str) -> str:
        if value != cls.DEFAULT_DATA_DIR:
            return value
        return cls._first_existing(cls.DEFAULT_DATA_DIR, cls.LEGACY_DATA_DIRS)

    @classmethod
    def resolve_config_path(cls, path: str | None) -> str:
        if path:
            return os.path.expanduser(path)
        candidates = tuple(os.path.join(directory, "config.toml") for directory in cls.LEGACY_DATA_DIRS)
        selected = cls._first_existing(os.path.join(cls.DEFAULT_DATA_DIR, "config.toml"), candidates)
        return os.path.expanduser(selected)

    @staticmethod
    def _first_existing(default: str, legacy: tuple[str, ...]) -> str:
        if os.path.exists(os.path.expanduser(default)):
            return default
        return next((path for path in legacy if os.path.exists(os.path.expanduser(path))), default)


# Output room kept out of the input budget for one request's answer. It is a planning reserve, not a
# wire parameter, so it stays fixed whether or not the user configured a cap (see output_token_budget).
DEFAULT_OUTPUT_RESERVE_TOKENS = 16_384
# Zero means unconfigured. Each wire's catalog policy decides whether it needs a concrete default.
DEFAULT_MAX_TOKENS = 0
MIN_CONTEXT_SAFETY_TOKENS = 4_096
HTTP_HEADER_NAME = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+\Z")


def request_budget_for(max_context_tokens: int, output_budget: int) -> int:
    """The input budget one request is measured against: the context limit less the output reserve and
    a safety margin. Pure, so ContextManager and the usage recorder share the same denominator."""
    safety = max(MIN_CONTEXT_SAFETY_TOKENS, (max_context_tokens + 49) // 50)
    return max(1, max_context_tokens - output_budget - safety)


@dataclass
class SystemInfo:
    # fmt: off
    COMMANDS: ClassVar[tuple[str, ...]] = (
        "bash", "git", "rg", "sed", "grep", "find", "awk", "python3", "jq", "xargs", "cat", "head", "tail", "wc",
        "sort", "uniq", "make", "cmake", "gcc", "g++", "clang", "clang++", "node", "npm", "uv", "pytest",
    )
    # fmt: on

    cwd: str
    os: str
    arch: str
    commands: tuple[str, ...]
    # The project's instruction files as the session started, repository root first (see
    # agentsmd.AgentsFile.project_files); empty when there are none.
    agents_md_project: tuple[AgentsFile, ...] = ()
    agents_md_global: str = ""  # loaded <data_dir>/AGENTS.md text; "" when the file does not exist
    agents_md_global_display: str = ""  # what the prefix and menus show, e.g. "~/.wizolt/AGENTS.md"
    agents_md_global_path: str = ""  # exact wizolt-owned path, even when the file is absent

    @classmethod
    def detect(cls, cwd: str, data_dir: str = "") -> SystemInfo:
        from wizolt.agentsmd import AgentsFile, display_path, global_agents_md_path  # local import: agentsmd sits above config

        agents_md_global = ""
        agents_md_global_display = ""
        agents_md_global_path = global_agents_md_path(data_dir) if data_dir else ""
        if agents_md_global_path:
            try:
                with open(agents_md_global_path, encoding="utf-8") as file:
                    agents_md_global = file.read()
                agents_md_global_display = display_path(agents_md_global_path)
            except (OSError, UnicodeDecodeError):
                pass
        return cls(
            cwd=cwd,
            os=platform.system() or sys.platform,
            arch=platform.machine() or "unknown",
            commands=tuple(name for name in cls.COMMANDS if shutil.which(name)),
            agents_md_project=AgentsFile.project_files(Workspace(cwd)),
            agents_md_global=agents_md_global,
            agents_md_global_display=agents_md_global_display,
            agents_md_global_path=agents_md_global_path,
        )


@dataclass(frozen=True)
class ModelOverride:
    """One `[provider.X.models]` entry: a model glob and what it declares about those models.

    Effort support is a property of the model, not of the endpoint, so it cannot be settled by a
    field on the entry -- `/model` switches models under one entry. A glob keeps the declaration
    where the fact lives, and carries to the next model of the same family."""

    match: str
    reasoning_levels: tuple[str, ...] = ()

    def matches(self, model: str) -> bool:
        return fnmatch.fnmatchcase(model.lower(), self.match)


@dataclass
class ProviderConfig:
    url: str = ""
    key: str = ""
    model: str = ""
    api: str = "auto"
    stream: bool = True
    prompt_cache_key: str = "auto"
    available_models: tuple[str, ...] = ()
    temperature: float | None = None
    max_tokens: int = DEFAULT_MAX_TOKENS
    # How much of *this* entry's model window to use; 0 inherits runtime.max_context_tokens. Entries
    # are effectively per-model, so a 1M-window model and a 128K one no longer have to share one
    # number -- see context_token_limit.
    max_context_tokens: int = 0
    strict_tools: bool = False
    reasoning: str = "medium"
    chat_reasoning: str = "auto"
    # Replay policy is independent of the request's reasoning spelling. ``auto`` selects the
    # catalog policy; an explicit value is the generic escape hatch for an unknown gateway.
    reasoning_history: str = "auto"
    timeout: int = 120
    response_timeout: int = 600
    extra_body: Json = field(default_factory=dict)
    # Extra HTTP headers sent with every request to this entry. `extra_body` reaches the request
    # body only, so transport metadata needs a separate channel. Merged over wizolt's own
    # defaults; the SDK still derives authentication from `key`.
    headers: dict[str, str] = field(default_factory=dict)
    # `[provider.X.models]` declarations in declaration order; the first matching glob wins, the
    # way catalog rules resolve. A declaration overrides the catalog for those models.
    model_overrides: tuple[ModelOverride, ...] = ()
    # Request-body fields this endpoint must never receive. Removal is applied at the wire boundary;
    # this value stays plain configuration rather than learning request-body behavior.
    omit_body: tuple[str, ...] = ()
    builtin_tools: tuple[Json, ...] = ()
    # Per-provider compaction overrides ([provider.X.compaction] model/reasoning/api), folded on
    # top of the global [compaction] section by compaction_provider_config: the per-provider value
    # wins, empty inherits the global value, and a fully empty pair leaves the entry's own value.
    compaction_model: str = ""
    compaction_reasoning: str = ""
    compaction_api: str = ""

    @classmethod
    def from_dict(cls, data: Json, *, policy: ProviderPolicy | None = None) -> ProviderConfig:
        policy = policy or bundled_policy()
        chat_reasoning_choices = ("auto", *policy.reasoning_dialects)
        api = Config.str(data, "api", "auto")
        prompt_cache_key = cls.clean_prompt_cache_key(Config.str(data, "prompt_cache_key", "auto"))
        reasoning = Config.str(data, "reasoning", "medium")
        chat_reasoning = Config.str(data, "chat_reasoning", "auto")
        reasoning_history = Config.str(data, "reasoning_history", "auto")
        model_overrides = cls.model_overrides_from(data)
        declared = {level for override in model_overrides for level in override.reasoning_levels}
        for key, value, choices in (
            ("api", api, PROVIDER_API_CHOICES),
            ("chat_reasoning", chat_reasoning, chat_reasoning_choices),
            ("reasoning_history", reasoning_history, REASONING_HISTORY_CHOICES),
        ):
            if value not in choices:
                raise ConfigError("provider." + key + " must be one of " + ", ".join(choices))
        compaction_root = Config.table(data, "compaction")
        compaction_reasoning = Config.str(compaction_root, "reasoning", "")
        compaction_api = Config.str(compaction_root, "api", "")
        if compaction_api and compaction_api not in PROVIDER_API_CHOICES:
            raise ConfigError("provider.compaction.api must be one of " + ", ".join(PROVIDER_API_CHOICES))
        provider = cls(
            url=Config.str(data, "url"),
            key=Config.str(data, "key"),
            model=Config.str(data, "model"),
            api=api,
            stream=Config.bool(data, "stream", True),
            prompt_cache_key=prompt_cache_key,
            available_models=Config.str_tuple(data, "available_models"),
            temperature=Config.float(data, "temperature", None),
            max_tokens=max(0, Config.int(data, "max_tokens", DEFAULT_MAX_TOKENS)),
            max_context_tokens=max(0, Config.int(data, "max_context_tokens", 0)),
            strict_tools=Config.bool(data, "strict_tools", False),
            reasoning=reasoning,
            chat_reasoning=chat_reasoning,
            reasoning_history=reasoning_history,
            timeout=Config.int(data, "timeout", 120),
            response_timeout=max(0, Config.int(data, "response_timeout", 600)),
            extra_body=Config.table(data, "extra_body"),
            headers=Config.header_table(data, "headers"),
            model_overrides=model_overrides,
            omit_body=cls.clean_omit_body(Config.str_tuple(data, "omit_body")),
            builtin_tools=Config.table_tuple(data, "builtin_tools"),
            compaction_model=Config.str(compaction_root, "model", ""),
            compaction_reasoning=compaction_reasoning,
            compaction_api=compaction_api,
        )
        choices = policy.reasoning_values(provider)
        if reasoning not in choices:
            raise ConfigError("provider.reasoning must be one of " + ", ".join(choices))
        # The global [compaction].model is not visible while an entry is parsed, so its nested
        # override accepts any name this entry declares; Config.from_dict validates the effective
        # provider/model pair once both layers are known.
        compaction_choices = tuple(dict.fromkeys(("off", *policy.effort_order, *sorted(declared))))
        if compaction_reasoning and compaction_reasoning not in compaction_choices:
            raise ConfigError("provider.compaction.reasoning must be one of " + ", ".join(compaction_choices))
        return provider

    @staticmethod
    def model_overrides_from(data: Json) -> tuple[ModelOverride, ...]:
        """Parse `[provider.X.models]`: a model glob mapped to what it declares.

        `reasoning` is an ordered list, weakest first — the order is what gives a level wizolt
        does not recognize its place on the scale, so an unordered set would not do."""
        raw_models = data.get("models")
        if raw_models is not None and not isinstance(raw_models, dict):
            raise ConfigError("provider.models must be a table")
        overrides: list[ModelOverride] = []
        for pattern, declared in (raw_models or {}).items():
            if not isinstance(declared, dict):
                raise ConfigError(f"provider.models.{pattern} must be a table")
            levels = tuple(level.strip() for level in Config.str_tuple(declared, "reasoning"))
            if not levels or any(not level for level in levels):
                raise ConfigError(f"provider.models.{pattern}.reasoning must contain at least one level")
            if "off" in levels or len(set(levels)) != len(levels):
                raise ConfigError(f"provider.models.{pattern}.reasoning must contain unique effort levels, without off")
            match = pattern.strip().lower()
            if not match:
                raise ConfigError("provider.models patterns must not be empty")
            overrides.append(ModelOverride(match=match, reasoning_levels=levels))
        return tuple(overrides)

    # These fields either carry the request or select the local response parser. Dropping `stream`
    # while still entering the streaming branch can turn a valid non-streaming response into an
    # empty assistant message, so changing that behavior belongs to provider.stream instead.
    OMIT_BODY_PROTECTED: ClassVar[tuple[str, ...]] = ("model", "messages", "input", "stream")

    @classmethod
    def clean_omit_body(cls, names: tuple[str, ...]) -> tuple[str, ...]:
        for name in names:
            if name in cls.OMIT_BODY_PROTECTED:
                raise ConfigError("provider.omit_body cannot drop " + ", ".join(cls.OMIT_BODY_PROTECTED))
        return names

    def declared_levels(self, model: str = "") -> tuple[str, ...]:
        """The effort scale declared for this model, or none when no glob matches it."""
        model = model or self.model
        return next((override.reasoning_levels for override in self.model_overrides if override.matches(model)), ())

    def missing_fields(self) -> list[str]:
        """The required fields this entry leaves empty. An entry missing any of them cannot serve a
        request, whichever role it is filling — the active provider, or the one `[compaction]`
        points at."""
        return [name for name in ("url", "key", "model") if not getattr(self, name)]

    def builtin_function_names(self) -> tuple[str, ...]:
        """Declared provider functions that use the client-echo handshake."""
        return builtin_function_names(self.builtin_tools)

    def output_token_budget(self) -> int:
        return self.max_tokens or DEFAULT_OUTPUT_RESERVE_TOKENS

    def context_token_limit(self, fallback: int) -> int:
        """How much context this entry may use: its own `max_context_tokens`, else the runtime
        default. Resolved per call, never cached, so `/provider` moves the budget with the entry —
        and so a worker on a small model stops borrowing the parent model's window."""
        return self.max_context_tokens or fallback

    @staticmethod
    def clean_prompt_cache_key(value: str) -> str:
        value = value.strip()
        if not value:
            return "auto"
        lower = value.lower()
        if lower in {"auto", "off"}:
            return lower
        if len(value) > 64 or any(char.isspace() for char in value):
            raise ConfigError("provider.prompt_cache_key must be auto, off, or a stable key up to 64 chars without whitespace")
        return value


@dataclass
class RuntimeSettings:
    shell_timeout: int = 60
    # Bash foreground wait budget: if the command hasn't exited within this many seconds the running
    # process is promoted to a background job (see BashTool.stream_process) and control returns to
    # the model with a partial-output payload. Set to 0 to disable promotion (fall back to killing
    # on shell_timeout).
    bash_wait_timeout: int = 10
    max_steps: int = 400
    max_context_tokens: int = DEFAULT_MAX_CONTEXT_TOKENS
    session_retention_days: int = 7
    # Max read-only tool calls from one model batch to execute concurrently; 1 disables parallelism.
    max_parallel_tools: int = 4
    max_subagents: int = 3
    # Tokens of a Bash result the model receives inline. Every later request re-sends it, so a
    # lower cap saves tokens on each of them; the full output stays in a file the cut points to.
    # Applies to new results only, so already-sent requests and the prompt cache never change.
    bash_output_tokens: int = MAX_TOOL_OUTPUT_TOKENS
    yolo: bool = False
    theme: str = "auto"
    language: str = "auto"  # forced reply language; "auto" injects nothing (see /language)
    attribution: bool = True  # ask the model to sign the commits and PR bodies it writes
    agents_md: bool = True  # inject global and project instructions into every request

    @classmethod
    def from_dict(cls, data: Json, *, yolo: bool = False, theme: str = "") -> RuntimeSettings:
        runtime = Config.table(data, "runtime")
        return cls(
            shell_timeout=Config.int(runtime, "shell_timeout", 60),
            bash_wait_timeout=max(0, Config.int(runtime, "bash_wait_timeout", 10)),
            max_steps=max(1, Config.int(runtime, "max_agent_steps", 400)),
            max_context_tokens=max(1, Config.int(runtime, "max_context_tokens", DEFAULT_MAX_CONTEXT_TOKENS)),
            max_parallel_tools=max(1, Config.int(runtime, "max_parallel_tools", 4)),
            max_subagents=cls.clean_max_subagents(Config.int(runtime, "max_subagents", 3)),
            bash_output_tokens=cls.clean_bash_output_tokens(Config.int(runtime, "bash_output_tokens", MAX_TOOL_OUTPUT_TOKENS)),
            session_retention_days=max(0, Config.int(runtime, "session_retention_days", 7)),
            yolo=yolo or Config.bool(runtime, "yolo", False),
            theme=theme or Config.str(runtime, "theme", "auto"),
            language=RuntimeSettings.clean_language(Config.str(runtime, "language", "auto")),
            attribution=Config.bool(runtime, "attribution", True),
            agents_md=Config.bool(runtime, "agents_md", True),
        )

    @staticmethod
    def clean_max_subagents(value: int) -> int:
        if isinstance(value, bool) or not 0 <= value <= MAX_SUBAGENTS:
            raise ConfigError(f"max_subagents must be between 0 and {MAX_SUBAGENTS}")
        return value

    @staticmethod
    def clean_bash_output_tokens(value: int) -> int:
        if isinstance(value, bool) or not MIN_BASH_OUTPUT_TOKENS <= value <= MAX_TOOL_OUTPUT_TOKENS:
            raise ConfigError(f"bash_output_tokens must be between {MIN_BASH_OUTPUT_TOKENS} and {MAX_TOOL_OUTPUT_TOKENS}")
        return value

    @staticmethod
    def clean_language(value: str) -> str:
        value = value.strip()
        if not value or value.lower() == "auto":
            return "auto"
        if len(value) > 64 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ConfigError("runtime.language must be a single-line language name up to 64 chars, or auto")
        return value


@dataclass(frozen=True)
class SubagentDefaults:
    """Creation defaults only; live/resumed children keep their own approved settings."""

    provider: str = ""
    model: str = ""
    reasoning: str = ""
    api: str = ""


@dataclass
class Config:
    active_provider: str = "default"
    providers: dict[str, ProviderConfig] = field(default_factory=lambda: {"default": ProviderConfig()})
    subagent: SubagentDefaults = field(default_factory=SubagentDefaults)
    data_dir: str = UserPaths.DEFAULT_DATA_DIR
    mcp: Json = field(default_factory=dict)
    # The raw `[hooks]` table; wizolt.shellhooks validates it when a session is assembled.
    hooks: Json = field(default_factory=dict)
    # Plugin-owned tables are validated by each candidate in its isolated worker.
    plugins: Json = field(default_factory=dict)
    # UI owns parsing and validation; config only carries the raw tables.
    ui: Json = field(default_factory=dict)
    # The provider entry compaction summaries run on: compaction_provider names
    # a base provider entry (empty = the active provider), and compaction_model/reasoning/api
    # override that entry per field (empty = inherit the entry's value). Resolved per call by
    # compaction_provider_config, never cached, so a runtime /provider switch is picked up by the
    # next compaction.
    compaction_provider: str = ""
    compaction_model: str = ""
    compaction_reasoning: str = ""
    compaction_api: str = ""

    # The provider entry used by explicit ViewImage calls. It only perceives an image and question;
    # attachments still go directly to the active provider and never route here implicitly.
    vision_provider: str = ""

    # The config file this was read from, where `/theme` saves its pick; empty when not from a file.
    path: str = ""

    def __post_init__(self) -> None:
        self.data_dir = UserPaths.resolve_data_dir(self.data_dir)

    @property
    def provider(self) -> ProviderConfig:
        return self.providers[self.active_provider]

    def for_subagent(self, *, policy: ProviderPolicy | None = None) -> Config:
        """Resolve defaults into a detached approval draft, after the parent's live switches.

        The approved draft is copied verbatim at spawn. Reapplying defaults there would undo
        user choices; applying them on restore would overwrite the child's saved choices.
        """
        config = deepcopy(self)
        defaults = self.subagent
        if defaults.provider:
            if defaults.provider not in config.providers:
                raise ConfigError(f"subagent.provider `{defaults.provider}` does not exist")
            config.active_provider = defaults.provider
        for key in ("model", "reasoning", "api"):
            if value := getattr(defaults, key):
                setattr(config.provider, key, value)
        if defaults.api and defaults.api not in PROVIDER_API_CHOICES:
            raise ConfigError("subagent.api must be one of " + ", ".join(PROVIDER_API_CHOICES))
        choices = (policy or bundled_policy()).reasoning_values(config.provider, config.provider.model)
        if defaults.reasoning and defaults.reasoning not in choices:
            raise ConfigError("subagent.reasoning must be one of " + ", ".join(choices))
        return config

    @classmethod
    def from_dict(cls, data: Json, *, policy: ProviderPolicy | None = None, path: str = "") -> Config:
        policy = policy or bundled_policy()
        provider_root = cls.table(data, "provider")
        active = cls.str(provider_root, "active", "default")
        providers = {
            name: ProviderConfig.from_dict(value, policy=policy) for name, value in provider_root.items() if name != "active" and isinstance(value, dict)
        }
        if not providers:
            providers = {active: ProviderConfig.from_dict(provider_root, policy=policy)}
        if active not in providers:
            raise ConfigError(f"provider.active `{active}` does not exist")
        paths = cls.table(data, "paths")
        compaction_root = cls.table(data, "compaction")
        compaction_provider = cls.str(compaction_root, "provider", "")
        if compaction_provider and compaction_provider not in providers:
            raise ConfigError(f"compaction.provider `{compaction_provider}` does not exist")
        compaction_model = cls.str(compaction_root, "model", "")
        compaction_reasoning = cls.str(compaction_root, "reasoning", "")
        compaction_entry = providers[compaction_provider or active]
        effective_compaction_model = compaction_entry.compaction_model or compaction_model or compaction_entry.model
        compaction_choices = policy.reasoning_values(compaction_entry, effective_compaction_model)
        effective_compaction_reasoning = compaction_entry.compaction_reasoning or compaction_reasoning
        if effective_compaction_reasoning and effective_compaction_reasoning not in compaction_choices:
            raise ConfigError("compaction.reasoning must be one of " + ", ".join(compaction_choices))
        compaction_api = cls.str(compaction_root, "api", "")
        if compaction_api and compaction_api not in PROVIDER_API_CHOICES:
            raise ConfigError("compaction.api must be one of " + ", ".join(PROVIDER_API_CHOICES))
        vision_root = cls.table(data, "vision")
        vision_provider = cls.str(vision_root, "provider", "")
        if vision_provider and vision_provider not in providers:
            raise ConfigError(f"vision.provider `{vision_provider}` does not exist")
        # Legacy spelling is normalized only here. New keys, including explicit empty values,
        # win per field; no worker-era behavior escapes into the engine or approval UI.
        subagent_root = {**cls.table(data, "worker"), **cls.table(data, "subagent")}
        subagent = SubagentDefaults(**{key: cls.str(subagent_root, key, "") for key in ("provider", "model", "reasoning", "api")})
        config = cls(
            active_provider=active,
            providers=providers,
            subagent=subagent,
            data_dir=cls.str(paths, "data_dir", UserPaths.DEFAULT_DATA_DIR),
            mcp=cls.table(data, "mcp"),
            hooks=cls.table(data, "hooks"),
            plugins=cls.table(data, "plugins"),
            ui=cls.table(data, "ui"),
            compaction_provider=compaction_provider,
            compaction_model=compaction_model,
            compaction_reasoning=compaction_reasoning,
            compaction_api=compaction_api,
            vision_provider=vision_provider,
            path=path,
        )
        config.for_subagent(policy=policy)  # reject invalid creation defaults at config load
        return config

    @classmethod
    def data_dir_from(cls, data: Json) -> str:
        """Resolve the catalog/session directory before provider config is parsed."""

        return UserPaths.resolve_data_dir(cls.str(cls.table(data, "paths"), "data_dir", UserPaths.DEFAULT_DATA_DIR))

    @staticmethod
    def table(data: Json, key: str) -> Json:
        return value if isinstance((value := data.get(key)), dict) else {}

    @staticmethod
    def table_tuple(data: Json, key: str) -> tuple[Json, ...]:
        """A list of tables passed through verbatim, checked only for the shape every host shares.

        Entries reach the wire unmodified, so validating their contents would mean tracking each
        host's tool catalog. `type` is the one field every documented builtin tool carries, and
        requiring it turns a typo into a config error instead of a provider 400."""
        value = data.get(key)
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"config value `{key}` must be a list of tables")
        entries: list[Json] = []
        for item in value:
            if not isinstance(item, dict):
                raise ConfigError(f"config value `{key}` must be a list of tables")
            if not (isinstance(item.get("type"), str) and item["type"]):
                raise ConfigError(f"config value `{key}` entries must each set a non-empty `type`")
            entries.append(dict(item))
        return tuple(entries)

    @staticmethod
    def header_table(data: Json, key: str) -> dict[str, str]:
        """HTTP headers as a flat name/value table, checked for what a transport can actually send.

        Integers are accepted and stringified because providers document flag headers as bare `1`
        (`x-cmd-zdr: 1`), and a TOML author writes that unquoted. Booleans are not: `true` has no
        agreed-upon wire spelling. Control characters are rejected here rather than reaching httpx
        as an opaque encoding error at request time."""
        raw = data.get(key)
        if raw is None:
            return {}
        if not isinstance(raw, dict):
            raise ConfigError(f"config value `{key}` must be a table")
        headers: dict[str, str] = {}
        for name, value in raw.items():
            if not isinstance(name, str):
                raise ConfigError(f"config value `{key}` contains a non-string header name")
            if isinstance(value, bool) or not isinstance(value, (str, int)):
                raise ConfigError(f"config value `{key}.{name}` must be a string or integer")
            text = str(value)
            normalized = name.lower()
            if HTTP_HEADER_NAME.fullmatch(name) is None or not text.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in text):
                raise ConfigError(f"config value `{key}.{name}` must be an ASCII HTTP header name and single-line value")
            if normalized in headers:
                raise ConfigError(f"config value `{key}` contains the duplicate header `{name}`")
            headers[normalized] = text
        return headers

    @staticmethod
    def str(data: Json, key: str, default: str = "") -> str:
        return default if (value := data.get(key)) is None else str(value)

    @staticmethod
    def str_tuple(data: Json, key: str) -> tuple[str, ...]:
        value = data.get(key)
        if value is None:
            return ()
        if isinstance(value, str):
            return tuple(item.strip() for item in value.split(",") if item.strip())
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            return tuple(value)
        raise ConfigError(f"config value `{key}` must be a string list")

    @staticmethod
    def bool(data: Json, key: str, default: bool = False) -> bool:
        value = data.get(key)
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        lower = value.lower() if isinstance(value, str) else ""
        if lower in {"on", "true", "yes", "1", "off", "false", "no", "0"}:
            return lower in {"on", "true", "yes", "1"}
        raise ConfigError(f"config value `{key}` must be boolean")

    @staticmethod
    def int(data: Json, key: str, default: int) -> int:
        value = data.get(key)
        if value is None:
            return default
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"config value `{key}` must be integer")
        return value

    @staticmethod
    def float(data: Json, key: str, default: float | None) -> float | None:
        value = data.get(key)
        if value is None:
            return default
        if value is False or (isinstance(value, str) and value.lower() == "off"):
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"config value `{key}` must be number or off")
        return float(value)


@dataclass(frozen=True)
class InlineKeys:
    """Keys written in the config itself, and whether the user already chose to keep exactly these.

    `record` holds SHA-256 fingerprints of the declined (entry, key) pairs, never a key, so a new or
    changed key is asked about again while a kept one only draws a reminder.
    """

    config_path: str
    keys: dict[str, str]
    record: str

    @property
    def declined(self) -> bool:
        try:
            with open(self.record, encoding="utf-8") as file:
                kept = set(file.read().split())
        except FileNotFoundError:
            kept = set()
        return self.fingerprints() <= kept

    def decline(self) -> None:
        """Remember these keys as kept; replacing the record lets moved or changed keys drop out."""
        os.makedirs(os.path.dirname(self.record), exist_ok=True)
        ConfigFile.write_atomic(self.record, "".join(line + "\n" for line in sorted(self.fingerprints())))

    def fingerprints(self) -> set[str]:
        return {hashlib.sha256(f"{name}\0{key}".encode()).hexdigest() for name, key in self.keys.items()}


class ConfigFile:
    # Only the provider block is required; every other key falls back to its built-in default, so the
    # commented lines below just document the common knobs and their defaults.
    DEFAULT_TEXT: ClassVar[str] = """# wizolt configuration — unset keys use built-in defaults.

[provider]
active = "default"

[provider.default]
url = ""
model = ""
# key: put it in secrets.toml beside this file (default = "sk-..."), so this file can be shared
# api = "auto"                 # auto | chat | responses | anthropic
# stream = true
# reasoning = "medium"
# reasoning_history = "auto"  # auto | all | current_turn | tool_calls
# max_context_tokens = 0       # how much of THIS model's window to use; 0 inherits runtime.max_context_tokens.
                               # Set it per entry when models differ: 1048576 for a 1M-window model,
                               # 131072 for a 128K one. Fewer compactions on the big one, no overflow
                               # on the small one.
# max_tokens = 0               # output cap per request, reasoning included; 0 uses the wire's catalog policy.
                               # 16K is still reserved from the
                               # input budget, trading against runtime.max_context_tokens one for one
# timeout = 120                # transport inactivity
# response_timeout = 600       # total generation time; 0 disables
# available_models = ["example-model", "example-model-mini"]
# headers = { x-routing-mode = "private" }  # extra HTTP headers; the key still sets auth
# omit_body = ["reasoning_effort"]   # request fields this endpoint rejects; extra_body is the
                                     # other half, for fields it needs added

# [provider.default.models]    # what a model accepts, when the built-in guess is wrong
# "reasoner-*" = { reasoning = ["low", "medium", "high", "ultra"] }   # weakest first

# builtin_tools = [{ type = "web_search" }]   # provider-side tools, passed through verbatim
                                              # entries use the active protocol's documented shape

# [runtime]                    # optional overrides (defaults shown)
# yolo = false
# max_context_tokens = 262144      # 256K; how much of the model's window to use, not its size.
                               # Raise it for a 1M-window model; lower it for a smaller one.
# max_agent_steps = 400
# max_subagents = 3            # retained children across the whole group; 0 disables spawn, maximum 32
# bash_output_tokens = 6000    # Bash output the model sees inline (1000-6000); later requests re-send it,
                               # so 2000 saves tokens on each; the full output stays in a file
# shell_timeout = 60
                               # (flipping it changes the tool block and thus the prompt-cache scope)
# language = "auto"           # auto follows your messages and injects nothing; set a language
                               # name (e.g. "Chinese") to force the reply language
# theme = "auto"               # auto follows the terminal's light or dark background; /theme
                               # previews built-ins, ui.themes and <data_dir>/themes/, and saves here
# attribution = true           # ask the model to end the commit messages and pull requests it
                               # writes with a "Generated with wizolt" line
# agents_md = true               # inject global AGENTS.md and the project's AGENTS.md files (or CLAUDE.md
                                 # fallback), repository root down to cwd, under one shared budget

# [ui.themes.mine]             # optional: define a theme here, or in <data_dir>/themes/mine.toml
# base = "slate"               # choose with /theme mine; restart after editing inline definitions
# [ui.themes.mine.colors]
# user = "#f2c97d"
# tool = "#8bd5ca"
# [ui.statusbar]
# theme = "inherit"           # own colors: an existing theme name, or auto for terminal light/dark
# [ui.divider]
# theme = "inherit"           # line, glow and labels; /theme divider theme forest

# [subagent]                   # defaults for new children; approval can override each field
# provider = "default"         # name of a configured provider entry; omitted = inherit parent
# model = ""                   # omitted/empty = inherit the selected provider's value
# reasoning = ""               # reasoning effort; omitted/empty = inherit
# api = ""                     # auto | chat | responses | anthropic; omitted/empty = inherit

# [vision]                     # optional: provider entry used only by explicit ViewImage calls
# provider = "vision"          # its model receives the local image and optional question, then
                               # returns a text observation to the active model
# [mcp.example]                # url (+ auth = "oauth") for remote, or command/args for stdio
# url = "https://example.com/mcp"
# auto_connect = false
# [[hooks.PreToolUse]]         # commands run around tool calls and turns, in Claude Code's format;
# matcher = "Bash"             # events: PreToolUse, PostToolUse, UserPromptSubmit, Stop.
# hooks = [{ type = "command", command = "~/bin/guard.sh" }]   # exit 2 blocks, stderr says why
"""

    # Provider keys live here, `<provider entry> = "<key>"`, so the config itself can be shared. A key
    # written in the config still works and wins; the startup migration moves it here.
    SECRETS_NAME: ClassVar[str] = "secrets.toml"
    SECRETS_HEADER: ClassVar[str] = """# wizolt API keys, one per provider entry in config.toml: <entry name> = "<key>".
# Keep this file private; config.toml can then be shared without them.
"""
    SECRETS_TEXT: ClassVar[str] = SECRETS_HEADER + 'default = ""\n'

    @classmethod
    def resolve_path(cls, path: str | None) -> str:
        return UserPaths.resolve_config_path(path)

    @classmethod
    def secrets_path(cls, config_path: str) -> str:
        """Beside the config path as given, not its symlink target, so a config linked from a
        dotfiles checkout never pulls keys into that checkout."""
        return os.path.join(os.path.dirname(config_path), cls.SECRETS_NAME)

    @classmethod
    def init(cls, path: str | None = None) -> tuple[str, bool]:
        config_path = cls.resolve_path(path)
        if os.path.exists(config_path):
            return config_path, False
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as file:
            file.write(cls.DEFAULT_TEXT)
        if not os.path.exists(secrets_path := cls.secrets_path(config_path)):
            cls.write_atomic(secrets_path, cls.SECRETS_TEXT)
        return config_path, True

    @classmethod
    def load(cls, path: str | None = None) -> Json:
        config_path = cls.resolve_path(path)
        try:
            data = cls.read_toml(config_path)
        except FileNotFoundError as error:
            raise ConfigError(f"config not found: {config_path}; run --init-config") from error
        # A secret for an entry this config lacks is ignored, not an error: one shared config may
        # define fewer entries than a machine's secrets, and a misspelled name still surfaces as
        # its entry's missing key.
        entries = cls.provider_entries(data)
        for name, key in cls.load_secrets(config_path).items():
            if name in entries and not entries[name].get("key"):
                entries[name]["key"] = key
        return data

    @classmethod
    def load_secrets(cls, config_path: str) -> dict[str, str]:
        """The non-empty keys in the secrets file beside `config_path`; none when it is absent."""
        path = cls.secrets_path(config_path)
        try:
            data = cls.read_toml(path)
        except FileNotFoundError:
            return {}
        for name, value in data.items():
            if not isinstance(value, str):
                raise ConfigError(f'{path}: `{name}` must be a string; this file holds only <provider entry> = "<key>" lines')
        return {name: value for name, value in data.items() if value}

    @classmethod
    def inline_keys(cls, path: str | None = None) -> InlineKeys:
        """The keys written in the config itself; none when there is no config yet."""
        config_path = cls.resolve_path(path)
        try:
            data = cls.read_toml(config_path)
        except FileNotFoundError:
            data = {}
        keys = {name: key for name, entry in cls.provider_entries(data).items() if isinstance(key := entry.get("key"), str) and key}
        record = os.path.join(os.path.expanduser(Config.data_dir_from(data)), "kept-config-keys")
        return InlineKeys(config_path, keys, record)

    @classmethod
    def migrate_keys(cls, path: str | None = None) -> list[str]:
        """Move the keys written in the config into secrets.toml, replacing older values there, and
        return the entries moved. Comments and layout of both files survive.

        Secrets are saved before the config, so an interruption leaves a key in both files, never in
        neither, and running it again finishes the move."""
        import tomlkit

        config_path = cls.resolve_path(path)
        config_real = os.path.realpath(config_path)
        document = cls.parse_document(config_real, "")
        moved = {name: entry for name, entry in cls.provider_entries(document).items() if isinstance(entry.get("key"), str) and entry["key"]}
        if not moved:
            return []
        secrets_real = os.path.realpath(cls.secrets_path(config_path))
        secrets = cls.parse_document(secrets_real, cls.SECRETS_HEADER)
        for name, entry in moved.items():
            secrets[name] = str(entry.pop("key"))
        cls.write_atomic(secrets_real, tomlkit.dumps(secrets))
        cls.write_atomic(config_real, tomlkit.dumps(document))
        return list(moved)

    @staticmethod
    def parse_document(path: str, missing: str) -> TOMLDocument:
        """A comment-preserving document of `path`, or of `missing` when the file is absent."""
        import tomlkit
        from tomlkit.exceptions import ParseError

        try:
            with open(path, encoding="utf-8") as file:
                return tomlkit.parse(file.read())
        except FileNotFoundError:
            return tomlkit.parse(missing)
        except ParseError as error:
            raise ConfigError(f"invalid config {path}: {error}") from error

    @staticmethod
    def read_toml(path: str) -> Json:
        try:
            with open(path, "rb") as file:
                return tomllib.load(file)
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f"invalid config {path}: {error}") from error

    @staticmethod
    def provider_entries(data: MutableMapping) -> dict[str, MutableMapping]:
        """Provider entries by name, as Config.from_dict reads them: the named tables, or else the
        flat `[provider]` block under its `active` name."""
        root = data.get("provider")
        if not isinstance(root, MutableMapping):
            return {}
        named = {name: value for name, value in root.items() if name != "active" and isinstance(value, MutableMapping)}
        return named or {str(root.get("active", "default")): root}

    @staticmethod
    def set_runtime(path: str, key: str, value: str) -> None:
        """Set `runtime.<key>` in the config file, keeping its comments, layout and permissions.

        The config holds provider keys, so the rewrite keeps the mode the user gave it (a 0600
        file stays 0600) and its sibling starts at 0600 rather than the umask's default. It is
        synced before one rename replaces the config, so neither an interrupted save nor a crash
        leaves half a file. A symlinked config (a dotfiles checkout) is written through, not
        replaced by a plain file. A `runtime` that is not a table is a ConfigError, as a load
        reports its other mistakes.
        """
        ConfigFile.set_ui_value(path, ("runtime",), key, value)

    @staticmethod
    def set_ui_value(path: str, section: tuple[str, ...], key: str, value: str | Json, *, overwrite: bool = True, expected: Json | None = None) -> None:
        """Persist a selection; optional expected record prevents stale read/modify/write.

        A changed record is left untouched when expected is supplied. This is for automatic
        normalization; explicit user choices normally overwrite the selected value.
        """
        # Lock the stable sidecar, not the replaced inode. Separate processes must read
        # the latest document inside the transaction or one preference can erase another.
        with ConfigFile._write_lease(path):
            ConfigFile._write_value(path, section, key, value, overwrite=overwrite, expected=expected)

    @staticmethod
    @contextlib.contextmanager
    def _write_lease(path: str):
        path = os.path.realpath(path)
        descriptor = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(descriptor, "a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ConfigError("Configuration is being updated by another process; retry shortly") from error
            yield

    @staticmethod
    def patch_table(path: str, section: tuple[str, ...], values: Json, reset: list[str], *, expected: Json) -> None:
        """Commit a validated table edit only if its read snapshot still matches.

        Validation happens outside the lease (potentially in a worker). Compare inside the
        lease and fail explicitly on conflict. Updating individual keys preserves comments.
        """
        import tomlkit

        path = os.path.realpath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with ConfigFile._write_lease(path):
            if os.path.exists(path):
                with open(path, encoding="utf-8") as file:
                    document = tomlkit.parse(file.read())
            else:
                document = tomlkit.document()
            table = ConfigFile._table(document, section, path)
            if dict(table) != expected:
                raise ConfigError("Plugin settings changed concurrently; retry the update")
            for key in reset:
                table.pop(key, None)
            for key, value in values.items():
                table[key] = value
            ConfigFile.write_atomic(path, tomlkit.dumps(document))

    @staticmethod
    def _table(document, section: tuple[str, ...], path: str):
        import tomlkit

        table = document
        for name in section:
            child = table.get(name)
            if child is None:
                child = tomlkit.table()
                table[name] = child
            elif not isinstance(child, MutableMapping):
                raise ConfigError(f"{'.'.join(section)} in {path} must be a table")
            table = child
        return table

    @staticmethod
    def _write_value(path: str, section: tuple[str, ...], key: str, value: str | Json, *, overwrite: bool, expected: Json | None) -> None:
        """Read and replace a complete document while the caller holds its write lease."""
        import tomlkit

        path = os.path.realpath(path)
        with open(path, encoding="utf-8") as file:
            document = tomlkit.parse(file.read())
        table = ConfigFile._table(document, section, path)
        if not overwrite and key in table:
            return
        if expected is not None and table.get(key) != expected:
            return
        table[key] = value
        ConfigFile.write_atomic(path, tomlkit.dumps(document))

    @staticmethod
    def write_atomic(path: str, text: str) -> None:
        """Replace `path` with `text` in one rename, keeping an existing file's mode; a new file is
        0600, since config and secrets files both may hold keys."""
        mode = stat.S_IMODE(os.stat(path).st_mode) if os.path.exists(path) else 0o600
        temporary = path + ".tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise


def compaction_provider_config(config: Config) -> ProviderConfig:
    """The provider entry compaction summaries run on, with two override layers applied.

    Base entry is `config.compaction_provider` or the active provider when unset. Per field,
    precedence is: the base entry's own `[provider.X.compaction]` nested table, then the global
    `[compaction]` section, then the base entry's own value — per-provider wins over global, and a
    fully empty pair leaves the entry's value. Never shares or mutates the base entry object:
    dataclasses.replace is shallow, so folding onto a shared object would leak compaction-only
    overrides into the main provider.
    Resolved per call, never cached, so a runtime /provider switch is picked up by the next
    compaction.
    """
    base = config.providers[config.compaction_provider or config.active_provider]
    provider = replace(base)
    for attr, per_value, global_value in (
        ("model", base.compaction_model, config.compaction_model),
        ("reasoning", base.compaction_reasoning, config.compaction_reasoning),
        ("api", base.compaction_api, config.compaction_api),
    ):
        value = per_value or global_value
        if value:
            setattr(provider, attr, value)
    return provider
