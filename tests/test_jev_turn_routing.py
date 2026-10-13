"""Semantic routes preserve author, phase, human gates and existing review checks."""
import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from duet.adapters.base import TurnResult
from duet.core.dialogue import Turn
from duet.core.turn_routing import TurnRouter
from duet.core.work import REPORT_TRIGGER, WorkItem

from duet.tests.test_agreement import PLAN, advance, agreed, orch, start, submit_plan


def routing(orch, action):
    orch.cfg.settings.update(jev_mode='on', jev_flow=True)
    orch.jev.route_turn = AsyncMock(return_value=action)
    orch.jev.route_blocker = AsyncMock(return_value=None)
    orch.jev.handoff = AsyncMock(return_value=None)
    return orch.jev.route_turn


def test_repair_plan_keeps_phase_and_records_corrected_submission(orch):
    async def run():
        await start(orch)
        route = routing(orch, 'repair')
        nxt = await advance(orch, 'implementer', PLAN.replace('<!-- duet: PLAN ready -->', ''), 'plan')
        assert nxt[:2] == ('implementer', 'plan')
        assert orch.cfg.state.task['submitted_version'] == 0
        assert orch.cfg.state.task['negotiations'] == 0
        assert route.call_args.kwargs['issue']
        route.return_value = 'follow'
        nxt = await submit_plan(orch)
        assert nxt[:2] == ('architect', 'plan_review')
        assert orch.cfg.state.task['submitted_version'] == 1
    asyncio.run(run())


def test_architect_format_repair_does_not_repeat_worker_plan(orch):
    async def run():
        await start(orch)
        await submit_plan(orch)
        rounds = orch.cfg.state.task['rounds']
        negotiations = orch.cfg.state.task['negotiations']
        route = routing(orch, 'repair')
        nxt = await advance(orch, 'architect', '검토 결과 문제가 없습니다.\nAGREE v1', 'plan_review')
        assert nxt[:2] == ('architect', 'plan_review')
        assert orch.cfg.state.task['rounds'] == rounds
        assert orch.cfg.state.task['negotiations'] == negotiations
        route.return_value = 'follow'
        nxt = await advance(orch, 'architect', '<!-- duet: AGREE v1 -->', 'plan_review')
        assert nxt[:2] == ('implementer', 'implement')
    asyncio.run(run())


def test_verifier_repair_does_not_mark_accept_or_send_rework(orch):
    async def run():
        await agreed(orch)
        await advance(orch, 'implementer', '<!-- duet: REPORT done -->', 'implement')
        routing(orch, 'repair')
        nxt = await advance(orch, 'architect', '테스트 통과, 검토 완료.\nACCEPT', 'verify')
        assert nxt[:2] == ('architect', 'verify')
        assert orch.cfg.state.task['phase'] == 'verify'
    asyncio.run(run())


def test_clear_user_question_waits_even_full_auto_without_directive(orch):
    async def run():
        await agreed(orch)
        orch.cfg.settings['full_auto'] = True
        routing(orch, 'human')
        nxt = await advance(orch, 'implementer', '기존 운영 데이터를 삭제할까요? 삭제하면 복구할 수 없습니다.', 'implement')
        assert nxt is None and orch.cfg.state.task['waiting']
        assert any(e.kind == 'ask' and '운영 데이터' in e.data['text'] for e in orch.events)
    asyncio.run(run())


def test_follow_question_never_auto_consents(orch):
    async def run():
        await agreed(orch)
        orch.cfg.settings['full_auto'] = True
        routing(orch, 'follow')
        assert await advance(orch, 'implementer', '<!-- duet: ASK_HUMAN 데이터 삭제를 허용하나요? -->', 'implement') is None
        assert orch.cfg.state.task['waiting']
        assert any(e.kind == 'ask' for e in orch.events)
    asyncio.run(run())


