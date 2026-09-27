"""B2-9/14: a negotiation is one plan submission plus its review, not two turns."""
import asyncio
import pytest
from duet.tests.test_agreement import orch, start, advance, PLAN
from duet.core.config import Config
from duet.core.prompts import system_append, turn_prompt


@pytest.mark.parametrize('bad', [PLAN.replace('```files', '```text'),
                               PLAN.replace('test_command:', 'command:'),
                               PLAN.replace('```files', '```files\nx\n```\n```files')])
def test_plan_ready_rejects_invalid_immediately(orch, bad):
    async def run():
        await start(orch)
        nxt = await advance(orch, 'implementer', bad, 'plan')
        task = orch.cfg.state.task
        assert task['submitted_version'] == 0
        assert task['phase'] == 'plan' and not task['waiting']
        assert nxt[0] == 'implementer'
        assert '```files' in nxt[2] and 'test_command:' in nxt[2]
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['sprint', 'review', 'deliberate'])
@pytest.mark.parametrize('valid', [True, False])
def test_seventh_submission_requires_ask_six_pairs_persisted(orch, mode, valid):
    async def run():
        # Create an agreement task before changing mode, including sprint.
        await start(orch)
        orch.cfg.state.mode = mode
        orch.cfg.settings['plan_rounds'] = 100
        for i in range(6):
            nxt = await advance(orch, 'implementer', PLAN if valid else 'bad', 'plan')
            if valid:
                nxt = await advance(orch, 'architect', '<!-- duet: REVISE change -->', 'plan_review')
        assert nxt is None
        task = orch.cfg.state.task
        assert task['waiting']
        assert any(e.kind == 'ask' for e in orch.events)
        loaded = Config(orch.project); loaded.load()
        assert loaded.state.task['negotiations'] == 6
        nxt = await advance(orch, 'architect', '<!-- duet: RESUME -->')
        assert nxt[0] == 'implementer'
        assert task['negotiation_limit'] == 12
    asyncio.run(run())


def test_plan_prompts_explicit_files_label(orch):
    async def run():
        await start(orch)
        role = orch.cfg.roles['implementer']
        assert '```files' in system_append(orch.cfg, role)
        assert '```files' in turn_prompt(orch.cfg, role, 1, 0, 'plan', '')
    asyncio.run(run())


def test_agree_revalidates_damaged_submitted_plan(orch):
    async def run():
        await start(orch)
        await advance(orch, 'implementer', PLAN, 'plan')
        task = orch.cfg.state.task
        path = orch.project / task['plan_path']
        path.write_text(path.read_text().replace('```files', '```text'))
        nxt = await advance(orch, 'architect', '<!-- duet: AGREE v1 -->', 'plan_review')
        assert nxt[1] == 'system'
        assert task['phase'] == 'plan_review' and task['agreed_version'] is None
        assert any('AGREE 거부' in e.data.get('text', '') for e in orch.events)
    asyncio.run(run())


def test_human_reply_extends_negotiations_once(orch):
    async def run():
        await start(orch)
        task = orch.cfg.state.task
        task.update(negotiations=6, negotiation_limit=6)
        assert orch._plan_retry('implementer', 'retry') is None
        orch._append_human(None, 'continue')
        orch._append_human(None, 'details')
        assert task['negotiation_limit'] == 12
        assert task['waiting']  # main still decides RESUME
    asyncio.run(run())


@pytest.mark.parametrize('report', ['done', 'deviation'])
def test_plan_report_repetition_cannot_bypass_negotiation_limit(orch, report):
    async def run():
        await start(orch)
        orch.cfg.settings['plan_rounds'] = 100
        orch.cfg.state.max_turns = None
        text = PLAN.replace('<!-- duet: PLAN ready -->', f'<!-- duet: REPORT {report} -->')
        for _ in range(7):
            nxt = await advance(orch, 'implementer', text, 'plan')
            assert orch.cfg.state.task['submitted_version'] == 0
            if nxt is None: break
        assert orch.cfg.state.task['negotiations'] == 6
        assert orch.cfg.state.task['waiting'] and nxt is None
        assert any(e.kind == 'ask' for e in orch.events)
    asyncio.run(run())
