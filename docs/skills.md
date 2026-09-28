# Skills

A **skill** is a reusable pack of instructions the agent loads only when it's needed —
a release checklist, a project convention, a multi-step procedure. Keeping the
<span class="marker">full text out of context until it's used</span> means you can have many
skills without bloating every request.

wizolt reads the [Agent Skills](https://agentskills.io/specification) format and the extra
fields Claude Code uses, so a skill you already have for another agent works here unchanged.

## Creating and installing skills

### What a skill looks like

A skill is a folder containing a `SKILL.md` file with YAML frontmatter:

```
.wizolt/skills/
  release-notes/
    SKILL.md
    generate.py        # optional bundled script
```

```markdown
---
name: release-notes
description: Draft release notes from the git log since the last tag.
---

1. Run `git log $(git describe --tags --abbrev=0)..HEAD --oneline`.
2. Group the commits by type and summarize each group.
3. If a bundled script is needed, run it with Bash — see paths below.
```

Until the skill is used, the agent sees only a one-line entry with its name and
description (cut to 250 characters). All entries together are capped at about 4K tokens, with
project skills listed first. The full body loads when the skill is used.

`/skills` lists any skill that does not follow the spec (a name that isn't lowercase with
hyphens, a missing description) under **Warnings**; it still loads. A skill whose frontmatter
is not valid YAML is listed under **Not loaded** with the reason.

### Where skills come from

wizolt looks where other agents keep skills too, so one install serves all of them:

- **Project** — `.claude/skills/`, `.agents/skills/` and `.wizolt/skills/`, in the working
  directory and every folder above it up to the repository's top. Outside a git repository,
  only the working directory is searched.
- **Packages** — the same folders inside a subfolder the agent reads or edits a file in, such
  as `packages/web/.claude/skills/` in a monorepo. They are picked up from the next turn on.
- **User** — `~/.claude/skills/`, `~/.agents/skills/` and `~/.wizolt/skills/` (under
  `<data_dir>/skills/` when `paths.data_dir` is customized).

When two skills share a name, the one nearer your working directory wins; in the same folder,
`.wizolt` beats `.agents`, which beats `.claude`; any project skill beats a user skill. A
package's skill only adds to these, and never replaces one of the same name. `/skills` shows
where each skill came from, whether it is a project or user skill, and which same-named skills
it hides.

Skills you add or change during a session reach the agent at the start of your next turn;
`/skills reload` rescans right away and says what changed.

### Trusting a repository

A repository you clone decides what is in its skill folders. A project skill that only holds
instructions loads right away, like the repository's AGENTS.md. A project skill that **acts on
its own** — it runs [commands when it loads](#commands-in-a-skill), carries
[hooks](#hooks-and-pre-approved-tools), or pre-approves tools — stays off until you run
`/skills trust` in that repository. Until then `/skills` lists it under **Untrusted**, and
neither you nor the agent can start it.

`/skills untrust` takes the decision back. Trust is stored for you, in
`<data_dir>/trusted-projects.json`, never in the repository. Your own user-level skills need
no trust.

## Using skills

- **On demand** — the agent loads a skill itself when it fits your request.
- **`/name`** — type `/release-notes` to start a skill yourself; its instructions go to the
  agent with your message. Words after the name are the skill's
  [arguments](#arguments). Skills appear in the `/` menu after the built-in commands; a
  built-in command keeps its name if a skill shares it.
- **`$name`** — mention a skill in a message (Tab-completes) to point the agent at it
  <span class="marker">for that turn</span>; it loads the instructions itself when they matter.
  The menu shows whether each skill is a project or a user skill.

```{figure} ../snapshots/wizolt-skill-mention.png
:alt: Using $skill mention to point the agent at a skill
:width: 600px
:align: center

Pointing the agent at a skill with $name.
```

Two frontmatter fields decide who can start a skill:

| Field | Effect |
|---|---|
| `disable-model-invocation: true` | Only you can start it, with `/name`. Use it for something like a deploy you want to trigger yourself. |
| `user-invocable: false` | It is not in the `/` menu; only the agent loads it. Use it for background knowledge. |

## Writing richer skills

### Bundled scripts

A skill can ship helper scripts alongside `SKILL.md`. Inside the body, `{skill_dir}`,
`${SKILL_DIR}` or `${CLAUDE_SKILL_DIR}` expands to the skill's absolute path, so instructions can
point the agent at a script to run through Bash:

```markdown
Run the generator: `python {skill_dir}/generate.py`
```

### Arguments

`/deploy staging eu` passes `staging eu` to the skill; the agent can pass arguments too.
Give the expected shape in `argument-hint` (`argument-hint: <env> [region]`); it shows in the
`/` menu and in the agent's list of skills.

| In the body | Becomes |
|---|---|
| `$ARGUMENTS` | All the arguments, as typed |
| `$0`, `$1`, … or `$ARGUMENTS[0]`, … | One argument, counting from 0; quoted words count as one |

A skill with no placeholder receives the arguments on a final `ARGUMENTS:` line. `$0` and
friends are left alone when there are no arguments, so a script line like `awk '{print $1}'`
keeps working.

### Commands in a skill

`` !`command` `` in the body runs when the skill loads, and is replaced by the command's
output — the current branch, a list of changed files:

```markdown
Current branch: !`git branch --show-current`
Changed files: !`git diff --name-only`
```

When the agent loads such a skill you approve the commands first, shown with the arguments
filled in; when you start it with `/name`, starting it is the approval. A failing command is
replaced by its exit code and error, and each command gets `runtime.shell_timeout` seconds.

### Hooks and pre-approved tools

A skill can carry [hooks](hooks.md) and an `allowed-tools` list. Both take effect the first
time the skill loads and last for the rest of the session, including after you resume it. The
agent is told what the skill put in force.

```markdown
---
name: deploy
description: Deploy the service.
allowed-tools: Bash(./deploy.sh:*) Read
hooks:
  PreToolUse:
    - matcher: Bash
      hooks:
        - type: command
          command: "${CLAUDE_PROJECT_DIR}/.wizolt/guard-prod.sh"
---
```

`allowed-tools` lets calls run without asking you:

| Rule | Covers |
|---|---|
| `Read` | Every call to that tool |
| `Bash(git status)` | Exactly that command |
| `Bash(npm run:*)` | `npm run` followed by anything |
| `Read(docs/*)` | Paths matching the pattern |

A command that chains, pipes, substitutes or redirects (`;`, `&&`, `|`, `$(…)`, `>`) is never
covered, and still asks.

### Running in the worker

`context: fork` runs the skill in the [worker](worker.md). You confirm it like a Delegate send,
and only the worker's report comes back, so the skill's instructions, commands and hooks stay
out of your conversation. Without a configured worker, the skill loads normally.

Claude Code's `agent` and `model` fields are not supported. `/skills` lists them as warnings:
wizolt has one worker, set under `[worker]`, and keeps your session's model.
