"""Package authoring through the same admission/worker path as single-file plugins."""

import shutil
from pathlib import Path

import pytest

from wizolt.plugins.loading import PluginSource
from wizolt.plugins.process import PluginProcess
from wizolt.plugins.runtime import PluginRuntime
from wizolt.plugins.testing import PluginTrial
from wizolt.sdk import Context, PluginError


def package(root, *, src=False, entry="sample:register"):
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "sample-plugin"\nversion = "0.1.0"\ndependencies = []\n'
        f'[tool.wizolt.plugin]\nsdk = 1\nentry = "{entry}"\n'
    )
    module = root / "src" / "sample" if src else root / "sample"
    module.mkdir(parents=True)
    (module / "__init__.py").write_text(
        'from .helper import VALUE\nfrom importlib.resources import files\n'
        'def register(p):\n    text = files(__package__).joinpath("label.txt").read_text()\n'
        '    p.field("value", lambda ctx: VALUE)\n    p.field("text", lambda ctx: text)\n'
    )
    (module / "helper.py").write_text("VALUE = 1\n")
    (module / "label.txt").write_text("original")
    return module


def context():
    return Context("test", "main", "/tmp", "idle", 0, 0, "test", 0)


@pytest.mark.parametrize("action", ["reload", "hot_reload", "load", "enable", "install"])
async def test_installed_package_cannot_change_identity_on_reload(tmp_path, action):
    from agent_harness import session_with_provider

    from wizolt.plugins.session import SessionPlugins

    root = tmp_path / "source"
    package(root)
    runtime = SessionPlugins(session_with_provider(tmp_path))
    try:
        await runtime.manage("enable", str(root))
        manifest = root / "pyproject.toml"
        manifest.write_text(manifest.read_text().replace('name = "sample-plugin"', 'name = "renamed"'))
        if action == "reload":
            with pytest.raises(PluginError, match="identity"):
                await runtime.manage("reload", "sample_plugin")
        elif action == "hot_reload":
            [result] = (await runtime.hot_reload("sample_plugin"))["plugins"]
            assert result["status"] == "failed" and "identity" in result["error"]
        elif action == "load":
            await runtime.close()
            runtime = SessionPlugins(session_with_provider(tmp_path))
            await runtime.load()
            assert "identity" in runtime.problems["sample_plugin"]
            assert not runtime.entries
            return
        else:
            from wizolt.plugins.installation import PluginInstallations

            with pytest.raises(PluginError, match="identity"):
                await PluginInstallations(runtime.catalog, runtime.session.cwd).manage(action, "sample_plugin")
        assert list(runtime.entries) == ["sample_plugin"]
        assert runtime.entries["sample_plugin"].active.plugin.name == "sample_plugin"
        assert runtime.fields()["plugins.sample_plugin.value"] == 1
    finally:
        await runtime.close()


async def test_pending_package_cannot_change_identity_before_publication(tmp_path):
    root = tmp_path / "source"
    package(root)
    runtime = PluginRuntime(context)
    try:
        await runtime.start_turn()
        await runtime.manage("enable", str(root))
        manifest = root / "pyproject.toml"
        manifest.write_text(manifest.read_text().replace('name = "sample-plugin"', 'name = "renamed"'))
        with pytest.raises(PluginError, match="identity"):
            await runtime.manage("enable", str(root))
        await runtime.finish_turn()
        assert list(runtime.entries) == ["sample_plugin"]
    finally:
        await runtime.close()


@pytest.mark.parametrize("src", [False, True])
async def test_package_relative_imports_resources_reload_and_full_rollback(tmp_path, src):
    root = tmp_path / "source"
    module = package(root, src=src)
    runtime = PluginRuntime(context)
    try:
        original = (await runtime.manage("enable", str(root)))["version"]
        assert runtime.fields() == {"plugins.sample_plugin.value": 1, "plugins.sample_plugin.text": "original"}
        (module / "helper.py").write_text("VALUE = 2\n")
        (module / "label.txt").write_text("updated")
        reloaded = await runtime.manage("reload", "sample_plugin")
        assert reloaded["version"] != original
        assert runtime.fields() == {"plugins.sample_plugin.value": 2, "plugins.sample_plugin.text": "updated"}
        (module / "helper.py").write_text("raise RuntimeError('bad candidate')\n")
        with pytest.raises(PluginError, match="bad candidate"):
            await runtime.manage("reload", "sample_plugin")
        assert runtime.fields()["plugins.sample_plugin.value"] == 2
        shutil.rmtree(root)
        await runtime.manage("rollback", "sample_plugin")
        assert runtime.fields() == {"plugins.sample_plugin.value": 1, "plugins.sample_plugin.text": "original"}
    finally:
        await runtime.close()


