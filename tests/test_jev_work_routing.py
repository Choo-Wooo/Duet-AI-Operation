"""JEV fixes the emitting work session and never substitutes acceptance or a user answer."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from duet.adapters.base import TurnResult
from duet.core.dialogue import extract_directives
from duet.core.work import MAX_CONSULTS, MAX_ROUTE_REPAIRS, _Wait
from duet.tests.test_work_conventions import board, add_item, snapshot
from duet.tests.test_agreement import PLAN


def setup(board, responses, actions, *, phase="implement", architect=False):
    it = add_item(board, snapshot(enabled=False))
    it.status = phase
    ad = SimpleNamespace(label=("architect" if architect else "implementer") + "#api",
                         plan_read_only=phase in ("plan", "plan_review"))
    board.orch.run_agent_turn = AsyncMock(side_effect=[TurnResult(t, full_text=t) for t in responses])
    board.orch.jev = SimpleNamespace(route_turn=AsyncMock(side_effect=actions), record_turn=Mock(),
                                    handoff=AsyncMock(return_value="review"))
    return it, ad


def test_reviewer_format_repair_stays_with_reviewer_and_keeps_counters(board):
    it, ad = setup(board, ["검토 결과\n계획에 동의합니다.", "동의합니다.\n<!-- duet: AGREE -->"],
                   ["repair", "follow"], phase="plan_review", architect=True)
    result = asyncio.run(board._turn(ad, it, "work_review", "Review the plan", "설계자"))
    assert result[2] == [("AGREE", "")]
    assert board.orch.run_agent_turn.await_count == 2
    assert all(c.args[0] is ad for c in board.orch.run_agent_turn.await_args_list)
    assert it.reworks == it.negotiations == it.rounds == 0
    calls = board.orch.jev.route_turn.await_args_list
    assert calls[0].kwargs["issue"] and not calls[1].kwargs["issue"]
    assert "review" not in calls[0].kwargs["allowed"]


def test_malformed_question_repair_is_bounded_and_keeps_original(board):
    body = "<!-- duet: ASK_HUMAN 어떻게? -->"
    it, ad = setup(board, [body] * 3, ["repair"] * 3)
    with pytest.raises(_Wait, match="어떻게"):
        asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    assert board.orch.run_agent_turn.await_count == MAX_ROUTE_REPAIRS + 1
    assert it.reworks == 0


def test_worker_technical_question_goes_to_architect(board):
    body = "<!-- duet: ASK_HUMAN 정해진 API의 반환값은 무엇인가요? -->"
    it, ad = setup(board, [body, "구현 완료\n<!-- duet: REPORT done -->"], ["architect", "review"])
    board._answer = AsyncMock(return_value="정해진 반환값은 7입니다.")
    tr, full, dirs = asyncio.run(board._consultable(ad, it, "work_implement", "Implement", board.project))
    assert dirs == [("REPORT", "done")] and tr.jev_route == "review"
    board._answer.assert_awaited_once_with(it, "architect", "정해진 API의 반환값은 무엇인가요?", board.project)
    assert "정해진 반환값은 7" in board.orch.run_agent_turn.await_args_list[1].args[1]


def test_user_question_waits_with_original_text_even_without_tag(board):
    body = "기존 데이터를 삭제할까요? A: 보존(추천), B: 삭제하면 복원할 수 없습니다."
    it, ad = setup(board, [body], ["human"])
    with pytest.raises(_Wait, match="기존 데이터를 삭제"):
        asyncio.run(board._consultable(ad, it, "work_implement", "Implement", board.project))
    assert board.orch.run_agent_turn.await_count == 1


def test_question_routing_fallback_preserves_existing_user_wait(board):
    body = "<!-- duet: ASK_HUMAN 데이터 보관 기간은 얼마인가요? -->"
    it, ad = setup(board, [body], [None])
    with pytest.raises(_Wait, match="보관 기간"):
        asyncio.run(board._consultable(ad, it, "work_implement", "Implement", board.project))
    assert board.orch.run_agent_turn.await_count == 1


def test_completion_routing_requires_existing_tests_and_review(board):
    it, ad = setup(board, ["요구한 코드를 구현했습니다."], ["review"])
    board._worker = AsyncMock(return_value=ad)
    board.wt.in_merge = lambda _: False
    board._verify = AsyncMock(return_value=None)
    board._merge = AsyncMock(return_value=True)
    asyncio.run(board._implement_loop(it, board.project))
    board._verify.assert_awaited_once_with(it, board.project, "요구한 코드를 구현했습니다.")
    board._merge.assert_awaited_once()
    board.orch.jev.handoff.assert_not_awaited()


@pytest.mark.parametrize("directive", ["ASK_HUMAN 결제할까요?", "REPORT blocked 권한 없음", "REPORT deviation 새 파일 필요"])
def test_review_cannot_erase_explicit_blocker(board, directive):
    body = f"<!-- duet: {directive} -->"
    it, ad = setup(board, [body], ["review"])
    _, _, dirs = asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    assert dirs == extract_directives(body)
    assert not {"continue", "review"}.intersection(board.orch.jev.route_turn.await_args.kwargs["allowed"])


def test_cancel_during_routing_never_starts_repair(board):
    it, ad = setup(board, ["bad"], ["repair"])
    async def cancelled(*args, **kwargs):
        it.status = "cancelled"
        return "repair"
    board.orch.jev.route_turn.side_effect = cancelled
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    assert board.orch.run_agent_turn.await_count == 1


@pytest.mark.parametrize("bad", ["architect", "absent question", "api question"])
def test_bad_consult_is_repaired_before_consuming_consult_count(board, bad):
    it, ad = setup(board, [f"<!-- duet: CONSULT {bad} -->", "<!-- duet: CONSULT architect Which API? -->",
                          "<!-- duet: REPORT done -->"], [None] * 3)
    board._answer = AsyncMock(return_value="Use the existing API.")
    asyncio.run(board._consultable(ad, it, "work_implement", "Implement", board.project))
    assert it.consults == 1
    board._answer.assert_awaited_once_with(it, "architect", "Which API?", board.project)


def test_invalid_consult_repair_is_bounded(board):
    it, ad = setup(board, ["<!-- duet: CONSULT absent question -->"] * 3, [None] * 3)
    with pytest.raises(_Wait, match="협의 질문 형식 보완 한도"):
        asyncio.run(board._consultable(ad, it, "work_implement", "Implement", board.project))
    assert it.consults == 0 and board.orch.run_agent_turn.await_count == 3


def test_consult_limit_stops_instead_of_requesting_endless_turns(board):
    it, ad = setup(board, ["<!-- duet: CONSULT architect Which API? -->"], [None])
    it.consults = MAX_CONSULTS
    board._answer = AsyncMock()
    with pytest.raises(_Wait, match="협의 한도"):
        asyncio.run(board._consultable(ad, it, "work_implement", "Implement", board.project))
    board._answer.assert_not_awaited()
    assert board.orch.run_agent_turn.await_count == 1


def test_architect_consult_human_question_is_not_treated_as_answer(board):
    body = "<!-- duet: ASK_HUMAN 외부 서비스 요금제를 골라주세요 -->"
    it, ad = setup(board, [body], [None], architect=True)
    board._architect = AsyncMock(return_value=ad)
    with pytest.raises(_Wait, match="요금제"):
        asyncio.run(board._answer(it, "architect", "어느 외부 API?", board.project))


def test_plan_repair_stays_readonly_and_parses_repaired_plan(board):
    it, ad = setup(board, ["계획입니다.", PLAN], ["repair", "follow"], phase="plan")
    _, _, dirs = asyncio.run(board._turn(ad, it, "work_plan", "Plan", "implementer"))
    assert ("PLAN", "ready") in dirs and ad.plan_read_only
    assert it.negotiations == 0


def test_budget_reached_during_route_does_not_start_more_turns(board):
    it, ad = setup(board, ["bad"], ["repair"])
    board.orch.budget_usd, board.orch.cost_usd = 1, 0
    async def expensive(*args, **kwargs):
        board.orch.cost_usd = 1
        return "repair"
    board.orch.jev.route_turn.side_effect = expensive
    with pytest.raises(_Wait, match="예산"):
        asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    assert board.orch.run_agent_turn.await_count == 1


def test_continue_finishes_in_same_worker_without_code_rework_counter(board):
    it, ad = setup(board, ["본문 작업이 한 군데 남아 직접 마무리하겠습니다.\n<!-- duet: REPORT done -->",
                          "코드와 테스트를 마쳤습니다.\n<!-- duet: REPORT done -->"], ["continue", "review"])
    tr, _, dirs = asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    assert tr.jev_route == "review" and dirs == [("REPORT", "done")]
    assert it.reworks == 0 and board.orch.run_agent_turn.await_count == 2


def test_new_input_during_routing_preserved_without_stale_transition(board):
    it, ad = setup(board, ["완료했습니다."], ["review"])
    async def new_message(*args, **kwargs):
        it.inbox.append("변경 범위를 다시 확인해 주세요.")
        return "review"
    board.orch.jev.route_turn.side_effect = new_message
    with pytest.raises(_Wait, match="새 사용자"):
        asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    assert it.inbox == ["변경 범위를 다시 확인해 주세요."]
    assert board.orch.run_agent_turn.await_count == 1


def test_conflicting_report_directives_are_presented_as_protocol_issue(board):
    body = "<!-- duet: REPORT done -->\n<!-- duet: REPORT blocked missing data -->"
    it, ad = setup(board, [body, "<!-- duet: ASK_HUMAN 필요한 데이터 파일은 어디 있나요? -->"],
                   ["repair", "human"])
    _, _, dirs = asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    assert dirs == [("ASK_HUMAN", "필요한 데이터 파일은 어디 있나요?")]
    assert board.orch.jev.route_turn.await_args_list[0].kwargs["issue"]
