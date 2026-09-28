"""The SKILLS block the model sees: bounded, labeled, and without skills reserved for the user."""

import pytest
from agent_harness import session

from wizolt.agent.context import ContextManager
from wizolt.base import ToolError
from wizolt.skill import SkillLibrary
from wizolt.tools import SkillTool, Tool


def _skill(root, name, frontmatter="", body="body"):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(f"---\nname: {name}\n{frontmatter}---\n{body}\n", encoding="utf-8")


def _tool_names(s):
    return {schema["function"]["name"] for schema in Tool.resolved_schemas(s)}


def test_rows_carry_source_and_argument_hint(tmp_path, isolate_home):
    _skill(tmp_path / ".wizolt" / "skills", "deploy", "description: Deploy the service.\nargument-hint: <env>\n")
    _skill(isolate_home / ".claude" / "skills", "pdf", "description: Read PDFs.\n")
    index = session(tmp_path).skills.index()

    assert "- deploy [project] (args: <env>): Deploy the service." in index
    assert "- pdf [user]: Read PDFs." in index


async def test_user_only_skill_is_out_of_the_index_and_refused_to_the_model(tmp_path):
    _skill(tmp_path / ".wizolt" / "skills", "deploy", "description: Ship it.\ndisable-model-invocation: true\n")
    _skill(tmp_path / ".wizolt" / "skills", "guide", "description: Conventions.\n")
    s = session(tmp_path)

    assert "deploy" not in s.skills.index()
    assert "- guide [project]: Conventions." in s.skills.index()
    with pytest.raises(ToolError, match="only be started by the user, with /deploy"):
        await SkillTool(s, ["deploy"]).call()
    # A mention of it tells the model why, instead of inviting a refused call.
    assert "(only the user can start this one, with /deploy)" in s.skills.resolve_mentions("run $deploy")


def test_only_user_only_skills_offer_no_skill_tool(tmp_path):
    _skill(tmp_path / ".wizolt" / "skills", "deploy", "description: Ship it.\ndisable-model-invocation: true\n")
    s = session(tmp_path)

    assert s.skills.index() == ""
    assert "Skill" not in _tool_names(s)
    assert all("--- SKILLS ---" not in str(message["content"]) for message in ContextManager(s).model_messages("system"))


def test_invalid_boolean_warns_and_keeps_the_default(tmp_path):
    _skill(tmp_path / ".wizolt" / "skills", "odd", "description: d\ndisable-model-invocation: sometimes\n")
    skill = session(tmp_path).skills.get("odd")

    assert skill.model_invocable
    assert "disable-model-invocation must be true or false; using false" in skill.warnings


def test_long_descriptions_are_cut_in_the_index_only(tmp_path):
    long = "word " * 200
    _skill(tmp_path / ".wizolt" / "skills", "wordy", f"description: {long}\n")
    s = session(tmp_path)
    row = next(line for line in s.skills.index().splitlines() if line.startswith("- wordy"))

    assert len(row) < SkillLibrary.INDEX_DESCRIPTION_CHARS + 40
    assert row.endswith("…")
    assert s.skills.get("wordy").description == long.strip()  # /skills still has all of it


def test_index_budget_keeps_project_skills_first_and_counts_the_rest(tmp_path, isolate_home, monkeypatch):
    monkeypatch.setattr(SkillLibrary, "INDEX_BUDGET_CHARS", 200)
    for number in range(5):
        _skill(isolate_home / ".claude" / "skills", f"user-{number}", "description: a user skill of moderate length\n")
    _skill(tmp_path / ".wizolt" / "skills", "zz-project", "description: the project's own\n")
    index = session(tmp_path).skills.index()

    rows = [line for line in index.splitlines() if line.startswith("- ")]
    assert "- zz-project [project]: the project's own" in rows  # sorted last by name, kept first by source
    assert sum(len(row) + 1 for row in rows) <= 200
    listed_users = sum(row.startswith("- user-") for row in rows)
    assert f"({5 - listed_users} more skills are installed but not listed here" in index
    assert listed_users < 5
