import asyncio
import json
from unittest.mock import AsyncMock

from duet.adapters.base import TurnResult
from duet.core.config import Config
from duet.core.conventions import load_conventions, save_conventions
from duet.tests.test_agreement import orch, advance, agreed, start


RULES = '# Coding conventions\n\n- Public functions use snake_case.\n- Keep side effects out of the parser.\n'


def enable(orch):
    save_conventions(orch.cfg, RULES)
    orch.cfg.settings['coding_conventions_enabled'] = True
    orch.cfg.save_roles()


def report(snapshot, status='pass', digest=None):
    return '```duet-conventions\n' + json.dumps({
        'sha256': digest or snapshot['sha256'], 'status': status,
        'checks': [{'rule': 'Public functions use snake_case', 'status': status,
                    'evidence': 'src.py:1 answer uses snake_case; inspected the implementation.'},
                   {'rule': 'Keep side effects out of the parser', 'status': status,
                    'evidence': 'src.py:1-3 contains only a return statement; no I/O.'}]
    }) + '\n```\n'


def test_sequential_task_pins_rules_and_requires_reviewer_evidence(orch):
    enable(orch)
    async def run():
        await agreed(orch)
        snapshot = dict(orch.cfg.state.task['conventions'])
        save_conventions(orch.cfg, '# New rules\nUse camelCase.')
        orch.cfg.settings['coding_conventions_enabled'] = False
        await advance(orch, 'implementer', '<!-- duet: REPORT done -->', 'implement')
        assert 'Public functions use snake_case' in orch.prompts[-1][2]
        assert 'Use camelCase' not in orch.prompts[-1][2]
        next_turn = await advance(orch, 'architect', '<!-- duet: ACCEPT -->', 'verify')
        assert next_turn[:2] == ('architect', 'verify')
        assert orch.cfg.state.task['phase'] == 'verify'
        next_turn = await advance(orch, 'architect', report(snapshot, 'fail')+'<!-- duet: ACCEPT -->', 'verify')
        assert next_turn[:2] == ('architect', 'verify')
        assert orch.cfg.state.task is not None
        next_turn = await advance(orch, 'architect', report(snapshot)+'<!-- duet: ACCEPT -->', 'verify')
        assert next_turn is None and orch.cfg.state.task is None
        checks = [event.data for event in orch.events if event.kind == 'convention_check']
        assert [check['passed'] for check in checks] == [False, False, True]
    asyncio.run(run())


def test_sprint_uses_review_workflow_when_conventions_required(orch):
    enable(orch)
    orch.cfg.state.mode = 'sprint'
    next_turn = asyncio.run(start(orch))
    assert next_turn[:2] == ('implementer', 'plan')
    assert orch.cfg.state.task['conventions']['enabled']
    restored = Config(orch.project)
    restored.load()
    assert restored.state.task['conventions'] == orch.cfg.state.task['conventions']


def test_direct_completion_cannot_skip_convention_report(orch):
    enable(orch)
    async def run():
        next_turn = await advance(orch, 'architect', '<!-- duet: STATUS done -->', 'human')
        assert next_turn[:2] == ('architect', 'system')
        assert await advance(orch, 'architect', report(load_conventions(orch.cfg))+'<!-- duet: STATUS done -->') is None
    asyncio.run(run())


def test_missing_required_file_does_not_run_an_agent(orch):
    orch.cfg.settings['coding_conventions_enabled'] = True
    orch.usage.run = AsyncMock(return_value=TurnResult('should not run'))
    async def run():
        ad = await orch.adapter('architect')
        result = await orch.run_agent_turn(ad, 'Implement')
        assert not result.ok and '컨벤션' in result.error
        assert await orch._turn('architect', 'human', 'Implement') is None
    asyncio.run(run())
    orch.usage.run.assert_not_awaited()


def test_disabled_task_does_not_acquire_later_enabled_rules(orch):
    async def run():
        await agreed(orch)
        assert not orch.cfg.state.task['conventions']['enabled']
        enable(orch)
        await advance(orch, 'implementer', '<!-- duet: REPORT done -->', 'implement')
        assert 'Public functions use snake_case' not in orch.prompts[-1][2]
        await advance(orch, 'architect', '<!-- duet: ACCEPT -->', 'verify')
        assert orch.cfg.state.task is None
    asyncio.run(run())


def test_review_written_to_dialogue_is_not_lost_in_short_response(orch):
    enable(orch)
    async def run():
        await agreed(orch)
        snapshot = dict(orch.cfg.state.task['conventions'])
        await advance(orch, 'implementer', '<!-- duet: REPORT done -->', 'implement')
        async def write_report(adapter, prompt):
            orch.dialogue.append_turn('architect', orch._active_turn_n, report(snapshot)+'<!-- duet: ACCEPT -->')
            return TurnResult('DIALOGUE.md에 검토를 기록했습니다.')
        assert await advance(orch, 'architect', write_report, 'verify') is None
        assert orch.cfg.state.task is None
    asyncio.run(run())
