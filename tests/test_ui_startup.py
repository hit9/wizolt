"""The first editable frame survives assembly and becomes the session's real TUI."""

import asyncio
import subprocess
import sys
import threading

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.input.vt100_parser import Vt100Parser
from prompt_toolkit.keys import Keys
from tui_harness import ResizableOutput, loop, rendered_screen_text, wait_for

import wizolt.ui.tui.app as tui_module
from wizolt.base import ConfigError
from wizolt.providers.sync import CatalogRuntime
from wizolt.session import SessionSnapshotStore
from wizolt.ui.startup import _BackgroundReply, _run_startup
from wizolt.ui.cli.update import UpdateChecker
from wizolt.utils import terminal


def test_startup_import_keeps_session_and_command_assembly_off_the_first_frame():
    probe = (
        "import sys; import wizolt.ui.startup; "
        "assert not {'wizolt.session', 'wizolt.tools', 'wizolt.agent.engine', 'wizolt.ui.cli', "
        "'openai', 'anthropic', 'rich', 'markdown_it'} & sys.modules.keys()"
    )
    subprocess.run([sys.executable, '-c', probe], check=True, capture_output=True)


@pytest.fixture
def startup_ui(monkeypatch):
    monkeypatch.setattr(terminal, '_reported', terminal._UNQUERIED)
    terminal.background.cache_clear()
    monkeypatch.setattr(UpdateChecker, 'load_cached', lambda _: False)
    monkeypatch.setattr(CatalogRuntime, 'refresh_due', lambda _: False)
    monkeypatch.setattr(SessionSnapshotStore, 'clean_expired', lambda *_: 0)
    output = ResizableOutput()
    applications = []
    with create_pipe_input() as pipe:
        def application(**kwargs):
            app = Application(**(kwargs | {'input': pipe, 'output': output}))
            applications.append(app)
            return app
        monkeypatch.setattr(tui_module, 'Application', application)
        yield pipe, output, applications
    terminal.background.cache_clear()


async def test_starting_accepts_input_then_attaches_status_activity_and_commands(tmp_path, startup_ui):
    pipe, output, apps = startup_ui
    command_loop = loop(tmp_path)
    command_loop.session.config.provider.model = 'startup-model'
    release = threading.Event()
    assembling = threading.Event()
    commands = []
    command = command_loop.command

    async def tracked(value):
        commands.append(value)
        return await command(value)
    command_loop.command = tracked

    def assemble():
        assert apps[0].renderer.last_rendered_screen is not None
        assembling.set()
        assert release.wait(timeout=5)
        return command_loop

    task = asyncio.create_task(_run_startup(assemble, 'wizolt test banner\n\n'))
    try:
        await wait_for(assembling.is_set)
        assert 'starting…' in rendered_screen_text(apps[0], output)
        attrs = apps[0]._merged_style.get_attrs_for_style_str('class:bottom-toolbar')
        assert not attrs.reverse and attrs.bgcolor in {'', 'default'}
        buffer = apps[0].layout.current_buffer
        pipe.send_text('/status\r/help\rdraft stays')
        await wait_for(lambda: buffer.text == 'draft stays')
        assert commands == []
        release.set()
        await wait_for(lambda: commands == ['/status', '/help'] or task.done())
        if task.done():
            await task
        assert commands == ['/status', '/help']
        await wait_for(lambda: 'startup-model' in rendered_screen_text(apps[0], output))
        assert len(apps) == 1
        assert apps[0].layout.current_buffer is buffer
        assert buffer.text == 'draft stays'
        assert buffer.cursor_position == len('draft stays')
        assert buffer.history.get_strings()[-2:] == ['/status', '/help']
        pipe.send_text('\x15')
        await wait_for(lambda: buffer.text == '' and buffer._load_history_task is not None and buffer._load_history_task.done())
        pipe.send_text('\x1b[A')
        await wait_for(lambda: buffer.text == '/help')
        pipe.send_text('\x1b[A')
        await wait_for(lambda: buffer.text == '/status')
        pipe.send_text('\x1b[B\x1b[B')
        await wait_for(lambda: buffer.text == '')
        tui = command_loop.presentation.tui
        assert tui.scrollback.transcript.count('wizolt test banner\n\n') == 1
        command_loop.presentation.model_stream_text = 'live-preview'
        command_loop.presentation.model_stream_kind = 'answer'
        tui.set_running("working")
        await wait_for(lambda: 'live-preview' in rendered_screen_text(apps[0], output))
        tui.set_idle()
        pipe.send_text('\x15\x04')
        assert await asyncio.wait_for(task, 5) == (0, command_loop)
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        command_loop.session.close()


@pytest.mark.parametrize('failure', [False, True])
async def test_exit_or_failure_during_assembly_restores_the_application(tmp_path, startup_ui, failure):
    pipe, output, apps = startup_ui
    command_loop = loop(tmp_path)
    release = threading.Event()
    assembling = threading.Event()

    def assemble():
        assembling.set()
        assert release.wait(timeout=5)
        if failure:
            raise ConfigError('broken startup')
        return command_loop

    task = asyncio.create_task(_run_startup(assemble, 'banner\n\n'))
    try:
        await wait_for(assembling.is_set)
        if not failure:
            pipe.send_text('\x04')
            await wait_for(lambda: not apps[0].is_running)
            assert not task.done(), 'assembly must settle before exit releases ownership'
        release.set()
        if failure:
            with pytest.raises(ConfigError, match='broken startup'):
                await asyncio.wait_for(task, 5)
        else:
            assert await asyncio.wait_for(task, 5) == (0, None)
        assert not apps[0].is_running
        assert command_loop.presentation.tui is None
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        command_loop.session.close()


@pytest.mark.parametrize('chunk_size', [1, 3, 1000])
def test_background_replies_preserve_keys_arrows_and_pasted_escape_sequences(chunk_size):
    keys, colors = [], []
    parser = Vt100Parser(keys.append)
    replies = _BackgroundReply(parser.feed, lambda: parser._in_bracketed_paste, colors.append)
    parser.feed = replies.feed
    color = '\x1b]11;rgb:ffff/eeee/dddd\x1b\\'
    pasted = 'pasted ' + color + ' text'
    data = 'draft' + color + '\x1b[?1;2c' + '\x1b[D' + '\x1b[200~' + pasted + '\x1b[201~tail'
    for offset in range(0, len(data), chunk_size):
        replies.feed(data[offset:offset + chunk_size])
    replies.flush()
    parser.flush()
    assert colors == [(255, 238, 221)]
    assert ''.join(key.data for key in keys if key.key not in {Keys.Left, Keys.BracketedPaste}) == 'drafttail'
    assert [key.data for key in keys if key.key == Keys.BracketedPaste] == [pasted]
    assert sum(key.key == Keys.Left for key in keys) == 1


def test_background_filter_releases_escape_and_unrecognized_sequences():
    forwarded = []
    replies = _BackgroundReply(forwarded.append, lambda: False, lambda _: None)
    replies.feed('\x1b')
    replies.flush()
    replies.feed('text\x1b[?invalid\x1b]11;not-color\x1b\\')
    replies.flush()
    assert ''.join(forwarded) == '\x1btext\x1b[?invalid\x1b]11;not-color\x1b\\'