def test_internal_consult_returns_readonly_architect_answer_to_same_worker(orch):
    async def run():
        await agreed(orch)
        route = routing(orch, 'architect')
        nxt = await advance(orch, 'implementer', '<!-- duet: ASK_HUMAN 기존 parse helper를 재사용해도 될까요? -->', 'implement')
        assert nxt[:2] == ('architect', 'jev_consult')
        assert not orch.cfg.state.task['waiting']
        route.return_value = 'follow'
        orch.scripts['architect'].append('기존 helper가 같은 인터페이스를 제공하므로 재사용하세요.')
        result, turn = await orch._turn(*nxt)
        assert orch.adapters['architect'].plan_read_only
        assert '읽기 전용' in orch.prompts[-1][2]
        nxt = await orch._decide('architect', 'jev_consult', result, turn)
        assert nxt[:2] == ('implementer', 'implement')
        assert '재사용하세요' in nxt[2]
        assert orch.cfg.state.task['phase'] == 'implement'
    asyncio.run(run())


def test_consult_user_question_defers_instead_of_returning_as_answer(orch):
    async def run():
        await agreed(orch)
        route = routing(orch, 'architect')
        await advance(orch, 'implementer', '기존 schema를 바꿔도 될까요?', 'implement')
        route.return_value = 'human'
        assert await advance(orch, 'architect', '하위 호환성을 깨는 변경을 허용할까요?', 'jev_consult') is None
        assert orch.cfg.state.task['waiting']
        assert orch.turn_router.consult is None
    asyncio.run(run())


def test_semantic_completion_enters_review_without_accepting(orch):
    async def run():
        await agreed(orch)
        routing(orch, 'review')
        nxt = await advance(orch, 'implementer', '합의한 구현을 마쳤습니다. 검토해 주세요.', 'implement')
        assert nxt[:2] == ('architect', 'verify')
        assert orch.cfg.state.task['phase'] == 'verify'
        orch.jev.handoff.assert_not_awaited()
    asyncio.run(run())


def test_initial_design_repair_cannot_start_implementation(orch):
    async def run():
        orch.cfg.settings['design_questions'] = True
        orch.design.state['phase'] = 'initial'
        route = routing(orch, 'repair')
        nxt = await advance(orch, 'architect', '<!-- duet: ASK_HUMAN 질문 -->', 'design_questions')
        assert nxt[:2] == ('architect', 'design_questions')
        assert orch.design.state['phase'] == 'initial'
        assert not orch.cfg.state.task
        route.return_value = 'human'
        assert await advance(orch, 'architect', '외부 서비스 없이 로컬 저장으로 진행할까요?\n<!-- duet: ASK_HUMAN 저장 방식 확인 -->', 'design_questions') is None
        assert orch.design.state['phase'] == 'awaiting_initial'
    asyncio.run(run())


def test_repairs_are_bounded_without_spending_plan_negotiations(orch):
    async def run():
        await start(orch)
        routing(orch, 'repair')
        for _ in range(2):
            assert (await advance(orch, 'implementer', '<!-- duet: ASK_HUMAN -->', 'plan'))[:2] == ('implementer', 'plan')
        assert await advance(orch, 'implementer', '<!-- duet: ASK_HUMAN -->', 'plan') is None
        assert orch.cfg.state.task['waiting']
        assert orch.cfg.state.task['negotiations'] == 0
    asyncio.run(run())


def test_explicit_blocked_and_deviation_never_offer_review_or_continue(orch):
    async def run():
        await agreed(orch)
        route = routing(orch, None)
        for report in ('blocked API key missing', 'deviation needs a new schema'):
            # Inspect candidate construction directly; leave fallback transitions to existing tests.
            from duet.core.dialogue import Turn
            from duet.adapters.base import TurnResult
            text = '<!-- duet: REPORT ' + report + ' -->'
            await orch.turn_router.route('implementer', 'implement', TurnResult(text), Turn('implementer', 1, '', 0, 0, text))
            if report.startswith('blocked'):
                orch.jev.route_blocker.assert_awaited_once()
                route.assert_not_awaited()
                continue
            allowed = route.call_args.kwargs['allowed']
            assert 'review' not in allowed and 'continue' not in allowed
    asyncio.run(run())


