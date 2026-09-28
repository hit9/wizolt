"""Bound shell-hook and skill command output while the process is still running."""

import shlex
import sys
import tracemalloc

from wizolt.utils.process import ShellCommand


async def test_large_output_is_drained_without_retaining_it(tmp_path):
    # Eight MiB would exceed this budget if communicate() buffered before truncating.
    tracemalloc.start()
    try:
        result = await ShellCommand("head -c 8388608 /dev/zero", str(tmp_path), 10).run(max_output=1024)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result.exit_code == 0
    assert result.stdout == "\0" * 1024 + "\n... (8387584 more characters cut)"
    assert peak < 3_000_000


async def test_unicode_and_stderr_are_bounded_in_characters(tmp_path):
    code = 'import sys; sys.stdout.write("界" * 20000); sys.stderr.write("错" * 30000)'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    result = await ShellCommand(command, str(tmp_path), 5).run(max_output=11)
    assert result.exit_code == 0
    assert result.stdout == "界" * 11 + "\n... (19989 more characters cut)"
    assert result.stderr == "错" * 11 + "\n... (29989 more characters cut)"


async def test_timeout_covers_process_after_output_pipes_close(tmp_path):
    result = await ShellCommand("exec >/dev/null 2>&1; sleep 30", str(tmp_path), 0.1).run()
    assert result.timed_out
