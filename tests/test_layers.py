"""Dependency-direction guard for module-level imports.

Application/UI -> agent orchestration -> model -> tools/features -> session -> shared values.
Within UI, commands/runtime sit above presentation and terminal rendering. agent/hooks and
agent/prompts are leaves: importing them must not load orchestration. TYPE_CHECKING and deferred
imports are excluded here; fresh-interpreter checks in test_startup cover assembly/import costs.
"""

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
WIZOLT = REPO / "wizolt"

# Module prefix -> layer number (larger = lower). Same-layer allowed; a source may only import
# targets whose layer is >= its own.
LAYERS = {
    "wizolt.__init__": 0,
    "wizolt": 100,
    "wizolt.__main__": 0,
    "wizolt.ui": 1,
    "wizolt.ui.cli": 1,
    "wizolt.ui.cli.presentation": 2,
    "wizolt.agent": 4,
    "wizolt.agent.lifecycle": 3,
    "wizolt.ui.tui": 2,
    "wizolt.ui.cli.hints": 1,
    "wizolt.ui.cli.update": 2,
    "wizolt.ui.tui.app": 2,
    "wizolt.ui.tui.views": 2,
    "wizolt.ui.render": 2,
    "wizolt.ui.bars": 2,
    "wizolt.ui.themes": 2,
    "wizolt.agent.engine": 3,
    "wizolt.agent.context": 4,
    "wizolt.agent.runner": 4,
    "wizolt.agent.compaction": 4,
    "wizolt.agent.vision": 4,
    "wizolt.model": 5,
    "wizolt.model.chat": 5,
    "wizolt.model.responses": 5,
    "wizolt.model.anthropic": 5,
    "wizolt.model.client": 5,
    "wizolt.model.resilience": 5,
    "wizolt.tools": 6,
    "wizolt.mcp": 6,
    "wizolt.mcp.config": 6,
    "wizolt.mcp.tokens": 6,
    "wizolt.mcp.rendering": 6,
    "wizolt.mcp.manager": 6,
    "wizolt.skill": 6,
    "wizolt.plugins": 6,
    "wizolt.sdk": 10,
    "wizolt.shellhooks": 6,
    "wizolt.mentions": 6,
    "wizolt.agentsmd": 6,
    "wizolt.agent.history": 6,
    "wizolt.session": 7,
    "wizolt.source": 8,
    "wizolt.image": 8,
    "wizolt.base": 9,
    "wizolt.agent.hooks": 9,
    "wizolt.config": 9,
    "wizolt.providers": 9,
    "wizolt.providers.catalog": 10,
    "wizolt.providers.compat": 9,
    "wizolt.providers.schema": 11,
    "wizolt.providers.sync": 9,
}
# Leaves: any layer may depend on them; their own dependencies are not checked.
LEAVES = ("wizolt.agent.prompts",)


def layer_of(module: str) -> int | None:
    """The layer for a module, matched by longest prefix."""
    best = None
    for prefix, layer in LAYERS.items():
        if (module == prefix or module.startswith(prefix + ".")) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, layer)
    return best[1] if best else None


def module_level_wizolt_imports(path: pathlib.Path) -> list[str]:
    """Module-level `import wizolt.x` / `from wizolt.x import ...` targets."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            child._parent = parent
    targets: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and isinstance(node._parent, ast.Module):
            if node.module and node.module.startswith("wizolt"):
                targets.append(node.module)
        elif isinstance(node, ast.Import) and isinstance(node._parent, ast.Module):
            for alias in node.names:
                if alias.name.startswith("wizolt"):
                    targets.append(alias.name)
    return targets


def all_sources() -> dict[str, pathlib.Path]:
    return {
        "wizolt." + path.relative_to(WIZOLT).as_posix()[:-3].replace("/", "."): path
        for path in WIZOLT.rglob("*.py")
    }


def source_layer(module: str) -> int:
    layer = layer_of(module)
    assert layer is not None, f"no layer configured for {module}"
    return layer


@pytest.mark.parametrize("module", sorted(all_sources()))
def test_module_level_imports_stay_within_or_below_layer(module: str):
    path = all_sources()[module]
    if any(module == leaf or module.startswith(leaf + ".") for leaf in LEAVES):
        return  # leaves may import anything
    source = source_layer(module)
    for target in module_level_wizolt_imports(path):
        if any(target == leaf or target.startswith(leaf + ".") for leaf in LEAVES):
            continue
        target_layer = layer_of(target)
        assert target_layer is not None, f"{module} imports {target}, which has no configured layer"
        assert target_layer >= source, (
            f"{path} imports {target} ({target} is layer {target_layer}, above {module}'s layer {source}); "
            "cross-layer imports may only point downward"
        )


def test_every_subpackage_is_declared_for_distribution():
    """setuptools uses an explicit package list, so a new subpackage that nobody adds there is
    simply absent from the wheel. Tests run from the source tree and never notice; the failure
    surfaces only as an ImportError after install, against whatever stale files remain."""
    import tomllib

    declared = set(tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["setuptools"]["packages"])
    found = {
        "wizolt." + path.parent.relative_to(WIZOLT).as_posix().replace("/", ".")
        for path in WIZOLT.rglob("__init__.py")
        if path.parent != WIZOLT
    } | {"wizolt"}
    assert found == declared, f"pyproject packages out of sync: missing {sorted(found - declared)}, stale {sorted(declared - found)}"
