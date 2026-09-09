"""The entry point stays light before it dispatches an interactive session."""

import subprocess
import sys


def test_fresh_interpreter_import_chain_stays_light():
    """Heavy UI, provider, and indexing dependencies do not return to the entry import path."""
    probe = (
        "import sys;"
        "import wizolt.base;"
        "import wizolt.__main__;"
        "import wizolt.tools.search;"
        "assert 'prompt_toolkit' not in sys.modules, 'base must not import prompt_toolkit';"
        "heavy = {'anthropic', 'openai', 'fastmcp', 'code_symbol_index'} & set(sys.modules);"
        "assert not heavy, heavy"
    )
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True)


def test_cli_import_chain_defers_request_path_stacks():
    """The interactive CLI's import path carries no HTTP client, image decoder, or JSON repair.

    Those first run on the request path -- a background probe, an admitted image, a malformed
    model payload -- where their import cost hides behind a turn instead of delaying the prompt."""
    probe = "import sys;import wizolt.cli;heavy = {'httpx2', 'PIL', 'json_repair'} & set(sys.modules);assert not heavy, heavy"
    subprocess.run([sys.executable, "-c", probe], check=True, capture_output=True)
