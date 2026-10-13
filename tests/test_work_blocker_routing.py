"""A technical deployment blocker must not become an unanswered user decision."""
import asyncio
import json
from contextvars import ContextVar
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from duet.core.design_questions import DesignQuestions
from duet.adapters.base import TurnResult
from duet.core.work import REPORT_TRIGGER, WorkBoard, _Wait
from duet.tests.test_jev_work_routing import setup
from duet.tests.test_work_conventions import board, add_item, snapshot


DEPLOY_BLOCKER = "운영 배포는 기준 브랜치 병합 후에만 가능합니다. 작업자는 병합할 수 없습니다."
BLOCKED = f"<!-- duet: REPORT blocked {DEPLOY_BLOCKER} -->"
DONE = "코드와 테스트를 마쳤습니다. 운영 배포는 병합 후 진행합니다.\n<!-- duet: REPORT done -->"


def controller(board, *, action="architect", mode="on"):
    board.orch.jev.mode = mode
    board.orch.jev.enabled = lambda _: mode != "off"
    board.orch.jev.route_blocker = AsyncMock(return_value=action)
    return board.orch.jev


def enable_design_questions(board):
    board.cfg.settings["design_questions"] = True
    board.orch.design = DesignQuestions(board.orch)
    return board.orch.design


def test_deployment_blocker_consults_architect_without_user_question(board):
    it, ad = setup(board, [BLOCKED, DONE], ["follow"])
    jev = controller(board)
    board._answer = AsyncMock(return_value="작업 범위는 코드·검증까지입니다. 배포는 병합 뒤 별도 작업으로 진행합니다.")
    board.orch.record_control_outcome = Mock()
    _, _, dirs = asyncio.run(board._consultable(ad, it, "work_implement", "Implement map", board.project))
    assert dirs == [("REPORT", "done")]
    jev.route_blocker.assert_awaited_once()
    assert jev.route_turn.await_count == 1  # blocker ownership is independent of question quality
    assert DEPLOY_BLOCKER in board._answer.await_args.args[2]
    assert it.consults == 1
    assert board.orch.record_control_outcome.call_args.args == ("architect", "work_consult")


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_disabled_blocker_routing_preserves_report_without_extra_turns(board, mode):
    it, ad = setup(board, [BLOCKED], [])
    jev = controller(board, action=None, mode=mode)
    _, _, dirs = asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    assert dirs == [("REPORT", "blocked " + DEPLOY_BLOCKER)]
    assert board.orch.run_agent_turn.await_count == 1
    jev.route_turn.assert_not_awaited()


def test_uncertain_blocker_requests_bounded_same_author_clarification(board):
    it, ad = setup(board, [BLOCKED] * 3, [])
    jev = controller(board, action=None)
    with pytest.raises(_Wait) as waiting:
        asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    assert waiting.value.kind == "system"
    assert DEPLOY_BLOCKER in waiting.value.reason
    assert board.orch.run_agent_turn.await_count == 3
    assert all(call.args[0] is ad for call in board.orch.run_agent_turn.await_args_list)
    assert "선행 조건" in board.orch.run_agent_turn.await_args_list[1].args[1]
    jev.route_turn.assert_not_awaited()


def test_blocker_dependency_wait_is_not_a_user_question(board):
    it, ad = setup(board, [BLOCKED], [])
    controller(board, action="dependency")
    with pytest.raises(_Wait) as waiting:
        asyncio.run(board._consultable(ad, it, "work_implement", "Implement", board.project))
    assert waiting.value.kind == "dependency"
    assert DEPLOY_BLOCKER in waiting.value.reason


def test_blocker_human_selection_preserves_real_human_wait(board):
    it, ad = setup(board, [BLOCKED], [])
    controller(board, action="human")
    with pytest.raises(_Wait) as waiting:
        asyncio.run(board._consultable(ad, it, "work_implement", "Implement", board.project))
    assert waiting.value.kind == "human"
    assert DEPLOY_BLOCKER in waiting.value.reason


@pytest.mark.parametrize("wait_kind", ["architect", "dependency", "system"])
def test_technical_wait_is_persisted_and_architect_can_resume(board, wait_kind):
    design = enable_design_questions(board)
    it = add_item(board, snapshot(enabled=False))
    it.status = "implement"
    board._implement_loop = AsyncMock(side_effect=_Wait("implement", DEPLOY_BLOCKER, wait_kind))
    board.orch.record_control_outcome = Mock()
    asyncio.run(board._run(it))
    assert it.status == "waiting:implement" and it.wait_kind == wait_kind
    assert not design.blocked("implementer#api")
    assert not design.state.get("pending")
    assert json.loads(board.path.read_text())["items"][0]["wait_kind"] == wait_kind
    restored = WorkBoard(board.orch)
    assert restored.items[it.id].wait_kind == wait_kind
    receipt = board.handle_directives_receipts([("RESUME_WORK", "api 병합 후 별도 배포 작업으로 진행하세요")])[0]
    assert receipt == {"action": "RESUME_WORK", "id": "api", "accepted": True,
                       "before": "waiting:implement", "after": "queued", "reason": "api 재개"}
    assert it.wait_kind == "" and it.inbox == ["설계자: 병합 후 별도 배포 작업으로 진행하세요"]
    record = board.orch.record_control_outcome.call_args
    assert record.kwargs["before"]["status"] == "implement"
    assert record.kwargs["after"]["status"] == "waiting:implement"


