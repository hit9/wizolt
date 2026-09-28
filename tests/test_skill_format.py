"""SKILL.md parsing: YAML frontmatter in the Agent Skills format, as other agents write it."""

from agent_harness import session

from wizolt.agent.engine import Agent
from wizolt.skill.trust import ProjectTrust
from wizolt.tools import SkillTool
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli.commands import skills_command


def _skill_file(root, folder, text):
    path = root / ".wizolt" / "skills" / folder
    path.mkdir(parents=True, exist_ok=True)
    (path / "SKILL.md").write_text(text, encoding="utf-8")
    return path


def _skills_output(tmp_path):
    loop = CommandLoop(Agent(session(tmp_path), output_fn=lambda _text: None), output_fn=lambda _text: None)
    return skills_command(loop, "")


def test_folded_description_and_spec_fields_load(tmp_path):
    # A spec skill: multi-line description, optional fields, nested metadata map. The line parser
    # this replaced read `description: >-` as the literal text ">-" and dropped the rest.
    _skill_file(
        tmp_path,
        "pdf-extract",
        "---\n"
        "name: pdf-extract\n"
        "description: >-\n"
        "  Extract tables and text\n"
        "  from PDF files.\n"
        "license: Apache-2.0\n"
        "compatibility: Requires poppler\n"
        "metadata:\n"
        "  author: someone\n"
        "  version: '1.0'\n"
        "allowed-tools: Bash(pdftotext:*) Read\n"
        "---\n"
        "# PDF\n\nRun pdftotext.\n",
    )
    # allowed-tools makes it a skill that acts on its own: a project one waits for trust.
    ProjectTrust(str(tmp_path / "data" / ProjectTrust.FILE_NAME), str(tmp_path)).grant()
    skill = session(tmp_path).skills.get("pdf-extract")

    assert skill is not None
    assert skill.description == "Extract tables and text from PDF files."
    assert skill.body == "# PDF\n\nRun pdftotext."
    assert [str(rule) for rule in skill.allowed_tools] == ["Bash(pdftotext:*)", "Read"]
    assert skill.warnings == ()


def test_quoted_scalars_and_crlf_still_parse(tmp_path):
    _skill_file(tmp_path, "guide", "﻿---\r\nname: \"guide\"\r\ndescription: 'A: quoted guide'\r\n---\r\nbody\r\n")
    skill = session(tmp_path).skills.get("guide")

    assert skill is not None
    assert skill.description == "A: quoted guide"
    assert skill.body == "body"


def test_frontmatter_without_body_is_a_skill(tmp_path):
    _skill_file(tmp_path, "empty", "---\nname: empty\ndescription: nothing inside\n---")
    skill = session(tmp_path).skills.get("empty")

    assert skill is not None
    assert skill.body == ""


def test_invalid_yaml_is_refused_and_reported(tmp_path):
    _skill_file(tmp_path, "broken", "---\nname: broken\ndescription: [unclosed\n---\nbody\n")
    _skill_file(tmp_path, "listy", "---\n- just\n- a list\n---\nbody\n")
    _skill_file(tmp_path, "fine", "---\nname: fine\ndescription: works\n---\nbody\n")
    s = session(tmp_path)

    assert s.skills.get("broken") is None
    assert s.skills.get("listy") is None
    assert s.skills.get("fine") is not None
    output = _skills_output(tmp_path)
    assert "#### Not loaded" in output
    assert "broken/SKILL.md: invalid YAML frontmatter" in output
    assert "listy/SKILL.md: frontmatter is not a key/value map" in output


def test_spec_deviations_load_with_warnings(tmp_path):
    _skill_file(tmp_path, "release", "---\nname: Release_Notes\n---\nbody\n")
    skill = session(tmp_path).skills.get("Release_Notes")

    assert skill is not None  # still usable: a warning, not a refusal
    output = _skills_output(tmp_path)
    assert "#### Warnings" in output
    assert "`Release_Notes`: name 'Release_Notes' is not 1-64 lowercase letters" in output
    assert "`Release_Notes`: name 'Release_Notes' does not match its folder 'release'" in output
    assert "`Release_Notes`: no description" in output


def test_no_frontmatter_takes_the_folder_name(tmp_path):
    _skill_file(tmp_path, "plain", "Just instructions.\n")
    skill = session(tmp_path).skills.get("plain")

    assert skill is not None
    assert skill.body == "Just instructions."


async def test_claude_skill_dir_placeholder_expands(tmp_path):
    folder = _skill_file(tmp_path, "build", "---\nname: build\ndescription: b\n---\nRun ${CLAUDE_SKILL_DIR}/go.sh and {skill_dir}/x\n")
    output = await SkillTool(session(tmp_path), ["build"]).call()

    assert f"Run {folder}/go.sh and {folder}/x" in output
    assert "CLAUDE_SKILL_DIR" not in output
