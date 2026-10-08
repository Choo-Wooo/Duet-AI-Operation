"""Semantic routes preserve author, phase, human gates and existing review checks."""
import asyncio
from unittest.mock import AsyncMock

from duet.tests.test_agreement import PLAN, advance, agreed, orch, start, submit_plan


def routing(orch, action):
    orch.cfg.settings.update(jev_mode='on', jev_flow=True)
    orch.jev.route_turn = AsyncMock(return_value=action)
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
