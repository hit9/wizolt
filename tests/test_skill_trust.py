"""A repository must be trusted before its own skills may run commands."""

import json

from agent_harness import session

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features
from wizolt.config import Config
from wizolt.session import Session
from wizolt.skill.listing import SkillListing
from wizolt.skill.trust import ProjectTrust
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli.commands import skills_command


def _skill(root, name, body):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name} skill\n---\n{body}\n", encoding="utf-8")


def _loop(s):
    return CommandLoop(Agent(s, output_fn=lambda _text: None), output_fn=lambda _text: None)


def test_untrusted_project_skill_that_runs_commands_is_held_back(tmp_path, isolate_home):
    _skill(tmp_path / ".wizolt" / "skills", "status", "Branch: !`git branch --show-current`")
    _skill(tmp_path / ".wizolt" / "skills", "guide", "Plain instructions.")
    _skill(isolate_home / ".claude" / "skills", "mine", "Mine: !`date`")
    s = session(tmp_path)

    # Nothing that looks skills up can reach it: index, lookup, mentions.
    assert s.skills.get("status") is None
    assert "- status [" not in s.skills.index()
    assert SkillListing.of(s, s.skills).resolve_mentions(s.skills, "$status") == ""
    # Instructions alone, and the user's own skills, need no trust.
    assert s.skills.get("guide") is not None
    assert s.skills.get("mine") is not None

    output = skills_command(_loop(s), "")
    assert "#### Untrusted" in output
    assert "- `status` (`.wizolt/skills/status`)" in output


def test_trust_enables_and_untrust_disables_in_place(tmp_path):
    _skill(tmp_path / ".wizolt" / "skills", "status", "Branch: !`echo main`")
    s = session(tmp_path)
    library = s.skills
    loop = _loop(s)

    assert skills_command(loop, "trust").endswith("Enabled: `status`.")
    assert s.skills is library  # reloaded in place: a worker sharing it sees the change too
    assert library.get("status") is not None
    assert session(tmp_path).skills.get("status") is not None  # remembered for the next session

    assert skills_command(loop, "untrust").endswith("Disabled: `status`.")
    assert library.get("status") is None
    assert session(tmp_path).skills.get("status") is None


def test_trust_belongs_to_the_repository_not_the_directory(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _skill(repo / "pkg" / ".claude" / "skills", "nested", "!`echo nested`")
    (repo / "other").mkdir()
    data = tmp_path / "data"
    skills_command(_loop(_session_in(repo / "other", data)), "trust")

    assert _session_in(repo / "pkg", data).skills.get("nested") is not None


def _session_in(cwd, data_dir):
    config = Config()
    config.data_dir = str(data_dir)
    s = Session(cwd=str(cwd), config=config)
    bootstrap_features(s)
    # The test reads the scanned index; interactive startup scans during the "starting" settle.
    s.skills.reload()
    return s


def test_trust_file_is_per_user_and_tolerates_damage(tmp_path):
    store = tmp_path / "data" / ProjectTrust.FILE_NAME
    store.parent.mkdir(parents=True)
    store.write_text("{not json", encoding="utf-8")
    trust = ProjectTrust(str(store), str(tmp_path))

    assert not trust.granted()  # damaged: trusts nothing
    trust.grant()
    assert json.loads(store.read_text(encoding="utf-8")) == {"projects": [str(tmp_path.resolve())]}


async def test_trusting_is_refused_while_the_agent_works(tmp_path):
    loop = _loop(session(tmp_path))
    shown = []
    loop.presentation.emit_turn = shown.append

    await loop.run_queued_command("/skills trust")

    assert shown == ["Only /skills (list) is available while the agent is working."]


def test_an_unwritable_trust_store_is_reported(tmp_path):
    import os

    s = session(tmp_path)
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    os.chmod(data, 0o500)
    try:
        if os.access(data, os.W_OK):  # running as root: permissions do not apply
            return
        output = skills_command(_loop(s), "trust")
    finally:
        os.chmod(data, 0o755)

    assert output.startswith("Error: could not record the decision in")
    assert s.skills.trust is not None and not s.skills.trust.granted()


def test_unknown_subcommand_prints_usage(tmp_path):
    assert skills_command(_loop(session(tmp_path)), "nope") == "Usage: /skills [list|reload|trust|untrust]"