async def test_admission_freezes_source_and_parent_retires_snapshot(tmp_path):
    root = tmp_path / "source"
    module = package(root)
    source = PluginSource.read(str(root))
    (module / "helper.py").write_text("VALUE = 9\n")
    worker, _ = await PluginProcess.start(source)
    directory = Path(worker.snapshot.name)
    try:
        from dataclasses import asdict

        result = await worker.request("snapshot", context=asdict(context()))
        assert result["fields"]["value"] == 1
    finally:
        await worker.close()
    assert not directory.exists()


@pytest.mark.parametrize("special", ["symlink", "fifo", "directory-depth"])
def test_package_rejects_unsafe_or_unbounded_trees(tmp_path, special):
    root = tmp_path / "source"
    module = package(root)
    if special == "symlink":
        (module / "escape").symlink_to(tmp_path)
    elif special == "fifo":
        import os

        os.mkfifo(module / "pipe")
    else:
        for _ in range(34):
            module = module / "nested"
            module.mkdir()
    with pytest.raises(PluginError):
        PluginSource.read(str(root))


async def test_package_conventional_entry_and_module_collision(tmp_path):
    root = tmp_path / "source"
    module = package(root, entry="sample:register")
    manifest = root / "pyproject.toml"
    manifest.write_text('[project]\nname = "sample"\n[tool.wizolt.plugin]\nsdk = 1\n')
    (module / "__init__.py").write_text('def setup(p):\n    p.field("value", lambda ctx: 7)\n')
    report = await PluginTrial(context()).run(str(root))
    assert report.status == "passed", report.error
    assert report.frames[0]["fields"] == {"value": 7}
    manifest.write_text('[project]\nname = "sample"\n[tool.wizolt.plugin]\nsdk = 1\nentry = "sys:exit"\n')
    (root / "sys.py").write_text("def exit(p): pass\n")
    report = await PluginTrial(context()).run(str(root))
    assert report.status == "failed" and "conflicts" in report.error


@pytest.mark.parametrize("declaration", ['dynamic = ["dependencies"]', 'dynamic = 1', 'requires-python = ">=99"', 'dependencies = "bad"'])
def test_package_metadata_is_static_and_checks_python(tmp_path, declaration):
    root = tmp_path / "source"
    package(root)
    (root / "pyproject.toml").write_text(f'[project]\nname = "sample"\n{declaration}\n[tool.wizolt.plugin]\nsdk = 1\n')
    with pytest.raises(PluginError):
        PluginSource.read(str(root))


async def test_failed_package_worker_leaves_no_snapshot_directory(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    root = tmp_path / "source"
    module = package(root)
    (module / "__init__.py").write_text("import os\nos._exit(7)\n")
    report = await PluginTrial(context()).run(str(root))
    assert report.status == "failed"
    assert not list(tmp_path.glob("wizolt-plugin-*"))


def test_package_budget_counts_resources_and_empty_directories(tmp_path, monkeypatch):
    import wizolt.plugins.package as admission

    root = tmp_path / "source"
    module = package(root)
    monkeypatch.setattr(admission, "MAX_PACKAGE_BYTES", 32)
    with pytest.raises(PluginError, match="KiB"):
        PluginSource.read(str(root))
    monkeypatch.setattr(admission, "MAX_PACKAGE_BYTES", 4 * 1024 * 1024)
    monkeypatch.setattr(admission, "MAX_PACKAGE_ENTRIES", 8)
    for index in range(10):
        (module / str(index)).mkdir()
    with pytest.raises(PluginError, match="directory entries"):
        PluginSource.read(str(root))
