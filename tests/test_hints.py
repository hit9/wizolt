"""Unit tests for the idle-input hint mechanism (wizolt/ui/cli/hints.py)."""

from wizolt.ui.cli.hints import HINTS, Context, HintPicker

SESSIONS = "/sessions resumes a past session"
SKILL = "$skill points the agent at a skill"
MCP = "@server.tool mentions an MCP tool"
IMAGE = "Paste an image path to attach it"
WIZOLT_HELP = "Questions about wizolt? Just ask"
PS = "/ps lists background jobs"


def _pool_for(ctx: Context) -> list[str]:
    """Capture the weighted pool the picker draws from for a given context."""
    captured: dict[str, list[str]] = {}

    def capture(pool):
        captured["pool"] = list(pool)
        return pool[0]

    HintPicker(choice=capture).pick(ctx)
    return captured["pool"]


def test_pick_returns_an_applicable_hint():
    picker = HintPicker()
    ctx = Context(early=False)
    assert picker.pick(ctx) in {hint.text for hint in HINTS}


def test_pick_is_stable_for_the_same_context():
    picker = HintPicker()
    ctx = Context(early=False)
    assert picker.pick(ctx) == picker.pick(ctx)


def test_pick_rerolls_when_context_changes():
    picks = iter(["first", "second"])
    picker = HintPicker(choice=lambda pool: next(picks))
    ctx1 = Context(early=False)
    assert picker.pick(ctx1) == "first"
    assert picker.pick(ctx1) == "first"  # cached within a context
    ctx2 = Context(early=False, jobs_running=True)
    assert picker.pick(ctx2) == "second"  # a new situation re-rolls


def test_sessions_hint_only_applies_when_early():
    sessions = next(hint for hint in HINTS if hint.text == SESSIONS)
    assert sessions.when is not None
    assert sessions.when(Context(early=True))
    assert not sessions.when(Context(early=False))


def test_mature_pool_excludes_navigation():
    pool = _pool_for(Context(early=False))
    assert SESSIONS not in pool


def test_early_pool_includes_sessions():
    assert SESSIONS in _pool_for(Context(early=True))


def test_skill_and_mcp_hints_gated_on_availability():
    skill = next(hint for hint in HINTS if hint.text == SKILL)
    mcp = next(hint for hint in HINTS if hint.text == MCP)
    assert skill.when is not None and mcp.when is not None
    base = Context(early=False)
    assert not skill.when(base) and not mcp.when(base)
    assert skill.when(Context(early=False, skills_available=True))
    assert mcp.when(Context(early=False, mcp_connected=True))


def test_unavailable_features_drop_out_of_the_pool():
    bare = _pool_for(Context(early=False))
    assert SKILL not in bare and MCP not in bare
    rich = _pool_for(Context(early=False, skills_available=True, mcp_connected=True))
    assert SKILL in rich and MCP in rich


def test_image_hint_is_always_available():
    assert IMAGE in _pool_for(Context(early=False))


def test_wizolt_help_hint_is_always_available():
    assert WIZOLT_HELP in _pool_for(Context(early=False))


def test_ps_hint_only_while_jobs_running_and_weighted():
    ps = next(hint for hint in HINTS if hint.text == PS)
    assert ps.weight > 1
    assert PS not in _pool_for(Context(early=False))
    pool = _pool_for(Context(early=False, jobs_running=True))
    assert pool.count(PS) == ps.weight


def test_pick_varies_across_rounds_but_is_stable_within_one():
    picks = iter(["first", "second"])
    picker = HintPicker(choice=lambda pool: next(picks))
    ctx = Context(early=False)
    assert picker.pick(ctx, 1) == "first"
    assert picker.pick(ctx, 1) == "first"  # cached: no flicker within a round
    assert picker.pick(ctx, 2) == "second"  # a new round re-rolls the random pick
