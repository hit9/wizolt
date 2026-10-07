"""How much is left on the AI providers you have configured, with /usage.

`/usage` asks every configured provider that offers a usage or balance API -- OpenCode Go,
DeepSeek, Kimi (Moonshot), z.ai, Synthetic and Command Code -- and prints one section per
provider: usage windows as percentages with reset times, balances as amounts. Providers are
matched by their API domain, so any entry name works. A provider that fails prints one
`error:` line; the rest still report.

```text
OpenCode Go
  rolling   12%  [██░░░░░░░░]  resets in 1h23m
  weekly    34%  [████░░░░░░]  resets in 2d
  monthly   56%  [██████░░░░]  resets in 19d
DeepSeek API
  balance  ¥110.00  (granted ¥10.00 + topped up ¥100.00)  ok
```

## Use

- Enable **usage** in `/plugins`, then type `/usage`. It makes one or two requests per
  supported provider, with that provider's configured key, and sends nothing else.
- A `!` marks a window at 80% or more, or a balance of 10 or less.
"""

# Built only on the public SDK, and read-only: the command reads config.toml and secrets.toml
# exactly as wizolt resolves them (an entry's own key wins, secrets.toml fills a missing one),
# then queries each matched provider. Keys travel only in Authorization headers; error lines
# carry HTTP codes and error classes, never headers, response bodies or keys. Sources are the
# only place that knows endpoints and response shapes; rows are either a usage Window or an
# account Balance, and one renderer formats them all.

from __future__ import annotations

import asyncio
import json
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

from wizolt.sdk import Context, Plugin

SDK_VERSION = 1

TIMEOUT = 10.0  # Seconds per request; one slow provider cannot stall the rest of the report.
WARN_PERCENT = 80.0  # A usage window at or above this marks its line with "!".
WARN_AMOUNT = 10.0  # A balance at or below this marks its line with "!".
BAR_CELLS = 10
SYMBOLS = {"CNY": "¥", "USD": "$"}


@dataclass(frozen=True)
class Window:
    """One usage window: server-computed percent and a reset time as epoch seconds (0 unknown)."""

    label: str
    percent: float
    resets_at: float = 0.0


@dataclass(frozen=True)
class Balance:
    """One account balance: a total amount plus where it came from."""

    currency: str
    total: float
    parts: tuple[tuple[str, float], ...] = ()
    available: bool = True


@dataclass(frozen=True)
class Report:
    """One provider's answer: rows when it worked, one note line when it did not."""

    title: str
    rows: tuple[Window | Balance, ...] = ()
    note: str = ""


class HttpError(Exception):
    """The provider answered with an HTTP error; the code is all that is reported."""

    def __init__(self, code: int) -> None:
        super().__init__(f"HTTP {code}")
        self.code = code


class Unreachable(Exception):
    """The provider could not be reached; the reason is a socket message, never a key."""


class BadResponse(ValueError):
    """A source's own verdict on a payload it cannot use; its message is safe to print."""


def get_json(url: str, key: str) -> object:
    # Providers ask for the client's own name, not an HTTP library's (Go's docs say so).
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "wizolt"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        raise HttpError(error.code) from None
    except urllib.error.URLError as error:
        raise Unreachable(str(error.reason) or "unreachable") from None


def mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def clamp(percent: Any) -> float:
    return max(0.0, min(100.0, float(percent)))


def timestamp(value: object, now: float) -> float:
    """Epoch seconds from a date, epoch milliseconds, a duration in seconds, or 0 when unknown."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds >= 1e12:
            return seconds / 1000.0
        return seconds if seconds >= 1e9 else now + seconds
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def hosted_by(url: str, domain: str) -> bool:
    """The url's host is the domain or one of its subdomains, never a lookalike or a path."""
    host = urllib.parse.urlsplit(url).netloc.lower()
    return host == domain or host.endswith("." + domain)


class Source:
    """The only place that knows one provider's endpoint and response shape."""

    title = ""

    def matches(self, url: str) -> bool:
        raise NotImplementedError

    def fetch(self, key: str, url: str) -> object:
        raise NotImplementedError

    def rows(self, data: object, now: float) -> tuple[Window | Balance, ...]:
        raise NotImplementedError

    def report_title(self, data: object) -> str:
        return self.title


