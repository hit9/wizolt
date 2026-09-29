"""Fresh frontends must see installed skills before their first read or dispatch."""

import pytest

from wizolt.agent.engine import Agent
from wizolt.agent.lifecycle import bootstrap_features
from wizolt.config import Config
from wizolt.providers.sync import CatalogRuntime
from wizolt.session import Session, SessionSnapshotStore
from wizolt.ui.cli import CommandLoop
from wizolt.ui.cli.update import UpdateChecker


@pytest.mark.parametrize('entered', ['/guide', 'Please use $guide'])
@pytest.mark.parametrize('inspect_first', [False, True])
async def test_headless_first_input_sees_skills_even_after_status(tmp_path, monkeypatch, entered, inspect_first):
    folder = tmp_path / '.wizolt' / 'skills' / 'guide'
    folder.mkdir(parents=True)
    (folder / 'SKILL.md').write_text('---\nname: guide\ndescription: Startup guide\n---\nSTARTUP_SKILL_BODY\n')
    config = Config.from_dict({
        'provider': {'active': 'test', 'test': {'url': 'http://test', 'key': 'test', 'model': 'test'}},
        'paths': {'data_dir': str(tmp_path / 'data')},
    })
    s = Session(cwd=str(tmp_path), config=config)
    bootstrap_features(s)  # Deliberately bypass harnesses that used to reload on the caller's behalf.
    lines = iter(['/skills', *(['/status'] if inspect_first else []), entered, '/quit'])
    output, requests = [], []
    agent = Agent(s, output_fn=output.append)
    command_loop = CommandLoop(agent, input_fn=lambda _prompt='': next(lines), output_fn=output.append)
    monkeypatch.setattr(UpdateChecker, 'load_cached', lambda _: False)
    monkeypatch.setattr(CatalogRuntime, 'refresh_due', lambda _: False)
    monkeypatch.setattr(SessionSnapshotStore, 'clean_expired', lambda *_: 0)

    async def request(messages, tools=None):
        requests.append((messages, tools))
        return {'role': 'assistant', 'content': 'done'}, [], 'done'
    monkeypatch.setattr(agent.model, 'request', request)
    try:
        assert await command_loop._run_frontend(show_banner=False) == 0
        assert 'No skills installed.' not in '\n'.join(output)
        assert 'Unknown command: /guide' not in '\n'.join(output)
        assert len(requests) == 1
        messages, tools = requests[0]
        if entered.startswith('/'):
            assert 'STARTUP_SKILL_BODY' in str(messages)
        else:
            assert any(message.get('_session_event') == 'skill_mentions' and 'guide [project]' in message['content'] for message in messages)
        assert '--- SKILLS ---' in str(messages)
        assert any(tool['function']['name'] == 'Skill' for tool in tools)
    finally:
        s.close()
