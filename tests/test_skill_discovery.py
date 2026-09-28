"""Where skills are found: user and project roots shared with other agents, and their precedence."""

import os

from agent_harness import session
from prompt_toolkit.document import Document

from wizolt.agent.engine import Agent
from wizolt.skill import SkillLibrary
from wizolt.skill.discovery import SkillDiscovery
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli.commands import skills_command
from wizolt.utils.workspace import Workspace


def _skill(root, name, description):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\nbody of {description}\n", encoding="utf-8")


def _skills_output(s):
    return skills_command(CommandLoop(Agent(s, output_fn=lambda _text: None), output_fn=lambda _text: None), "")


def test_project_folders_of_other_agents_are_merged(tmp_path):
    # Previously only the first existing of .wizolt/.minacode/.nanocode was read at cwd.
    _skill(tmp_path / ".claude" / "skills", "from-claude", "claude")
    _skill(tmp_path / ".agents" / "skills", "from-agents", "agents")
    _skill(tmp_path / ".wizolt" / "skills", "from-wizolt", "wizolt")
    skills = session(tmp_path).skills

    assert {skill.name for skill in skills.all()} == {"from-claude", "from-agents", "from-wizolt"}
    assert skills.get("from-claude").location == ".claude/skills/from-claude"
    assert all(skill.source == "project" for skill in skills.all())


def test_user_folders_of_other_agents_are_read(tmp_path, isolate_home):
    _skill(isolate_home / ".claude" / "skills", "home-claude", "c")
    _skill(isolate_home / ".agents" / "skills", "home-agents", "a")
    _skill(tmp_path / "data" / "skills", "home-wizolt", "w")
    skills = session(tmp_path).skills

    assert {skill.name for skill in skills.all()} == {"home-claude", "home-agents", "home-wizolt"}
    assert skills.get("home-claude").source == "user"
    assert skills.get("home-claude").location == "~/.claude/skills/home-claude"


def test_precedence_wizolt_over_agents_over_claude_over_user(tmp_path, isolate_home):
    _skill(isolate_home / ".claude" / "skills", "guide", "user")
    s = session(tmp_path)
    assert s.skills.get("guide").description == "user"

    _skill(tmp_path / ".claude" / "skills", "guide", "claude")
    assert session(tmp_path).skills.get("guide").description == "claude"
    _skill(tmp_path / ".agents" / "skills", "guide", "agents")
    assert session(tmp_path).skills.get("guide").description == "agents"
    _skill(tmp_path / ".wizolt" / "skills", "guide", "wizolt")
    winner = session(tmp_path).skills.get("guide")
    assert winner.description == "wizolt"
    assert winner.overrides == ("~/.claude/skills/guide", ".claude/skills/guide", ".agents/skills/guide")

    output = _skills_output(session(tmp_path))
    assert "#### Overridden" in output
    assert "- `guide` hides `~/.claude/skills/guide`, `.claude/skills/guide`, `.agents/skills/guide`" in output


def test_repository_levels_from_top_down_to_cwd(tmp_path):
    # Starting in a subdirectory used to miss the repository's own skills entirely.
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _skill(repo / ".wizolt" / "skills", "top", "repo top")
    _skill(repo / ".wizolt" / "skills", "shared", "from top")
    _skill(repo / "packages" / "web" / ".claude" / "skills", "shared", "from web")
    _skill(repo / "packages" / "api" / ".claude" / "skills", "api-only", "sibling")
    cwd = repo / "packages" / "web"
    skills = session(cwd).skills

    assert skills.get("top").location == ".wizolt/skills/top"
    # The deeper level is more specific, even though `.claude` is weaker than `.wizolt` at one level.
    assert skills.get("shared").description == "from web"
    assert skills.get("shared").location == "packages/web/.claude/skills/shared"
    assert skills.get("api-only") is None  # a sibling of cwd is not on the path


def test_no_upward_walk_outside_a_repository(tmp_path):
    _skill(tmp_path / ".wizolt" / "skills", "parent", "above cwd")
    cwd = tmp_path / "work"
    cwd.mkdir()

    assert session(cwd).skills.get("parent") is None


def test_git_worktree_file_marks_the_top(tmp_path):
    repo = tmp_path / "wt"
    (repo / "sub").mkdir(parents=True)
    (repo / ".git").write_text("gitdir: /elsewhere\n", encoding="utf-8")
    _skill(repo / ".agents" / "skills", "wt-skill", "worktree")

    assert session(repo / "sub").skills.get("wt-skill") is not None


def test_the_same_directory_reached_twice_counts_once(tmp_path, isolate_home):
    # Running in the home directory: ~/.claude/skills is both a user and a project root.
    _skill(isolate_home / ".claude" / "skills", "home", "h")
    skills = SkillLibrary(*SkillDiscovery(Workspace(str(isolate_home)), str(tmp_path / "data" / "skills")).scan())

    assert skills.get("home").source == "user"
    assert skills.get("home").overrides == ()


def test_mention_menu_labels_each_skill_with_its_source(tmp_path, isolate_home):
    _skill(isolate_home / ".claude" / "skills", "mine", "user skill")
    _skill(tmp_path / ".wizolt" / "skills", "theirs", "project skill")
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda _text: None), output_fn=lambda _text: None)

    def menu(text):
        return {row.text: row.display_meta_text for row in loop.input_completer.get_completions(Document(text), None)}

    assert menu("@skill:") == {"@skill:mine": "user", "@skill:theirs": "project"}
    assert menu("$") == {"@skill:mine": "user", "@skill:theirs": "project"}
    assert menu("@the")["@skill:theirs"] == "skill · project"


def test_unlistable_root_is_reported(tmp_path):
    root = tmp_path / ".wizolt" / "skills"
    root.mkdir(parents=True)
    os.chmod(root, 0)
    try:
        if os.access(root, os.R_OK):  # running as root: permissions do not apply
            return
        output = _skills_output(session(tmp_path))
    finally:
        os.chmod(root, 0o755)
    assert "#### Not loaded" in output
    assert ".wizolt/skills: cannot list" in output