class GoUsage(Source):
    title = "OpenCode Go"

    def matches(self, url: str) -> bool:
        return hosted_by(url, "opencode.ai") and urllib.parse.urlsplit(url).path.lower().startswith("/zen/go")

    def fetch(self, key: str, url: str) -> object:
        return get_json("https://opencode.ai/zen/go/v1/usage", key)

    def rows(self, data: object, now: float) -> tuple[Window | Balance, ...]:
        usage = mapping(mapping(data).get("usage"))
        rows = []
        for label in ("rolling", "weekly", "monthly"):
            meter = mapping(usage.get(label))
            if "percent" in meter:
                rows.append(Window(label, clamp(meter["percent"]), timestamp(meter.get("resetsAt") or meter.get("resetInSec"), now)))
        if not rows:
            raise BadResponse("no subscription or no usage windows")
        return tuple(rows)


class DeepSeekBalance(Source):
    title = "DeepSeek API"

    def matches(self, url: str) -> bool:
        return hosted_by(url, "deepseek.com")

    def fetch(self, key: str, url: str) -> object:
        return get_json("https://api.deepseek.com/user/balance", key)

    def rows(self, data: object, now: float) -> tuple[Window | Balance, ...]:
        rows = []
        for info in mapping(data).get("balance_infos") or []:
            info = mapping(info)
            parts = tuple((name, float(info.get(field) or 0.0)) for name, field in (("granted", "granted_balance"), ("topped up", "topped_up_balance")))
            available = bool(mapping(data).get("is_available", True))
            rows.append(Balance(str(info.get("currency", "?")), float(info.get("total_balance") or 0.0), parts, available))
        if not rows:
            raise BadResponse("no balance data")
        return tuple(rows)


class KimiBalance(Source):
    title = "Kimi"
    PATH = "/v1/users/me/balance"
    NATIONAL, INTERNATIONAL = "https://api.moonshot.cn", "https://api.moonshot.ai"

    def matches(self, url: str) -> bool:
        return hosted_by(url, "moonshot.cn") or hosted_by(url, "moonshot.ai")

    def _origin(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url if "//" in url else f"https://{url}")
        base = f"{parts.scheme}://{parts.netloc}"
        if parts.scheme in ("http", "https") and (hosted_by(base, "moonshot.cn") or hosted_by(base, "moonshot.ai")):
            return base
        return self.NATIONAL

    def fetch(self, key: str, url: str) -> object:
        base = self._origin(url)
        try:
            answer = get_json(base + self.PATH, key)
        except HttpError as error:
            # National and international keys are separate; a rejected key gets one try on the
            # other host before the report calls it an error.
            if error.code not in (401, 403):
                raise
            base = self.INTERNATIONAL if base == self.NATIONAL else self.NATIONAL
            answer = get_json(base + self.PATH, key)
        # The national host balances in CNY, the international one in USD (its balance docs).
        return {"answer": answer, "currency": "USD" if base == self.INTERNATIONAL else "CNY"}

    def rows(self, data: object, now: float) -> tuple[Window | Balance, ...]:
        payload = mapping(mapping(mapping(data).get("answer")).get("data"))
        if "available_balance" not in payload:
            raise BadResponse("no balance data")
        parts = tuple((name, float(payload.get(field) or 0.0)) for name, field in (("cash", "cash_balance"), ("voucher", "voucher_balance")))
        return (Balance(str(mapping(data).get("currency") or "CNY"), float(payload["available_balance"]), parts),)


class ZaiQuota(Source):
    title = "Z.ai"
    QUOTA_URL = "https://api.z.ai/api/monitor/usage/quota/limit"
    PLAN_URL = "https://api.z.ai/api/biz/subscription/list"
    WINDOW_LABELS: ClassVar[dict[tuple[object, object], str]] = {(3, 5): "session", (6, 7): "weekly"}  # The plan's own unit/number periods.

    def matches(self, url: str) -> bool:
        return hosted_by(url, "z.ai")

    def fetch(self, key: str, url: str) -> object:
        data = {"quota": get_json(self.QUOTA_URL, key)}
        try:
            data["plans"] = get_json(self.PLAN_URL, key)
        except (HttpError, Unreachable, TimeoutError, json.JSONDecodeError):
            data["plans"] = {}  # The plan name is a bonus; quotas alone still report.
        return data

    def report_title(self, data: object) -> str:
        plans = mapping(mapping(data).get("plans")).get("data") or []
        name = next((" ".join(str(item["productName"]).split()) for item in plans if mapping(item).get("productName")), "")
        return f"{self.title} {name}" if name else self.title

    def rows(self, data: object, now: float) -> tuple[Window | Balance, ...]:
        limits = mapping(mapping(mapping(data).get("quota")).get("data")).get("limits") or []
        rows = []
        for item in limits:
            item = mapping(item)
            if item.get("type") == "TOKENS_LIMIT":
                label = self.WINDOW_LABELS.get((item.get("unit"), item.get("number")), "tokens")
                rows.append(Window(label, self._percent(item), timestamp(item.get("nextResetTime"), now)))
            elif item.get("type") == "TIME_LIMIT":
                rows.append(Window("searches", self._percent(item), 0.0))
        if not rows:
            raise BadResponse("no quota data")
        return tuple(rows)

    @staticmethod
    def _percent(item: dict) -> float:
        if item.get("percentage") is not None:
            return clamp(item["percentage"])
        limit = float(item.get("usage") or 0.0)
        return clamp(100.0 * float(item.get("currentValue") or 0.0) / limit) if limit else 0.0


