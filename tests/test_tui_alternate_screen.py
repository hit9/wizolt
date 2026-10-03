"""The alternate-screen probe one real tmux tells the truth about.

`TuiApp.alternate_screen_available` decides whether a full-screen viewer may take over the
primary screen; getting it wrong on a `set -wg` tmux.conf eats the transcript.
"""

import asyncio
import shlex
import shutil
import subprocess
import sys
import time

import pytest


@pytest.mark.tmux
async def test_alternate_screen_probe_reads_the_resolved_window_option(tmp_path):
    """alternate-screen is a window option, so `show-options` reports it only where a window
    overrides it and stays silent for the usual global `set -wg` form in a tmux.conf. The probe
    has to answer for both, or a full-screen viewer takes over the primary screen and eats the
    transcript."""
    executable = shutil.which("tmux")
    if executable is None:
        pytest.skip("requires tmux")
    socket = "wizolt-test-probe-" + tmp_path.name
    command = [executable, "-L", socket]
    probe = tmp_path / "alternate_screen_probe.py"
    probe.write_text(
        f"""import asyncio
import subprocess

from wizolt.ui.tui import TuiApp

print(asyncio.run(TuiApp.alternate_screen_available()))
subprocess.run([{executable!r}, "set-option", "-wg", "alternate-screen", "off"], check=True)
print(asyncio.run(TuiApp.alternate_screen_available()))
subprocess.run([{executable!r}, "set-option", "-wg", "alternate-screen", "on"], check=True)
print(asyncio.run(TuiApp.alternate_screen_available()))
"""
    )
    run = f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))} > {shlex.quote(str(tmp_path / 'out'))} 2>&1"

    def probe_values():
        subprocess.run([*command, "new-window", "-d", "-t", "holder", run], check=True, capture_output=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            values = (tmp_path / "out").read_text().splitlines() if (tmp_path / "out").exists() else []
            if len(values) == 3:
                return values
            time.sleep(0.01)
        return []

    try:
        await asyncio.to_thread(subprocess.run, [*command, "new-session", "-d", "-s", "holder", "sleep 60"], check=True, capture_output=True)
        # The global window-option form is invisible to `show-options` without -g; all three
        # observations happen in one tmux client process so process startup does not dominate.
        assert probe_values() == ["True", "False", "True"]
    finally:
        await asyncio.to_thread(subprocess.run, [*command, "kill-server"], check=False, capture_output=True)