def test_input_arriving_during_route_discards_decision(orch):
    async def run():
        await agreed(orch)
        route = routing(orch, 'review')
        async def interrupt(*args, **kwargs):
            orch.inbox.put_nowait((None, '잠깐 중단'))
            return 'review'
        route.side_effect = interrupt
        assert await advance(orch, 'implementer', '<!-- duet: REPORT done -->', 'implement') is None
        assert orch.cfg.state.task['phase'] == 'implement'
    asyncio.run(run())


def test_question_arguments_survive_unrelated_summary_body(orch):
    async def run():
        route = routing(orch, 'human')
        body = '검증을 마쳤습니다.\n<!-- duet: ASK_HUMAN 기존 데이터를 삭제할까요? -->\n<!-- duet: ASK_HUMAN 백업은 얼마나 보관할까요? -->'
        assert await advance(orch, 'architect', body) is None
        shown = next(e.data['text'] for e in orch.events if e.kind == 'ask')
        assert '기존 데이터를 삭제할까요?' in shown and '백업은 얼마나 보관할까요?' in shown
        orch.cfg.settings['design_questions'] = True
        orch.design.state['phase'] = 'initial'
        assert await advance(orch, 'architect', body, 'design_questions') is None
        assert '기존 데이터를 삭제할까요?' in orch.design.state['initial']
        assert '백업은 얼마나 보관할까요?' in orch.design.state['initial']
    asyncio.run(run())


def test_question_cannot_be_overridden_by_implementation_handoff(orch):
    async def run():
        await agreed(orch)
        route = routing(orch, 'review')
        assert await advance(orch, 'implementer', '<!-- duet: ASK_HUMAN 배포 비용을 승인할까요? -->', 'implement') is None
        assert not {'continue', 'review'} & set(route.call_args.kwargs['allowed'])
        assert orch.cfg.state.task['phase'] == 'implement' and orch.cfg.state.task['waiting']
    asyncio.run(run())


def recorded_response(orch, document, response, *, full_text=None):
    async def write(ad, prompt):
        orch.dialogue.append_turn(ad.role.name, orch._active_turn_n, document)
        return TurnResult(response, full_text=full_text or response)
    return write


def test_missing_resume_control_repairs_author_before_any_classification_or_execution(orch):
    async def run():
        route = routing(orch, None)  # An outage or low confidence cannot allow a mismatched command.
        orch.work.schedule = Mock()
        item = WorkItem('map', 'implementer', '지도 구현', status='waiting:implement')
        orch.work.items[item.id] = item
        command = '<!-- duet: RESUME_WORK map 배포를 별도 작업으로 분리하고 구현을 마치세요 -->'
        nxt = await advance(orch, 'architect', recorded_response(orch, '지도 작업을 재개했습니다.', command))
        assert nxt[:2] == ('architect', 'system')
        assert '일치하지 않습니다' in nxt[2]
        assert item.status == 'waiting:implement' and not item.inbox
        route.assert_not_awaited()
        route.return_value = 'follow'
        assert await advance(orch, 'architect', command) is None
        assert item.status == 'queued'
        assert len(item.inbox) == 1
    asyncio.run(run())


@pytest.mark.parametrize(('document', 'response'), [
    ('<!-- duet: RESUME_WORK map -->', '<!-- duet: CANCEL_WORK map 삭제 -->'),
    ('<!-- duet: RESUME_WORK map 기존 설계 유지 -->', '<!-- duet: RESUME_WORK map 새 설계로 변경 -->'),
    ('<!-- duet: ASK_HUMAN 운영 데이터 삭제를 승인하나요? -->', '<!-- duet: STATUS done -->'),
    ('작업을 등록했습니다.', '```duet-work\n- id: map\n  role: implementer\n  task: 지도 구현\n```'),
    ('```duet-work\n- id: map\n  role: implementer\n  task: 지도 구현\n```',
     '```duet-work\n- id: map\n  role: implementer\n  task: 운영 데이터 삭제\n```'),
])
def test_source_mismatch_requires_repair(document, response):
    assert TurnRouter._source_issue(TurnResult(response), Turn('architect', 1, '', 0, 0, document))


