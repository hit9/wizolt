"""Shared TuiRuntime test helpers; the behavior tests live in the test_tui_runtime_* modules."""


def history_file(path, entries, line="x" * 200):
    """Write a prompt_toolkit history file with `entries` numbered entries."""
    with open(path, "wb") as file:
        file.writelines(f"\n# 2026-01-01 00:00:{index:02d}\n+{index}-{line}\n".encode() for index in range(entries))
    return path
