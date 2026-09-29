"""The entry point stays light before it dispatches an interactive session."""

import subprocess
import sys

import pytest


@pytest.mark.parametrize("url,api,expected", [
    ("https://example.test/v1", "chat", "openai"),
    ("https://example.test/v1", "responses", "openai"),
    ("https://example.test/v1", "anthropic", "anthropic"),
    ("https://example.test/v1/messages", "auto", "anthropic"),
])
def test_startup_prewarms_only_the_resolved_protocol(tmp_path, url, api, expected):
    from wizolt.__main__ import startup_imports
    from wizolt.config import Config, ProviderConfig
    from wizolt.session import Session

    config = Config(data_dir=str(tmp_path), providers={"default": ProviderConfig(url=url, api=api)})
    assert startup_imports(Session(config=config, cwd=str(tmp_path))) == [expected, "wizolt.ui.markdown"]


@pytest.mark.parametrize("route", ["compaction", "vision", "worker"])
def test_startup_includes_configured_auxiliary_protocols(tmp_path, route):
    from wizolt.__main__ import startup_imports
    from wizolt.config import Config, ProviderConfig
    from wizolt.session import Session

    config = Config(data_dir=str(tmp_path), providers={
        "default": ProviderConfig(api="chat"), "other": ProviderConfig(api="anthropic"),
        "unused": ProviderConfig(api="responses"),
    })
    setattr(config, route + "_provider", "other")
    assert startup_imports(Session(config=config, cwd=str(tmp_path))) == ["openai", "anthropic", "wizolt.ui.markdown"]


def test_startup_resolves_compaction_override_and_mcp_autoconnect(tmp_path):
    from types import SimpleNamespace

    from wizolt.__main__ import startup_imports
    from wizolt.config import Config
    from wizolt.session import Session

    config = Config(data_dir=str(tmp_path), compaction_api="anthropic")
    session = Session(config=config, cwd=str(tmp_path))
    session.mcp = SimpleNamespace(parse_configs=lambda: [SimpleNamespace(auto_connect=False)])
    assert startup_imports(session) == ["openai", "anthropic", "wizolt.ui.markdown"]
    session.mcp = SimpleNamespace(parse_configs=lambda: [SimpleNamespace(auto_connect=True)])
    assert startup_imports(session) == ["openai", "anthropic", "mcp.client", "wizolt.ui.markdown"]


def test_fresh_interpreter_import_chain_stays_light():
    """Heavy UI and provider dependencies do not return to the entry import path."""
    probe = (
        "import sys;"
        "import wizolt.base;"
        "import wizolt.__main__;"
        "assert 'prompt_toolkit' not in sys.modules, 'base must not import prompt_toolkit';"
        "heavy = {'anthropic', 'openai', 'mcp'} & set(sys.modules);"
        "assert not heavy, heavy"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True)


def test_cli_import_chain_defers_request_path_stacks():
    """The interactive CLI's import path carries no HTTP client or image decoder.

    Those first run on the request path -- a background version probe, an admitted image -- where
    their import cost hides behind a turn instead of delaying the prompt."""
    probe = "import sys;import wizolt.ui.cli;heavy = {'httpx2', 'PIL'} & set(sys.modules);assert not heavy, heavy"
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True)


@pytest.mark.parametrize("module", ["wizolt.agent.hooks", "wizolt.ui", "wizolt.model.protocol", "wizolt.session"])
def test_lower_layer_imports_do_not_assemble_an_application(module):
    probe = (
        "import importlib, sys;"
        f"importlib.import_module({module!r});"
        "forbidden = {'wizolt.agent.lifecycle', 'wizolt.agent.engine', 'wizolt.ui.cli', "
        "'wizolt.ui.tui', 'openai', 'anthropic', 'mcp', 'httpx2'} & set(sys.modules);"
        "assert not forbidden, forbidden"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True)
