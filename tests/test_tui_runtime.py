"""TuiRuntime behavior: command dispatch, the follow-up queue, streamed response promotion,
resume, and session housekeeping at startup."""


def history_file(path, entries, line="x" * 200):
    """Write a prompt_toolkit history file with `entries` numbered entries."""
    with open(path, "wb") as file:
        file.writelines(f"\n# 2026-01-01 00:00:{index:02d}\n+{index}-{line}\n".encode() for index in range(entries))
    return path