class SyntheticQuotas(Source):
    title = "Synthetic"

    def matches(self, url: str) -> bool:
        return hosted_by(url, "synthetic.new")

    def fetch(self, key: str, url: str) -> object:
        return get_json("https://api.synthetic.new/v2/quotas", key)

    def rows(self, data: object, now: float) -> tuple[Window | Balance, ...]:
        rows = []
        rolling = mapping(mapping(data).get("rollingFiveHourLimit"))
        limit = float(rolling.get("max") or 0.0)
        if limit:
            used = max(0.0, limit - float(rolling.get("remaining") or 0.0))
            rows.append(Window("5h rate", clamp(100.0 * used / limit), 0.0))
        weekly = mapping(mapping(data).get("weeklyTokenLimit"))
        if weekly.get("percentRemaining") is not None:
            rows.append(Window("weekly", clamp(100.0 - float(weekly["percentRemaining"])), timestamp(weekly.get("nextRegenAt"), now)))
        if not rows:
            subscription = mapping(mapping(data).get("subscription"))
            if subscription.get("limit"):
                percent = 100.0 * float(subscription.get("requests") or 0.0) / float(subscription["limit"])
                rows.append(Window("subscription", clamp(percent), timestamp(subscription.get("renewsAt"), now)))
        if not rows:
            raise BadResponse("no quota data")
        return tuple(rows)


class CommandCodeQuota(Source):
    """Command Code's own `/usage` endpoints: API-key auth, undocumented, /alpha paths."""

    title = "Command Code"
    WHOAMI_URL = "https://api.commandcode.ai/alpha/whoami"
    CREDITS_URL = "https://api.commandcode.ai/alpha/billing/credits"

    def matches(self, url: str) -> bool:
        return hosted_by(url, "commandcode.ai")

    def fetch(self, key: str, url: str) -> object:
        whoami = get_json(self.WHOAMI_URL, key)
        org_id = str(mapping(mapping(whoami).get("org")).get("id") or "")
        query = f"?orgId={urllib.parse.quote(org_id)}" if org_id else ""
        return {"credits": get_json(self.CREDITS_URL + query, key)}

    def rows(self, data: object, now: float) -> tuple[Window | Balance, ...]:
        credits = mapping(mapping(data).get("credits"))
        rows = []
        for field, label in (("fiveHour", "5-hour"), ("weekly", "weekly")):
            window = mapping(mapping(credits.get("windowLimits")).get(field))
            cap = float(window.get("cap") or 0.0)
            if cap:
                percent = clamp(100.0 * float(window.get("used") or 0.0) / cap)
                rows.append(Window(label, percent, timestamp(window.get("resetAt"), now)))
        amounts = mapping(credits.get("credits"))
        fields = {"monthly": "monthlyCredits", "purchased": "purchasedCredits", "free": "freeCredits"}
        values = {name: float(amounts.get(field) or 0.0) for name, field in fields.items()}
        if any(amounts.get(field) is not None for field in fields.values()):
            parts = tuple((name, value) for name, value in values.items() if value)
            rows.append(Balance("USD", sum(values.values()), parts))
        if not rows:
            raise BadResponse("no usage windows or credits")
        return tuple(rows)


SOURCES = (GoUsage(), DeepSeekBalance(), KimiBalance(), ZaiQuota(), SyntheticQuotas(), CommandCodeQuota())


def provider_entries(data: dict) -> dict[str, dict]:
    """The provider tables wizolt reads: the named entries, or the flat block under its active name."""
    root = data.get("provider")
    if not isinstance(root, dict):
        return {}
    named = {name: value for name, value in root.items() if name != "active" and isinstance(value, dict)}
    return named or {str(root.get("active", "default")): root}


