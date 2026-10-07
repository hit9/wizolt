"""/usage: which providers it asks, what it prints, and what one provider's failure costs.

A handler runs in a plugin worker subprocess, so most cases fake the network at the plugin's own
``get_json`` seam and call the handler in process. Two tests drive the plugin worker through
``PluginRuntime``, and three keep the transport real against a loopback server the test starts
itself. Nothing reaches a provider.
"""

from __future__ import annotations

import asyncio
import email.message
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import wizolt.plugins.builtin.usage as usage_plugin
from wizolt.plugins.runtime import PluginRuntime
from wizolt.sdk import Context, Line, Text

BUILTIN = Path(__file__).parents[1] / "wizolt/plugins/builtin"
NOW = 1_700_000_000.0  # Any wall clock: only the distance to a reset is ever printed.
UNMATCHED = "No configured provider matches a supported usage API (OpenCode Go, DeepSeek, Kimi, z.ai, Synthetic, Command Code)."

# Configured urls, and the endpoints each source asks for.
GO = "https://opencode.ai/zen/go/v1"
DEEPSEEK = "https://api.deepseek.com"
KIMI = "https://api.moonshot.cn/v1"
ZAI = "https://api.z.ai/api/paas/v4"
SYNTHETIC = "https://api.synthetic.new/v1"
COMMANDCODE = "https://api.commandcode.ai/provider/v1"
GO_USAGE = "https://opencode.ai/zen/go/v1/usage"
DEEPSEEK_BALANCE = "https://api.deepseek.com/user/balance"
KIMI_BALANCE = "https://api.moonshot.cn/v1/users/me/balance"
KIMI_BALANCE_AI = "https://api.moonshot.ai/v1/users/me/balance"
ZAI_QUOTA = "https://api.z.ai/api/monitor/usage/quota/limit"
ZAI_PLAN = "https://api.z.ai/api/biz/subscription/list"
SYNTHETIC_QUOTAS = "https://api.synthetic.new/v2/quotas"
COMMANDCODE_WHOAMI = "https://api.commandcode.ai/alpha/whoami"
COMMANDCODE_CREDITS = "https://api.commandcode.ai/alpha/billing/credits"
COMMANDCODE_CREDITS_ORG = "https://api.commandcode.ai/alpha/billing/credits?orgId=org_123"


class Wires:
    """A stand-in for the provider network: payloads by url, and a log of every call.

    An answer may be a callable of the key, to serve two entries of one provider differently,
    and may be an exception instance to fail. An unrouted url fails the test loudly instead of
    turning into the plugin's own ``error: failed``.
    """

    def __init__(self) -> None:
        self.answers: dict[str, object] = {}
        self.delays: dict[str, float] = {}
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, key: str) -> object:
        self.calls.append((url, key))
        if url not in self.answers:
            pytest.fail(f"the plugin asked for an unrouted url: {url}")
        time.sleep(self.delays.get(url, 0.0))
        answer = self.answers[url]
        if callable(answer):
            answer = answer(key)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture
def wires(monkeypatch) -> Wires:
    wire = Wires()
    monkeypatch.setattr(usage_plugin, "get_json", wire)
    return wire


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch) -> None:
    """Reset lines are relative times; a frozen clock makes them exact strings."""
    monkeypatch.setattr(usage_plugin, "time", SimpleNamespace(time=lambda: NOW))


@pytest.fixture
async def runtime(tmp_path):
    """The real plugin worker, enabled from the builtin source, as a user's /plugins would."""
    state = {"context": context(tmp_path)}
    instance = PluginRuntime(lambda: state["context"])
    try:
        await instance.manage("enable", str(BUILTIN / "usage.py"))
        yield instance
    finally:
        await instance.close()


def context(root: Path, config_path: str = "") -> Context:
    """Only ``data_dir`` and ``config_path`` decide which config.toml the command opens."""
    return Context(agent_id="root", agent_name="main", cwd=str(root), status="idle", context_percent=0, elapsed=0, model="m", now=0, data_dir=str(root), config_path=config_path)


def configured(root: Path, config: str, secrets: str = "") -> Context:
    (root / "config.toml").write_text(config)
    if secrets:
        (root / "secrets.toml").write_text(secrets)
    return context(root)


def provider_config(*entries: tuple[str, str, str | None]) -> str:
    """A config.toml of provider entries -- (name, url, key), key None left out."""
    blocks = []
    for name, url, key in entries:
        lines = [f"[provider.{name}]", f'url = "{url}"']
        if key is not None:
            lines.append(f'key = "{key}"')
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n"


def sections(*blocks: str) -> str:
    """What the command prints for these sections, in this order."""
    return "\n\n".join(blocks)


