"""A JEV choice must lead to an observed command result, not a false success."""
import asyncio
from unittest.mock import AsyncMock

from duet.adapters.base import TurnResult
from duet.core.dialogue import Turn
from duet.core.jev_log import DecisionLogEntry
from duet.core.work import REPORT_TRIGGER, WorkItem
from duet.tests.test_agreement import advance, orch


def ready(orch, monkeypatch):
    orch.cfg.settings.update(jev_mode='on', jev_flow=True)
    orch.jev.route_turn = AsyncMock(return_value='follow')
    monkeypatch.setattr(orch.work, 'schedule', lambda: None)
    item = WorkItem('map', 'implementer', 'Build the approved map.', status='waiting:implement')
    orch.work.items[item.id] = item
    return item


def test_internal_report_does_not_drop_resume_and_receipt_confirms_change(orch, monkeypatch):
    item = ready(orch, monkeypatch)
    async def classify(*args, **kwargs):
        orch.inbox.put_nowait((None, REPORT_TRIGGER))
        return 'follow'
    orch.jev.route_turn.side_effect = classify
    asyncio.run(advance(orch, 'architect', '<!-- duet: RESUME_WORK map Code only; deploy after merge. -->'))
    assert item.status == 'queued'
    assert not orch.has_pending_user_input()
    receipt = [e for e in orch.events if e.kind == 'control_outcome'][-1].data
    assert receipt['stage'] == 'executed' and receipt['applied']
    assert receipt['receipts'][0]['before'] == 'waiting:implement'
    assert receipt['receipts'][0]['after'] == 'queued'


def test_rejected_resume_is_reported_to_architect_without_false_success(orch, monkeypatch):
    item = ready(orch, monkeypatch)
    orch.cfg.settings['design_questions'] = True
    orch.design.defer('implementer#map', 'May we delete the production backup?')
    nxt = asyncio.run(advance(orch, 'architect', '<!-- duet: RESUME_WORK map proceed -->'))
    assert item.status == 'waiting:implement' and not item.inbox
    assert nxt[:2] == ('architect', 'system') and '실행되지 않았습니다' in nxt[2]
    receipt = [e for e in orch.events if e.kind == 'control_outcome'][-1].data
    assert receipt['stage'] == 'rejected' and not receipt['applied']
    assert orch.design.blocked('implementer#map')


def test_same_rejection_feedback_is_bounded(orch, monkeypatch):
    ready(orch, monkeypatch)
    async def run():
        for _ in range(2):
            assert await advance(orch, 'architect', '<!-- duet: RESUME_WORK nonexistent -->')
        assert await advance(orch, 'architect', '<!-- duet: RESUME_WORK nonexistent -->') is None
    asyncio.run(run())
    assert not orch.design.state.get('pending')


def test_user_input_supersedes_command_but_retains_review_context(orch, monkeypatch):
    item = ready(orch, monkeypatch)
    async def classify(*args, **kwargs):
        orch.inbox.put_nowait((None, 'Cancel deployment; only explain the changes.'))
        return 'follow'
    orch.jev.route_turn.side_effect = classify
    assert asyncio.run(advance(orch, 'architect', '<!-- duet: RESUME_WORK map deploy -->')) is None
    assert item.status == 'waiting:implement' and orch._deferred_controls
    nxt = orch._attach_deferred_controls(orch._drain_inbox(None))
    assert nxt[:2] == ('architect', 'system')
    assert '미처리 제어 재검토' in nxt[2] and '실행된 것으로 간주하지' in nxt[2]
    assert not orch._deferred_controls
    assert any(e.kind == 'control_outcome' and e.data['stage'] == 'superseded' for e in orch.events)


def test_intermediate_work_block_is_never_executed(orch, monkeypatch):
    ready(orch, monkeypatch)
    submit = monkeypatch.setattr(orch.work, 'submit', lambda *_: (_ for _ in ()).throw(AssertionError('stale block executed')))
    old = '```duet-work\n- id: old\n  role: implementer\n  task: obsolete\n```'
    tr = TurnResult('설계만 정리했습니다.', full_text=old + '\n\n설계만 정리했습니다.')
    turn = Turn('architect', 10, '', 0, 0, '설계만 정리했습니다.')
    assert asyncio.run(orch._decide('architect', 'system', tr, turn)) is None
    assert 'old' not in orch.work.items