def load_secrets(path: Path) -> dict[str, str]:
    """The non-empty root keys of secrets.toml, as wizolt fills missing entry keys from them."""
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return {name: value for name, value in data.items() if isinstance(value, str) and value}


def resolve_key(name: str, entry: dict, secrets: dict[str, str]) -> str:
    """The entry's own key first, then the secret of the same name, as wizolt resolves them."""
    return str(entry.get("key") or "").strip() or str(secrets.get(name) or "").strip()


async def fetch_report(name: str, entry: dict, source: Source, secrets: dict[str, str]) -> Report:
    """One provider's report, whatever went wrong: failures stay inside their own section."""
    key = resolve_key(name, entry, secrets)
    if not key:
        return Report(source.title, note="skipped: no key configured")
    try:
        data = await asyncio.to_thread(source.fetch, key, str(entry.get("url") or ""))
        return Report(source.report_title(data), rows=source.rows(data, time.time()))
    except asyncio.CancelledError:
        raise
    except HttpError as error:
        return Report(source.title, note=f"error: {error}")
    except Unreachable as error:
        return Report(source.title, note=f"error: network ({error})")
    except TimeoutError:
        return Report(source.title, note="error: timeout")
    except BadResponse as error:
        return Report(source.title, note=f"error: {error or 'unparsable response'}")
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, UnicodeDecodeError):
        return Report(source.title, note="error: unparsable response")
    except Exception:  # noqa: BLE001 - one provider's unexpected failure stays inside its own section.
        return Report(source.title, note="error: failed")


def relative(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours}h" if hours else f"{days}d"


def window_line(row: Window) -> str:
    percent = round(row.percent)
    filled = round(BAR_CELLS * percent / 100.0)
    bar = "█" * filled + "░" * (BAR_CELLS - filled)
    mark = "! " if percent >= WARN_PERCENT else ""
    reset = f"  resets in {relative(row.resets_at - time.time())}" if row.resets_at else ""
    return f"{mark}{row.label:<8} {percent:3.0f}%  [{bar}]{reset}"


def balance_line(row: Balance) -> str:
    symbol = SYMBOLS.get(row.currency, f"{row.currency} ")
    total = round(row.total, 2)
    parts = " + ".join(f"{name} {symbol}{amount:.2f}" for name, amount in row.parts)
    detail = f"  ({parts})" if parts else ""
    state = "ok" if row.available else "insufficient"
    mark = "! " if (not row.available or total <= WARN_AMOUNT) else ""
    return f"{mark}balance  {symbol}{total:.2f}{detail}  {state}"


def section(title: str, report: Report) -> str:
    if report.note:
        return f"{title}\n  {report.note}"
    lines = [window_line(row) if isinstance(row, Window) else balance_line(row) for row in report.rows]
    return "\n".join([title, *(f"  {line}" for line in lines)])


async def usage(context: Context, arguments: Mapping[str, Any]) -> str:
    root = Path(context.data_dir or "~/.wizolt").expanduser()
    config_path = Path(context.config_path) if context.config_path else root / "config.toml"
    try:
        with config_path.open("rb") as file:
            config = tomllib.load(file)
    except FileNotFoundError:
        return "No config.toml yet: nothing configured."
    except (OSError, tomllib.TOMLDecodeError):
        return "config.toml is unreadable; fix it and try again."
    secrets = load_secrets(config_path.parent / "secrets.toml")  # Beside the config, as wizolt resolves them.
    entries = provider_entries(config)
    plans = []
    for source in SOURCES:
        for name, entry in entries.items():
            url = str(entry.get("url") or "")
            if url and source.matches(url):
                plans.append((name, entry, source))
    if not plans:
        return "No configured provider matches a supported usage API (OpenCode Go, DeepSeek, Kimi, z.ai, Synthetic, Command Code)."
    reports = list(
        zip(
            plans,
            await asyncio.gather(*(fetch_report(name, entry, source, secrets) for name, entry, source in plans)),
        )
    )
    counts = Counter(source for _, _, source in plans)
    sections = []
    for (name, _, source), report in reports:
        title = f"{report.title} ({name})" if counts[source] > 1 else report.title
        sections.append(section(title, report))
    return "\n\n".join(sections)


def setup(plugin: Plugin) -> None:
    """Default enablement belongs to the installation catalog, never to plugin source code."""
    plugin.command(
        "usage",
        "Show usage and balance for configured providers (OpenCode Go, DeepSeek, Kimi, z.ai, Synthetic, Command Code); one or two API requests each",
        usage,
    )