@pytest.mark.parametrize(('document', 'response', 'full_text'), [
    ('<!-- duet: RESUME_WORK map -->', 'DIALOGUE에 기록했습니다.', '<!-- duet: CANCEL_WORK map 예전 초안 -->'),
    ('<!-- duet: RESUME_WORK map -->', '<!-- duet: RESUME_WORK map -->', '<!-- duet: CANCEL_WORK map 예전 초안 -->'),
    ('완료했습니다.', '예시:\n```html\n<!-- duet: CANCEL_WORK map 예시 -->\n```', None),
    ('완료했습니다.', '> <!-- duet: CANCEL_WORK map 예시 -->', None),
    ('```duet-work\n- id: map\n  task: 지도 구현\n```', 'DIALOGUE에 작업을 기록했습니다.', None),
    ('```duet-work\n- id: map\n  task: 지도 구현\n```', '```duet-work\n- {task: 지도 구현, id: map}\n```', None),
])
def test_source_comparison_allows_acknowledgments_examples_and_equivalent_controls(document, response, full_text):
    assert not TurnRouter._source_issue(TurnResult(response, full_text=full_text),
                                       Turn('architect', 1, '', 0, 0, document))


def test_source_repair_exhaustion_never_creates_a_design_question(orch):
    async def run():
        await agreed(orch)
        routing(orch, None)
        orch.cfg.settings['design_questions'] = True
        orch.design.state['phase'] = 'active'
        orch.design.defer = Mock()
        for _ in range(2):
            nxt = await advance(orch, 'implementer', recorded_response(
                orch, '보고를 마쳤습니다.', '<!-- duet: REPORT done -->'), 'implement')
            assert nxt[:2] == ('implementer', 'implement')
        assert await advance(orch, 'implementer', recorded_response(
            orch, '보고를 마쳤습니다.', '<!-- duet: REPORT done -->'), 'implement') is None
        assert orch.cfg.state.task['phase'] == 'implement'
        assert orch.cfg.state.task['waiting']
        orch.design.defer.assert_not_called()
        assert not any(e.kind == 'ask' for e in orch.events)
    asyncio.run(run())


def test_source_repair_keeps_existing_human_wait(orch):
    async def run():
        await agreed(orch)
        routing(orch, None)
        orch._wait_task('운영 데이터 삭제에 대한 실제 사용자 승인 대기')
        reason = orch.cfg.state.task['wait_reason']
        nxt = await advance(orch, 'architect', recorded_response(
            orch, '작업을 재개합니다.', '<!-- duet: RESUME -->'))
        assert nxt[:2] == ('architect', 'implement')
        assert orch.cfg.state.task['waiting']
        assert orch.cfg.state.task['wait_reason'] == reason
    asyncio.run(run())


def test_both_sources_are_provided_but_intermediate_controls_are_not_evidence(orch):
    async def run():
        route = routing(orch, 'follow')
        await advance(orch, 'architect', recorded_response(
            orch, '완료했습니다.\n<!-- duet: STATUS done -->', 'DIALOGUE에 기록했습니다.',
            full_text='<!-- duet: CANCEL_WORK map 오래된 초안 -->'))
        evidence = route.call_args.kwargs['evidence']
        assert evidence['response_text'] == 'DIALOGUE에 기록했습니다.'
        assert 'STATUS done' in evidence['dialogue_text']
        assert 'CANCEL_WORK' not in str(evidence)
        await advance(orch, 'architect', TurnResult('<!-- duet: STATUS done -->',
                                                  full_text='<!-- duet: CANCEL_WORK map 오래된 초안 -->\n<!-- duet: STATUS done -->'))
        assert route.call_args.args[1] == '<!-- duet: STATUS done -->'
    asyncio.run(run())