def test_control_receipt_is_appended_to_same_decision_markdown(orch):
    path = orch.cfg.project / '.duet/logs/jev/DIALOGUE.md'
    path.parent.mkdir(parents=True)
    orch.jev._log_entry.set(DecisionLogEntry(path, 'test-decision'))
    orch.record_control_outcome('follow', 'rejected', role='architect', turn=675,
        receipts=[dict(action='RESUME_WORK', id='map', accepted=False,
                       before='waiting:implement', after='waiting:implement', reason='User answer required')])
    content = path.read_text()
    assert 'test-decision' in content and 'control_execution' in content
    assert 'rejected' in content and '"applied": false' in content


def test_unrelated_worker_completion_is_not_a_follow_execution(orch, monkeypatch):
    item = ready(orch, monkeypatch)
    async def classify(*args, **kwargs):
        item.status = 'merged'
        return 'follow'
    orch.jev.route_turn.side_effect = classify
    asyncio.run(advance(orch, 'architect', '현재 진행 상황을 확인했습니다.'))
    outcome = [e for e in orch.events if e.kind == 'control_outcome'][-1].data
    assert outcome['stage'] == 'no_transition' and not outcome['applied']


def test_local_source_repair_does_not_attach_to_previous_decision(orch, monkeypatch):
    ready(orch, monkeypatch)
    path = orch.cfg.project / '.duet/logs/jev/DIALOGUE.md'
    path.parent.mkdir(parents=True)
    orch.jev._log_entry.set(DecisionLogEntry(path, 'old-judgment'))
    tr = TurnResult('<!-- duet: RESUME_WORK map -->')
    turn = Turn('architect', 675, '', 0, 0, '재개했습니다.')
    nxt = asyncio.run(orch._decide('architect', 'system', tr, turn))
    assert nxt[:2] == ('architect', 'system')
    content = path.read_text()
    assert 'control_execution' in content and 'old-judgment' not in content
    assert '675' in content and 'routed' in content


def test_deferred_review_survives_user_input_arriving_before_next_dispatch(orch, monkeypatch):
    ready(orch, monkeypatch)
    monkeypatch.setattr(orch.work, 'start', lambda: None)
    orch.defer_control_turn('architect', 'system', Turn('architect', 675, '', 0, 0, ''), 'earlier command not executed')
    calls = 0
    async def budget():
        nonlocal calls
        calls += 1
        if calls == 1:
            orch.inbox.put_nowait((None, '새 지시: 배포는 취소하고 설명만 해주세요.'))
        return True
    monkeypatch.setattr(orch, '_check_budget', budget)
    orch.scripts['architect'].append('설명만 제공합니다. <!-- duet: STATUS done -->')
    asyncio.run(orch.run(('architect', 'system', '상태 확인')))
    assert len(orch.prompts) == 1
    assert '미처리 제어 재검토' in orch.prompts[0][2]
    assert '#675' in orch.prompts[0][2]
    assert not orch._deferred_controls


def test_uncertain_execution_claim_requests_bounded_evidence_without_resuming(orch, monkeypatch):
    item = ready(orch, monkeypatch)
    orch.jev.route_turn.return_value = None
    monkeypatch.setattr(orch.jev, 'needs_control_clarification', lambda: True)
    async def run():
        for _ in range(2):
            nxt = await advance(orch, 'architect', '이번 턴에서 map을 재개했습니다.')
            assert nxt[:2] == ('architect', 'system') and '판단 근거 보완' in nxt[2]
        assert await advance(orch, 'architect', '이번 턴에서 map을 재개했습니다.') is None
    asyncio.run(run())
    assert item.status == 'waiting:implement' and not orch.design.state.get('pending')
