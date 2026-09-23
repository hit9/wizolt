"""Shared CommandLoop test helpers; the behavior tests live in the test_loop_* modules."""

import os


def _write_skill(root, name, description, body, *, scripts=None):
    folder = os.path.join(root, ".wizolt", "skills", name)
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "SKILL.md"), "w", encoding="utf-8") as handle:
        handle.write(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n")
    for script_name, script_body in (scripts or {}).items():
        script_dir = os.path.join(folder, "scripts")
        os.makedirs(script_dir, exist_ok=True)
        with open(os.path.join(script_dir, script_name), "w", encoding="utf-8") as handle:
            handle.write(script_body)
    return folder


def queued_texts(s):
    return [item.text for item in s.pending_user_inputs]