def test_internal_report_arriving_during_route_does_not_drop_completed_turn(orch):
    async def run():
        await agreed(orch)
        route = routing(orch, 'review')
        async def report(*args, **kwargs):
            orch.inbox.put_nowait((None, REPORT_TRIGGER))
            return 'review'
        route.side_effect = report
        nxt = await advance(orch, 'implementer', '<!-- duet: REPORT done -->', 'implement')
        assert nxt[:2] == ('architect', 'verify')
        assert orch.cfg.state.task['phase'] == 'verify'
        assert not orch.inbox.empty()
    asyncio.run(run())


def test_new_user_input_retains_discarded_control_for_reevaluation(orch):
    async def run():
        await agreed(orch)
        route = routing(orch, 'review')
        orch.defer_control_turn = Mock()
        async def user(*args, **kwargs):
            orch.inbox.put_nowait((None, '잠시 기다려'))
            return 'review'
        route.side_effect = user
        assert await advance(orch, 'implementer', '<!-- duet: REPORT done -->', 'implement') is None
        assert orch.cfg.state.task['phase'] == 'implement'
        args = orch.defer_control_turn.call_args.args
        assert args[:2] == ('implementer', 'implement')
        assert args[2].directive('REPORT') == 'done'
    asyncio.run(run())


def test_blocked_report_without_question_reaches_architect_then_same_worker(orch):
    async def run():
        await agreed(orch)
        route = routing(orch, 'follow')
        orch.jev.route_blocker.return_value = 'architect'
        report = '<!-- duet: REPORT blocked 병합 전에는 운영 배포를 실행할 수 없습니다 -->'
        nxt = await advance(orch, 'implementer', report, 'implement')
        assert nxt[:2] == ('architect', 'jev_consult')
        assert '병합 전에는 운영 배포' in nxt[2]
        assert not orch.cfg.state.task['waiting']
        route.assert_not_awaited()
        nxt = await advance(orch, 'architect', '배포는 병합 이후 별도 작업으로 진행하고 현재 합의 범위의 구현·검증을 마치세요.', 'jev_consult')
        assert nxt[:2] == ('implementer', 'implement')
        assert orch.cfg.state.task['phase'] == 'implement'
        assert not any(e.kind == 'ask' for e in orch.events)
    asyncio.run(run())


def test_blocker_user_ownership_preserves_real_human_wait(orch):
    async def run():
        await agreed(orch)
        routing(orch, 'follow')
        orch.cfg.settings['design_questions'] = True
        orch.design.state['phase'] = 'active'
        orch.jev.route_blocker.return_value = 'human'
        assert await advance(orch, 'implementer', '<!-- duet: REPORT blocked 운영 데이터 삭제에 대한 사용자 승인이 필요합니다 -->', 'implement') is None
        assert orch.cfg.state.task['waiting']
        assert orch.design.blocked('implementer')
        assert '운영 데이터 삭제' in orch.cfg.state.task['wait_reason']
    asyncio.run(run())


def test_blocker_uncertainty_and_dependency_never_create_user_owned_questions(orch):
    async def run():
        await agreed(orch)
        routing(orch, 'follow')
        orch.cfg.settings['design_questions'] = True
        orch.design.state['phase'] = 'active'
        orch.design.defer = Mock()
        report = '<!-- duet: REPORT blocked 선행 작업이 필요합니다 -->'
        for _ in range(2):
            nxt = await advance(orch, 'implementer', report, 'implement')
            assert nxt[:2] == ('implementer', 'implement')
        assert await advance(orch, 'implementer', report, 'implement') is None
        assert orch.cfg.state.task['waiting']
        orch.design.defer.assert_not_called()
        orch.cfg.state.task['waiting'] = False
        orch.jev.route_blocker.return_value = 'dependency'
        assert await advance(orch, 'implementer', '<!-- duet: REPORT blocked 지도 선행 작업 병합 대기 -->', 'implement') is None
        assert orch.cfg.state.task['waiting']
        orch.design.defer.assert_not_called()
        assert not any(e.kind == 'ask' for e in orch.events)
    asyncio.run(run())