def test_real_human_wait_cannot_be_resolved_by_architect_command(board):
    design = enable_design_questions(board)
    it = add_item(board, snapshot(enabled=False))
    it.status = "implement"
    board._implement_loop = AsyncMock(side_effect=_Wait("implement", "요금제를 골라주세요", "human"))
    asyncio.run(board._run(it))
    assert design.blocked("implementer#api") and it.wait_kind == "human"
    receipt = board.handle_directives_receipts([("RESUME_WORK", "api 유료 요금제를 선택했습니다")])[0]
    assert receipt["accepted"] is False
    assert receipt["before"] == receipt["after"] == "waiting:implement"
    assert "실제 사용자" in receipt["reason"] and it.inbox == []


def test_real_human_wait_without_question_mode_still_rejects_model_resume(board):
    it = add_item(board, snapshot(enabled=False))
    it.status, it.wait_kind = "waiting:implement", "human"
    receipt = board.handle_directives_receipts([("RESUME_WORK", "api 제 판단으로 진행")])[0]
    assert not receipt["accepted"] and not it.inbox
    # The UI/user command remains able to explicitly resume the item.
    assert board.resume("api") == "api 재개"


def test_internal_completion_notification_during_jev_does_not_discard_work_turn(board):
    it, ad = setup(board, [DONE], ["review"])
    async def notification(*args, **kwargs):
        board.orch.inbox.put_nowait((None, REPORT_TRIGGER))
        return "review"
    board.orch.jev.route_turn.side_effect = notification
    _, _, dirs = asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    assert dirs == [("REPORT", "done")]
    assert board.orch.inbox.get_nowait() == (None, REPORT_TRIGGER)


def test_pending_real_input_still_prevents_stale_work_transition(board):
    it, ad = setup(board, [DONE], ["review"])
    board.orch.has_pending_user_input = Mock(return_value=True)
    with pytest.raises(_Wait, match="새 사용자") as waiting:
        asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    assert waiting.value.kind == "system"


def test_receipts_expose_missing_and_nonwaiting_work_without_adding_messages(board):
    it = add_item(board, snapshot(enabled=False))
    it.status = "implement"
    receipts = board.handle_directives_receipts([
        ("RESUME_WORK", "absent 새 지시"), ("RESUME_WORK", "api 이미 실행 중"),
        ("CANCEL_WORK", "absent 취소"), ("STATUS", "done")])
    assert len(receipts) == 3 and not any(r["accepted"] for r in receipts)
    assert receipts[0]["before"] is receipts[0]["after"] is None
    assert receipts[1]["before"] == receipts[1]["after"] == "implement"
    assert not it.inbox


def test_cancel_receipt_confirms_state_change_and_text_wrapper_stays_compatible(board):
    it = add_item(board, snapshot(enabled=False))
    board.wt.remove = Mock()
    receipt = board.handle_directives_receipts([("CANCEL_WORK", "api 범위 변경")])[0]
    assert receipt == {"action": "CANCEL_WORK", "id": "api", "accepted": True,
                       "before": "queued", "after": "cancelled", "reason": "api 취소"}
    assert board.handle_directives([("CANCEL_WORK", "api")]) == ["취소할 작업 'api' 가 없습니다."]


def test_early_work_wait_does_not_inherit_parent_decision_log(board):
    entry = ContextVar("early_work_decision", default=None)
    entry.set("main-decision")
    board.orch.jev = SimpleNamespace(_log_entry=entry)
    it = add_item(board, snapshot(error="Convention file is missing"))
    recorded = []
    board.orch.record_control_outcome = lambda *args, **kwargs: recorded.append(entry.get())
    asyncio.run(board._run(it))
    assert it.wait_kind == "system" and recorded == [None]
    assert entry.get() == "main-decision"  # parent context was not modified


def test_each_worker_turn_clears_previous_decision_log_before_execution(board):
    it, ad = setup(board, ["Incomplete report", DONE], ["repair", "follow"])
    entry = ContextVar("turn_decision", default=None)
    entry.set("previous-phase")
    board.orch.jev._log_entry = entry
    seen, responses, actions = [], iter(["Incomplete report", DONE]), iter(["repair", "follow"])
    async def run(*args, **kwargs):
        seen.append(entry.get())
        return TurnResult(next(responses))
    async def route(*args, **kwargs):
        action = next(actions)
        entry.set("decision-" + action)
        return action
    board.orch.run_agent_turn.side_effect = run
    board.orch.jev.route_turn.side_effect = route
    asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    assert seen == [None, None]


def test_failed_consultation_never_records_success(board):
    it, ad = setup(board, [BLOCKED], [])
    controller(board)
    board.orch.record_control_outcome = Mock()
    board._answer = AsyncMock(side_effect=RuntimeError("adapter unavailable"))
    with pytest.raises(RuntimeError, match="adapter unavailable"):
        asyncio.run(board._consultable(ad, it, "work_implement", "Implement", board.project))
    calls = board.orch.record_control_outcome.call_args_list
    assert len(calls) == 1 and calls[0].args[1] == "work_consult_requested"
    assert calls[0].kwargs["applied"] is False


def test_consultation_outcome_stays_with_requesting_decision(board):
    it, ad = setup(board, [BLOCKED, DONE], ["follow"])
    jev = controller(board)
    entry = ContextVar("consult_decision", default=None)
    jev._log_entry = entry
    recorded = []
    board.orch.record_control_outcome = lambda action, stage, **kwargs: recorded.append(
        (stage, kwargs["applied"], entry.get()))
    async def route(*args, **kwargs):
        entry.set("worker-blocker")
        return "architect"
    async def answer(*args, **kwargs):
        entry.set("architect-answer")
        return "Finish the code and review before deployment."
    jev.route_blocker.side_effect = route
    board._answer = AsyncMock(side_effect=answer)
    asyncio.run(board._consultable(ad, it, "work_implement", "Implement", board.project))
    assert recorded == [("work_consult_requested", False, "worker-blocker"),
                        ("work_consult", True, "worker-blocker")]