def plain(result: str | list[Line]) -> str:
    """A report answer flattened to text: a plain answer stays, styled rows join."""
    return result if isinstance(result, str) else "\n".join(line.text for line in result)


def deepseek(total: str, granted: str = "0", topped: str = "0", *, currency: str = "CNY", available: bool = True) -> dict:
    return {
        "is_available": available,
        "balance_infos": [{"currency": currency, "total_balance": total, "granted_balance": granted, "topped_up_balance": topped}],
    }


async def test_every_supported_domain_reports_in_sources_order(wires, tmp_path):
    # Configured back to front, and the entry configured first answers last: sections follow
    # SOURCES, never the config file or whichever request came back first.
    wires.delays[SYNTHETIC_QUOTAS] = 0.2
    wires.answers.update(
        {
            COMMANDCODE_WHOAMI: {"org": {"id": "org_123", "login": "acme"}},
            COMMANDCODE_CREDITS_ORG: {
                "windowLimits": {
                    "fiveHour": {"used": 2.8, "cap": 14, "resetAt": 1_700_007_200_000},
                    "weekly": {"used": 14, "cap": 35, "resetAt": "2023-11-21T22:13:20+00:00"},
                },
                "credits": {"monthlyCredits": 41.2, "purchasedCredits": 5, "freeCredits": 0},
            },
            SYNTHETIC_QUOTAS: {"weeklyTokenLimit": {"percentRemaining": 66, "nextRegenAt": "2023-11-14T23:00:00+00:00"}},
            ZAI_QUOTA: {
                "data": {
                    "limits": [
                        {"type": "TOKENS_LIMIT", "unit": 3, "number": 5, "percentage": 36, "nextResetTime": 1_700_001_800_000},
                        {"type": "TIME_LIMIT", "currentValue": 3, "usage": 100},
                    ]
                }
            },
            ZAI_PLAN: {"data": [{"productName": "GLM Coding Plan"}]},
            KIMI_BALANCE: {"data": {"available_balance": "42.5", "cash_balance": "2.5", "voucher_balance": "40"}},
            DEEPSEEK_BALANCE: deepseek("110", "10", "100"),
            GO_USAGE: {"usage": {"rolling": {"percent": 12, "resetInSec": 5000}}},
        }
    )
    config = provider_config(("cc", COMMANDCODE, "k"), ("synth", SYNTHETIC, "k"), ("z", ZAI, "k"), ("kimi", KIMI, "k"), ("deep", DEEPSEEK, "k"), ("go", GO, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == sections(
        "OpenCode Go\nrolling   12%  [█░░░░░░░░░]  resets in 1h23m",
        "DeepSeek API\nbalance  ¥110.00  (granted ¥10.00 + topped up ¥100.00)  ok",
        "Kimi\nbalance  ¥42.50  (cash ¥2.50 + voucher ¥40.00)  ok",
        "Z.ai GLM Coding Plan\nsession   36%  [████░░░░░░]  resets in 30m\nsearches   3%  [░░░░░░░░░░]",
        "Synthetic\nweekly    34%  [███░░░░░░░]  resets in 46m",
        "Command Code\n5-hour    20%  [██░░░░░░░░]  resets in 2h\nweekly    40%  [████░░░░░░]  resets in 7d\nbalance  $46.20  (monthly $41.20 + purchased $5.00)  ok",
    )
    assert len(wires.calls) == 8  # Two calls each for z.ai and Command Code, one each for the rest.


async def test_a_reset_time_is_read_as_a_duration_or_an_epoch_time(wires, tmp_path):
    # Durations count from now; 1e9 and up is an absolute epoch time, 1e12 and up milliseconds.
    wires.answers[GO_USAGE] = {
        "usage": {
            "rolling": {"percent": 12, "resetInSec": 5000},
            "weekly": {"percent": 34, "resetsAt": 1_700_007_200},
            "monthly": {"percent": 56, "resetsAt": 1_700_010_800_000},
        }
    }
    config = provider_config(("go", GO, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == sections(
        "OpenCode Go\nrolling   12%  [█░░░░░░░░░]  resets in 1h23m\nweekly    34%  [███░░░░░░░]  resets in 2h\nmonthly   56%  [██████░░░░]  resets in 3h"
    )


async def test_a_lone_unsupported_config_names_what_is_supported(wires, tmp_path):
    config = provider_config(("openai", "https://api.openai.com/v1", "k"), ("local", "", "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == UNMATCHED
    assert wires.calls == []


async def test_an_unsupported_or_urlless_entry_is_not_queried(wires, tmp_path):
    wires.answers[DEEPSEEK_BALANCE] = deepseek("110", "10", "100")
    config = provider_config(("openai", "https://api.openai.com/v1", "k"), ("local", "", "k"), ("deep", DEEPSEEK, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "DeepSeek API\nbalance  ¥110.00  (granted ¥10.00 + topped up ¥100.00)  ok"
    assert wires.calls == [(DEEPSEEK_BALANCE, "k")]


async def test_a_provider_without_a_key_is_skipped_where_it_stands(wires, tmp_path):
    config = provider_config(("deep", DEEPSEEK, None))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "DeepSeek API\nskipped: no key configured"
    assert wires.calls == []


async def test_the_entry_key_wins_and_a_secret_fills_the_missing_one(wires, tmp_path):
    wires.answers[DEEPSEEK_BALANCE] = deepseek("110", "10", "100")
    wires.answers[SYNTHETIC_QUOTAS] = {"weeklyTokenLimit": {"percentRemaining": 10}}
    config = provider_config(("mine", DEEPSEEK, "own"), ("theirs", SYNTHETIC, None))
    secrets = 'mine = "shadowed"\ntheirs = "from-secrets"\n'
    result = plain(await usage_plugin.usage(configured(tmp_path, config, secrets), {}))
    assert {url: key for url, key in wires.calls} == {DEEPSEEK_BALANCE: "own", SYNTHETIC_QUOTAS: "from-secrets"}
    assert "shadowed" not in result


async def test_two_entries_of_one_provider_are_told_apart_by_name(wires, tmp_path):
    # Two keys of one provider are two accounts, told apart by their entry names.
    wires.answers[DEEPSEEK_BALANCE] = deepseek("110", "10", "100")
    config = provider_config(("work", DEEPSEEK, "k-work"), ("home", DEEPSEEK, "k-home"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == sections(
        "DeepSeek API (work)\nbalance  ¥110.00  (granted ¥10.00 + topped up ¥100.00)  ok",
        "DeepSeek API (home)\nbalance  ¥110.00  (granted ¥10.00 + topped up ¥100.00)  ok",
    )


async def test_two_entries_sharing_url_and_key_are_asked_once_and_named_together(wires, tmp_path):
    # The same url and the same key are one account: one request, one section, and every
    # entry name in its title, so the report still shows everything that was configured.
    wires.answers[DEEPSEEK_BALANCE] = deepseek("110", "10", "100")
    config = provider_config(("work", DEEPSEEK, "k"), ("home", DEEPSEEK, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == (
        "DeepSeek API (work, home)\nbalance  ¥110.00  (granted ¥10.00 + topped up ¥100.00)  ok"
    )
    assert wires.calls == [(DEEPSEEK_BALANCE, "k")]


async def test_two_entries_of_one_provider_keep_their_names_when_their_titles_differ(wires, tmp_path):
    # Two accounts of one provider, one failing and one reporting its plan's name: the names are
    # counted per source, so both sections still say which entry they came from.
    def plan(key: str) -> object:
        return usage_plugin.HttpError(403) if key == "work" else {"data": [{"productName": "GLM Coding Plan"}]}

    wires.answers[ZAI_QUOTA] = {"data": {"limits": [{"type": "TOKENS_LIMIT", "unit": 6, "number": 7, "percentage": 40}]}}
    wires.answers[ZAI_PLAN] = plan
    config = provider_config(("work", ZAI, "work"), ("home", ZAI, "home"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == sections(
        "Z.ai (work)\nweekly    40%  [████░░░░░░]",
        "Z.ai GLM Coding Plan (home)\nweekly    40%  [████░░░░░░]",
    )


async def test_marks_a_window_at_80_and_a_balance_at_10(wires, tmp_path):
    # The printed numbers decide the marks: 79.9 reads as 80% and is marked, a balance of 10.004
    # rounds to 10.00 and is marked, and 10.01 is above the threshold.
    wires.answers[GO_USAGE] = {"usage": {"rolling": {"percent": 79.9}, "weekly": {"percent": 79}}}

    def balance(key: str) -> dict:
        if key == "poor":
            return deepseek("10.004", "0", "10.004")
        if key == "edge":
            return deepseek("10.01", "0", "10.01")
        if key == "rich":
            return deepseek("250", "0", "250", currency="USD")
        return deepseek("500", "0", "500", currency="XXX", available=False)

    wires.answers[DEEPSEEK_BALANCE] = balance
    config = provider_config(("go", GO, "k"), ("dry", DEEPSEEK, "dry"), ("poor", DEEPSEEK, "poor"), ("edge", DEEPSEEK, "edge"), ("rich", DEEPSEEK, "rich"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == sections(
        "OpenCode Go\n! rolling   80%  [████████░░]\nweekly    79%  [████████░░]",
        "DeepSeek API (dry)\n! balance  XXX 500.00  (granted XXX 0.00 + topped up XXX 500.00)  insufficient",
        "DeepSeek API (poor)\n! balance  ¥10.00  (granted ¥0.00 + topped up ¥10.00)  ok",
        "DeepSeek API (edge)\nbalance  ¥10.01  (granted ¥0.00 + topped up ¥10.01)  ok",
        "DeepSeek API (rich)\nbalance  $250.00  (granted $0.00 + topped up $250.00)  ok",
    )


async def test_each_failure_becomes_one_error_line_in_its_own_section(wires, tmp_path):
    wires.answers.update(
        {
            GO_USAGE: json.JSONDecodeError("Expecting value", "", 0),
            DEEPSEEK_BALANCE: deepseek("110", "10", "100"),
            KIMI_BALANCE: usage_plugin.HttpError(503),
            ZAI_QUOTA: usage_plugin.Unreachable("name or service not known"),
            SYNTHETIC_QUOTAS: {"rollingFiveHourLimit": {}},
        }
    )
    config = provider_config(("deep", DEEPSEEK, "k"), ("go", GO, "k"), ("kimi", KIMI, "k"), ("z", ZAI, "k"), ("synth", SYNTHETIC, "k"))
    result = plain(await usage_plugin.usage(configured(tmp_path, config), {}))
    assert result == sections(
        "OpenCode Go\nerror: unparsable response",
        "DeepSeek API\nbalance  ¥110.00  (granted ¥10.00 + topped up ¥100.00)  ok",
        "Kimi\nerror: HTTP 503",
        "Z.ai\nerror: network (name or service not known)",
        "Synthetic\nerror: no quota data",
    )
    assert result.count("error:") == 4


async def test_a_subscription_without_usage_windows_says_so(wires, tmp_path):
    wires.answers[GO_USAGE] = {"usage": {}}
    config = provider_config(("go", GO, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "OpenCode Go\nerror: no subscription or no usage windows"


@pytest.mark.parametrize(
    ("url", "asked", "retried", "symbol"),
    [(KIMI, KIMI_BALANCE, KIMI_BALANCE_AI, "$"), ("https://api.moonshot.ai/v1", KIMI_BALANCE_AI, KIMI_BALANCE, "¥")],
)
async def test_a_rejected_kimi_key_retries_the_other_host_and_its_currency(wires, tmp_path, url, asked, retried, symbol):
    # The international host balances in USD, the national one in CNY; whichever answered decides.
    wires.answers[asked] = usage_plugin.HttpError(401)
    wires.answers[retried] = {"data": {"available_balance": "300", "cash_balance": "0", "voucher_balance": "0"}}
    config = provider_config(("kimi", url, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == f"Kimi\nbalance  {symbol}300.00  (cash {symbol}0.00 + voucher {symbol}0.00)  ok"
    assert wires.calls == [(asked, "k"), (retried, "k")]  # The same key, once per host.


async def test_a_moonshot_url_without_a_scheme_falls_back_to_the_national_host(wires, tmp_path):
    wires.answers[KIMI_BALANCE] = {"data": {"available_balance": "300", "cash_balance": "0", "voucher_balance": "0"}}
    config = provider_config(("kimi", "//api.moonshot.cn", "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "Kimi\nbalance  ¥300.00  (cash ¥0.00 + voucher ¥0.00)  ok"
    assert wires.calls == [(KIMI_BALANCE, "k")]


async def test_a_kimi_failure_other_than_a_rejection_is_not_retried(wires, tmp_path):
    wires.answers[KIMI_BALANCE] = usage_plugin.HttpError(500)
    config = provider_config(("kimi", KIMI, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "Kimi\nerror: HTTP 500"
    assert wires.calls == [(KIMI_BALANCE, "k")]


@pytest.mark.parametrize("failure", [usage_plugin.HttpError(403), TimeoutError(), json.JSONDecodeError("Expecting value", "", 0)])
async def test_a_failed_plan_lookup_of_any_kind_still_reports_the_quotas(wires, tmp_path, failure):
    wires.answers[ZAI_QUOTA] = {"data": {"limits": [{"type": "TOKENS_LIMIT", "unit": 6, "number": 7, "percentage": 40}]}}
    wires.answers[ZAI_PLAN] = failure
    config = provider_config(("z", ZAI, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "Z.ai\nweekly    40%  [████░░░░░░]"


async def test_a_plan_name_with_line_breaks_stays_on_one_line(wires, tmp_path):
    wires.answers[ZAI_QUOTA] = {"data": {"limits": [{"type": "TOKENS_LIMIT", "unit": 6, "number": 7, "percentage": 40}]}}
    wires.answers[ZAI_PLAN] = {"data": [{"productName": "GLM\nCoding\tPlan"}]}
    config = provider_config(("z", ZAI, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "Z.ai GLM Coding Plan\nweekly    40%  [████░░░░░░]"


async def test_commandcode_computes_the_percent_and_prints_the_nonzero_credit_parts(wires, tmp_path):
    # A personal key: whoami names no org, so credits is asked without a query. The API reports
    # amounts used against each window's cap, not percents, and credits that are zero are left
    # out of the balance's parts.
    wires.answers[COMMANDCODE_WHOAMI] = {"user": {"login": "me"}}
    wires.answers[COMMANDCODE_CREDITS] = {
        "windowLimits": {
            "fiveHour": {"used": 2.8, "cap": 14, "resetAt": 1_700_007_200_000},
            "weekly": {"used": 14, "cap": 35, "resetAt": "2023-11-21T22:13:20+00:00"},
        },
        "credits": {"monthlyCredits": 41.2, "purchasedCredits": 5, "freeCredits": 0},
    }
    config = provider_config(("cc", COMMANDCODE, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == (
        "Command Code\n"
        "5-hour    20%  [██░░░░░░░░]  resets in 2h\n"
        "weekly    40%  [████░░░░░░]  resets in 7d\n"
        "balance  $46.20  (monthly $41.20 + purchased $5.00)  ok"
    )
    assert wires.calls == [(COMMANDCODE_WHOAMI, "k"), (COMMANDCODE_CREDITS, "k")]


async def test_commandcode_scopes_credits_to_the_org_whoami_names(wires, tmp_path):
    wires.answers[COMMANDCODE_WHOAMI] = {"org": {"id": "org_123", "login": "acme"}}
    wires.answers[COMMANDCODE_CREDITS_ORG] = {"windowLimits": {"weekly": {"used": 4, "cap": 8}}, "credits": {"monthlyCredits": 20}}
    config = provider_config(("cc", COMMANDCODE, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "Command Code\nweekly    50%  [█████░░░░░]\nbalance  $20.00  (monthly $20.00)  ok"
    assert wires.calls == [(COMMANDCODE_WHOAMI, "k"), (COMMANDCODE_CREDITS_ORG, "k")]


async def test_a_rejected_commandcode_key_fails_without_asking_for_credits(wires, tmp_path):
    wires.answers[COMMANDCODE_WHOAMI] = usage_plugin.HttpError(401)
    config = provider_config(("cc", COMMANDCODE, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "Command Code\nerror: HTTP 401"
    assert wires.calls == [(COMMANDCODE_WHOAMI, "k")]


async def test_a_commandcode_account_without_windows_reports_only_the_balance(wires, tmp_path):
    # Balance-only usage has no rolling windows; the balance still reports on its own.
    wires.answers[COMMANDCODE_WHOAMI] = {"user": {"login": "me"}}
    wires.answers[COMMANDCODE_CREDITS] = {"credits": {"purchasedCredits": 30, "freeCredits": 1.5}}
    config = provider_config(("cc", COMMANDCODE, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "Command Code\nbalance  $31.50  (purchased $30.00 + free $1.50)  ok"


async def test_commandcode_without_windows_or_credits_says_so(wires, tmp_path):
    wires.answers[COMMANDCODE_WHOAMI] = {"user": {"login": "me"}}
    wires.answers[COMMANDCODE_CREDITS] = {"windowLimits": {"fiveHour": {"used": 0, "cap": 0}}}
    config = provider_config(("cc", COMMANDCODE, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "Command Code\nerror: no usage windows or credits"


async def test_a_lookalike_host_or_a_pasted_path_is_not_the_provider(wires, tmp_path):
    # `deepseek.com.evil.tld` merely contains the domain, `notz.ai` merely ends with it,
    # `evil.tld/opencode.ai/zen/go` merely carries Go's path: no key is asked from anyone.
    config = provider_config(
        ("lookalike", "https://api.deepseek.com.evil.tld/v1", "k"),
        ("pasted", "https://evil.tld/opencode.ai/zen/go/v1", "k"),
        ("moonshotish", "https://api.moonshot.evil.tld/v1", "k"),
        ("plain", "https://notz.ai/v1", "k"),
    )
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == UNMATCHED
    assert wires.calls == []


async def test_a_null_resetsAt_falls_back_to_the_seconds_left(wires, tmp_path):
    wires.answers[GO_USAGE] = {"usage": {"rolling": {"percent": 12, "resetsAt": None, "resetInSec": 5000}}}
    config = provider_config(("go", GO, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "OpenCode Go\nrolling   12%  [█░░░░░░░░░]  resets in 1h23m"


async def test_a_string_zero_cap_keeps_the_window_and_the_report_alive(wires, tmp_path):
    # `"max": "0"` is truthy as given; the parsed zero ends the window, not the whole report.
    wires.answers[SYNTHETIC_QUOTAS] = {"rollingFiveHourLimit": {"max": "0", "remaining": "0"}, "weeklyTokenLimit": {"percentRemaining": 10}}
    config = provider_config(("synth", SYNTHETIC, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "Synthetic\n! weekly    90%  [█████████░]"


async def test_an_unusable_amount_never_prints_the_response_it_came_from(wires, tmp_path):
    wires.answers[DEEPSEEK_BALANCE] = {"balance_infos": [{"currency": "CNY", "total_balance": "n/a"}]}
    config = provider_config(("deep", DEEPSEEK, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "DeepSeek API\nerror: unparsable response"


async def test_an_all_zero_commandcode_balance_prints_no_empty_parts(wires, tmp_path):
    wires.answers[COMMANDCODE_WHOAMI] = {"user": {"login": "me"}}
    wires.answers[COMMANDCODE_CREDITS] = {"credits": {"monthlyCredits": 0, "purchasedCredits": 0, "freeCredits": 0}}
    config = provider_config(("cc", COMMANDCODE, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "Command Code\n! balance  $0.00  ok"


async def test_commandcode_used_over_the_cap_clamps_and_a_small_reset_counts_from_now(wires, tmp_path):
    wires.answers[COMMANDCODE_WHOAMI] = {"user": {"login": "me"}}
    wires.answers[COMMANDCODE_CREDITS] = {"windowLimits": {"weekly": {"used": 45, "cap": 35, "resetAt": 3600}}}
    config = provider_config(("cc", COMMANDCODE, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "Command Code\n! weekly   100%  [██████████]  resets in 1h"


async def test_a_config_beside_a_moved_data_directory_is_still_read(wires, tmp_path):
    # `--config` or a relocated `[paths] data_dir`: the config is not under data_dir. The command
    # follows the host's config_path, and the secrets are read beside the config.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    wires.answers[DEEPSEEK_BALANCE] = deepseek("110", "10", "100")
    (elsewhere / "config.toml").write_text(provider_config(("deep", DEEPSEEK, None)))
    (elsewhere / "secrets.toml").write_text('deep = "from-secrets"\n')
    result = plain(await usage_plugin.usage(context(tmp_path, str(elsewhere / "config.toml")), {}))
    assert result == "DeepSeek API\nbalance  ¥110.00  (granted ¥10.00 + topped up ¥100.00)  ok"
    assert wires.calls == [(DEEPSEEK_BALANCE, "from-secrets")]


async def test_a_missing_or_broken_config_says_what_to_do(wires, tmp_path):
    assert plain(await usage_plugin.usage(context(tmp_path), {})) == "No config.toml yet: nothing configured."
    (tmp_path / "config.toml").write_text('[provider.deep\nurl = "https://api.deepseek.com"\n')
    assert plain(await usage_plugin.usage(context(tmp_path), {})) == "config.toml is unreadable; fix it and try again."
    assert wires.calls == []


@pytest.mark.parametrize(("active", "secrets"), [('active = "work"\n', 'work = "from-secrets"\n'), ("", 'default = "from-secrets"\n')])
async def test_the_flat_provider_block_is_read_under_its_active_name(wires, tmp_path, active, secrets):
    # A config that still keeps its provider inline: the block takes the name `active` gives it,
    # and a secret under that name fills the key the block leaves out.
    wires.answers[DEEPSEEK_BALANCE] = deepseek("110", "10", "100")
    config = f'[provider]\n{active}url = "{DEEPSEEK}"\n'
    assert plain(await usage_plugin.usage(configured(tmp_path, config, secrets), {})) == "DeepSeek API\nbalance  ¥110.00  (granted ¥10.00 + topped up ¥100.00)  ok"
    assert wires.calls == [(DEEPSEEK_BALANCE, "from-secrets")]


async def test_a_failed_request_never_prints_the_key(monkeypatch, tmp_path):
    key = "sk-never-print-me"
    requests = []

    def refuse(request, timeout):
        requests.append(request)
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", email.message.Message(), None)

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    config = provider_config(("deep", DEEPSEEK, key))
    result = plain(await usage_plugin.usage(configured(tmp_path, config), {}))
    assert result == "DeepSeek API\nerror: HTTP 401"
    assert [request.headers["Authorization"] for request in requests] == [f"Bearer {key}"]  # The header is the only place it travels.
    assert key not in result


async def test_an_unreachable_provider_never_prints_the_key(monkeypatch, tmp_path):
    key = "sk-never-print-me"

    def refuse(request, timeout):
        raise urllib.error.URLError(OSError("getaddrinfo failed"))

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    config = provider_config(("synth", SYNTHETIC, key))
    result = plain(await usage_plugin.usage(configured(tmp_path, config), {}))
    assert result == "Synthetic\nerror: network (getaddrinfo failed)"
    assert key not in result


async def test_the_live_worker_reads_the_config_directory(runtime, tmp_path):
    (tmp_path / "config.toml").write_text(provider_config(("openai", "https://api.openai.com/v1", "k")))
    assert plain(await runtime.invoke("usage", "command", "usage", {})) == UNMATCHED


async def test_the_live_worker_skips_a_provider_without_a_key(runtime, tmp_path):
    (tmp_path / "config.toml").write_text(provider_config(("deep", DEEPSEEK, None)))
    assert plain(await runtime.invoke("usage", "command", "usage", {})) == "DeepSeek API\nskipped: no key configured"


# The real transport: a loopback server the test starts itself, with nothing monkeypatched.
#
# The command cannot be aimed at it: only KimiBalance reads the entry url, and only when its netloc
# contains `moonshot`, so pointing the handler here would take a host alias or a patched resolver.
# The HTTP layer is therefore exercised directly, on the body Kimi's own balance endpoint returns.

KIMI_BODY = {"code": 0, "data": {"available_balance": 49.58, "voucher_balance": 46.58, "cash_balance": 3.0}, "scode": "0x0", "status": True}


@contextmanager
def mock_provider(routes: dict[str, tuple[int, object]]):
    """A real HTTP server on an ephemeral loopback port, answering `routes` and recording requests."""
    received: list[tuple[str, dict[str, str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received.append((self.path, {name.lower(): value for name, value in self.headers.items()}))
            status, body = routes.get(self.path, (404, {"error": "no such path"}))
            payload: bytes = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *arguments: object) -> None:
            pass  # A served request is not test output.

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()


def test_get_json_against_a_real_server_sends_the_key_in_one_header():
    with mock_provider({"/v1/users/me/balance": (200, KIMI_BODY)}) as (base, received):
        assert usage_plugin.get_json(f"{base}/v1/users/me/balance", "fake-key") == KIMI_BODY
    [(path, headers)] = received
    assert path == "/v1/users/me/balance"
    assert headers["authorization"] == "Bearer fake-key"
    assert headers["accept"] == "application/json"
    assert headers["user-agent"] == "wizolt"  # The client's own name, not the HTTP library's.
    assert [name for name, value in headers.items() if "fake-key" in value] == ["authorization"]


def test_get_json_turns_a_real_rejection_and_a_refused_port_into_its_own_errors():
    with mock_provider({"/rejected": (401, {"error": "unauthorized"})}) as (base, _received), pytest.raises(usage_plugin.HttpError) as rejected:
        usage_plugin.get_json(f"{base}/rejected", "fake-key")
    assert rejected.value.code == 401
    with socket.socket() as probe:  # A port nothing listens on: refused, not answered.
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
    with pytest.raises(usage_plugin.Unreachable) as unreachable:
        usage_plugin.get_json(f"http://127.0.0.1:{closed}/v1/users/me/balance", "fake-key")
    assert "fake-key" not in str(unreachable.value)


async def test_an_entry_with_an_explicit_port_is_still_the_provider(wires, tmp_path):
    # A port in the url is spelling, not identity: the entry is still matched and asked.
    wires.answers[DEEPSEEK_BALANCE] = deepseek("110", "10", "100")
    config = provider_config(("deep", f"{DEEPSEEK}:443", "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {})) == "DeepSeek API\nbalance  ¥110.00  (granted ¥10.00 + topped up ¥100.00)  ok"
    assert wires.calls == [(DEEPSEEK_BALANCE, "k")]


async def test_streaming_prints_each_provider_block_as_it_answers(wires, tmp_path):
    # One frame per provider, the moment that provider answers; whichever answers last is
    # the command's own answer. A fast provider configured last used to be held back as the
    # answer, waiting out a slow one's timeout configured before it.
    wires.answers[DEEPSEEK_BALANCE] = deepseek("110", "10", "100")
    wires.answers[GO_USAGE] = usage_plugin.HttpError(503)
    wires.delays[GO_USAGE] = 0.2
    blocks: list[list[usage_plugin.Line]] = []
    finished = asyncio.Event()

    async def report(lines: list[usage_plugin.Line]) -> None:
        assert not finished.is_set()
        blocks.append(list(lines))

    config = provider_config(("go", GO, "k"), ("deep", DEEPSEEK, "k"))
    answer = await usage_plugin.usage(configured(tmp_path, config), {}, report)
    finished.set()
    assert blocks == [
        [Line((Text("DeepSeek API", "accent"),)), Line((Text("balance  ¥110.00  (granted ¥10.00 + topped up ¥100.00)  ", "text"), Text("ok", "muted")))],
    ]
    assert answer == [Line((Text("OpenCode Go", "accent"),)), Line((Text("error: HTTP 503", "error"),))]


async def test_a_host_that_cannot_show_blocks_answers_in_one_frame(wires, tmp_path):
    # No interactive UI (an offline trial): nothing streams, every provider answers in
    # one framed block, and the answer is the same one a plain report would give.
    wires.answers[DEEPSEEK_BALANCE] = deepseek("110", "10", "100")
    wires.answers[GO_USAGE] = usage_plugin.HttpError(503)

    async def refuse(lines: list[usage_plugin.Line]) -> None:
        raise usage_plugin.PluginError("Interactive UI is unavailable")

    config = provider_config(("deep", DEEPSEEK, "k"), ("go", GO, "k"))
    assert plain(await usage_plugin.usage(configured(tmp_path, config), {}, refuse)) == sections(
        "OpenCode Go\nerror: HTTP 503",
        "DeepSeek API\nbalance  ¥110.00  (granted ¥10.00 + topped up ¥100.00)  ok",
    )


async def test_a_channel_that_fails_midway_answers_the_rest_in_one_frame(wires, tmp_path):
    # One provider's block streamed, then the channel failed: nothing is lost or doubled
    # -- the blocks that did not print answer in the command's own frame.
    wires.answers[DEEPSEEK_BALANCE] = deepseek("110", "10", "100")
    wires.answers[SYNTHETIC_QUOTAS] = {"weeklyTokenLimit": {"percentRemaining": 66, "nextRegenAt": "2023-11-14T23:00:00+00:00"}}
    wires.answers[GO_USAGE] = usage_plugin.HttpError(503)
    wires.delays[GO_USAGE] = 0.02  # Go answers last, so DeepSeek and Synthetic stream first.
    blocks: list[list[usage_plugin.Line]] = []

    async def report(lines: list[usage_plugin.Line]) -> None:
        if len(blocks) == 1:
            raise usage_plugin.PluginError("Interactive UI is unavailable")
        blocks.append(list(lines))

    config = provider_config(("deep", DEEPSEEK, "k"), ("go", GO, "k"), ("synth", SYNTHETIC, "k"))
    answer = await usage_plugin.usage(configured(tmp_path, config), {}, report)
    assert plain(answer) == sections(
        "OpenCode Go\nerror: HTTP 503",
        "Synthetic\nweekly    34%  [███░░░░░░░]  resets in 46m",
    )


async def test_report_rows_carry_theme_roles(wires, tmp_path):
    # The report answer is styled rows -- accent titles, error notes in the error role,
    # threshold rows in warning -- and the host, never this plugin, turns roles into colors.
    wires.answers[DEEPSEEK_BALANCE] = deepseek("0", "0", "0", available=False)
    wires.answers[SYNTHETIC_QUOTAS] = {"unexpected": "shape"}
    config = provider_config(("deep", DEEPSEEK, "k"), ("synth", SYNTHETIC, "k"))
    result = await usage_plugin.usage(configured(tmp_path, config), {})
    assert result == [
        Line((Text("DeepSeek API", "accent"),)),
        Line((Text("! ", "warning"), Text("balance  ¥0.00  (granted ¥0.00 + topped up ¥0.00)  ", "error"), Text("insufficient", "error"))),
        Line(),
        Line((Text("Synthetic", "accent"),)),
        Line((Text("error: no quota data", "error"),)),
    ]


def test_the_body_a_real_server_returns_renders_as_the_balance_line():
    """The Kimi path in two honest pieces: the body comes off a real socket, then the source reads
    it and the shared renderer prints the line a user sees."""
    with mock_provider({"/v1/users/me/balance": (200, KIMI_BODY)}) as (base, _received):
        body = usage_plugin.get_json(f"{base}/v1/users/me/balance", "fake-key")
    rows = usage_plugin.KimiBalance().rows({"answer": body, "currency": "CNY"}, NOW)
    assert plain(usage_plugin.section("Kimi", usage_plugin.Report("Kimi", rows=rows))) == "Kimi\nbalance  ¥49.58  (cash ¥3.00 + voucher ¥46.58)  ok"
