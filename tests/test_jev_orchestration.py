"""Jev may route completed turns; existing policy and verification retain authority."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from duet.adapters.base import TurnResult
from duet.core.agreement import new_task
from duet.core.config import Config, Role
from duet.core.dialogue import Turn
from duet.core.events import EventBus
from duet.core.gitops import Git
from duet.core.jev import JevResult
from duet.core.policy import ApprovalRequest, Decision, Policy
from duet.core.work import MAX_REWORK, WorkBoard, WorkItem, _Wait
from duet.tests.test_autopilot_design import make


def answer(**choices):
    return JevResult(model="jev-1.13.0", answers={
        name: {"type": "choice", "choice": option, "confidence": 1.0, "probabilities": {option: 1.0}}
        for name, option in choices.items()
    })


def test_disabled_jev_never_constructs_api_client(tmp_path):
    orch, _ = make(tmp_path, "autopilot")
    orch.jev.client_factory = Mock(side_effect=AssertionError("API must not run"))

    async def run():
        assert await orch.jev.choose("턴 한도", "", [("+10", "더"), ("stop", "멈춤")]) is None
        assert await orch.jev.flow("implementer", "변경 없음") is None
        assert await orch.jev.handoff("implementer", "done", {"phase": "implement"}) is None
        request = ApprovalRequest("implementer", "command", "tool", command="unknown-tool")
        assert await orch.jev.approval(request, orch.policy, "unknown") is None
    asyncio.run(run())
    orch.jev.client_factory.assert_not_called()


def test_shadow_jev_retains_existing_auto_choice(tmp_path):
    orch, _ = make(tmp_path, "autopilot")
    orch.cfg.settings["jev_mode"] = "shadow"
    orch.jev._evaluate = AsyncMock(return_value=answer(next_action="stop"))
    result = asyncio.run(orch.ui.ask_choice("턴 한도", "진행", [("+30", "더"), ("stop", "중단")]))
    assert result == "+30"
    orch.jev._evaluate.assert_awaited_once()


def test_jev_can_replace_fixed_auto_choice(tmp_path):
    orch, _ = make(tmp_path, "autopilot")
    orch.cfg.settings["jev_mode"] = "on"
    orch.jev._evaluate = AsyncMock(return_value=answer(next_action="wait"))
    result = asyncio.run(orch.ui.ask_choice("[api] 재작업 한도", "같은 오류 반복", [("more", "더"), ("wait", "보류")]))
    assert result == "wait"


def test_jev_never_removes_budget_or_repetition_stop(tmp_path):
    orch, _ = make(tmp_path, "autopilot")
    orch.cfg.settings["jev_mode"] = "on"
    orch.jev.choose = AsyncMock(return_value="continue")

    async def run():
        assert await orch.ui.ask_choice("예산 한도", "", [("continue", "해제"), ("stop", "멈춤")]) == "stop"
        orch.ui.counts["재작업 한도"] = 5
        assert await orch.ui.ask_choice("재작업 한도", "", [("continue", "더"), ("wait", "보류")]) == "wait"
    asyncio.run(run())
    orch.jev.choose.assert_not_awaited()


def test_general_mode_human_choice_is_not_automated_by_jev(tmp_path):
    orch, ui = make(tmp_path)
    orch.cfg.settings["jev_mode"] = "on"
    orch.jev.choose = AsyncMock(return_value="more")
    assert asyncio.run(orch.ui.ask_choice("재작업 한도", "", [("more", "더"), ("wait", "보류")])) == "wait"
    assert ui.asked == ["재작업 한도"]
    orch.jev.choose.assert_not_awaited()


@pytest.mark.parametrize("mode,command", [("review", "git push origin main"),
                                         ("autopilot", "git push origin main"),
                                         ("autopilot", "sudo rm build")])
def test_jev_does_not_bypass_human_or_hard_denial(tmp_path, mode, command):
    orch, _ = make(tmp_path, mode)
    orch.cfg.settings["jev_mode"] = "on"
    orch.cfg.policy["ask_architect_opinion_for_human"] = False
    orch.jev.approval = AsyncMock(return_value=Decision(True, "pretend allow", by="jev"))
    result = asyncio.run(orch.handle_approval(ApprovalRequest("implementer", "command", command, command=command)))
    assert not result.allow
    orch.jev.approval.assert_not_awaited()


def test_jev_does_not_bypass_plan_read_only(tmp_path):
    orch, _ = make(tmp_path, "autopilot")
    orch.cfg.settings["jev_mode"] = "on"
    orch.cfg.state.task = new_task("implementer", "Plan the change", None, {})
    orch.jev.approval = AsyncMock(return_value=Decision(True, "pretend allow", by="jev"))
    result = asyncio.run(orch.handle_approval(ApprovalRequest("implementer", "command", "install", command="npm install")))
    assert not result.allow
    orch.jev.approval.assert_not_awaited()


def test_full_auto_jev_can_refuse_previously_unconditional_approval(tmp_path):
    orch, _ = make(tmp_path, "autopilot")
    orch.cfg.settings["jev_mode"] = "on"
    orch.jev.approval = AsyncMock(return_value=Decision(False, "outside task", by="jev"))
    result = asyncio.run(orch.handle_approval(ApprovalRequest("implementer", "command", "tool", command="unknown-tool")))
    assert not result.allow and result.by == "jev"
    orch.jev.approval.assert_awaited_once()


def test_full_auto_uncertain_jev_uses_architect_review(tmp_path):
    orch, _ = make(tmp_path, "autopilot")
    orch.cfg.settings["jev_mode"] = "on"
    orch.jev.approval = AsyncMock(return_value=None)
    orch._ask_architect = AsyncMock(return_value=(Decision(False, "unknown effects", by="architect"), "deny"))
    result = asyncio.run(orch.handle_approval(ApprovalRequest("implementer", "command", "tool", command="unknown-tool")))
    assert not result.allow and result.by == "architect"
    orch._ask_architect.assert_awaited_once()


def test_jev_approval_requires_confident_reversed_option_confirmation(tmp_path):
    orch, _ = make(tmp_path)
    orch.cfg.settings["jev_mode"] = "on"
    orch.current_task["implementer"] = "Run local unit tests for the implementation"
    orch.jev._evaluate = AsyncMock(side_effect=[answer(scope="within", effect="routine"),
                                               answer(scope="unclear", effect="routine")])
    request = ApprovalRequest("implementer", "command", "tests", command="python -m pytest")
    assert asyncio.run(orch.jev.approval(request, orch.policy, "review")) is None
    calls = orch.jev._evaluate.call_args_list
    first, second = calls[0].args[2], calls[1].args[2]
    for name in ("scope", "effect"):
        assert list(first[name]["criteria"]) == list(reversed(second[name]["criteria"]))


def test_jev_low_confidence_does_not_approve(tmp_path):
    orch, _ = make(tmp_path)
    orch.cfg.settings["jev_mode"] = "on"
    orch.current_task["implementer"] = "Run local unit tests for the implementation"
    result = answer(scope="within", effect="routine")
    result.answers["effect"]["confidence"] = .79
    orch.jev._evaluate = AsyncMock(return_value=result)
    request = ApprovalRequest("implementer", "command", "tests", command="python -m pytest")
    assert asyncio.run(orch.jev.approval(request, orch.policy, "review")) is None
    orch.jev._evaluate.assert_awaited_once()


@pytest.mark.parametrize("purpose", ["choice", "approval", "flow", "handoff", "idle"])
@pytest.mark.parametrize("threshold,confidence,accepted", [
    (.8, .8, True), (.8, .799, False), (.9, .85, False), (.7, .75, True),
])
def test_all_jev_decisions_use_configured_confidence(tmp_path, monkeypatch, purpose, threshold, confidence, accepted):
    orch, _ = make(tmp_path)
    orch.cfg.settings.update(jev_mode="on", jev_confidence_threshold=threshold)
    orch.current_task["implementer"] = "Run local unit tests for the implementation"
    result = answer(next_action="stop", scope="within", effect="routine", progress="progress",
                    readiness="review", health="wait")
    for response in result.answers.values():
        response["confidence"] = confidence
    orch.jev._evaluate = AsyncMock(return_value=result)

    async def run():
        if purpose == "choice":
            outcome = await orch.jev.choose("턴 한도", "", [("+10", "더"), ("stop", "중단")])
            assert outcome == ("stop" if accepted else None)
        elif purpose == "approval":
            request = ApprovalRequest("implementer", "command", "tests", command="python -m pytest")
            outcome = await orch.jev.approval(request, orch.policy, "review")
            assert bool(outcome and outcome.allow) == accepted
            assert orch.jev._evaluate.await_count == (2 if accepted else 1)
        elif purpose == "flow":
            outcome = await orch.jev.flow("implementer", "No recent code change")
            assert outcome == ("continue" if accepted else None)
        elif purpose == "handoff":
            outcome = await orch.jev.handoff("implementer", "Implementation complete", {"instruction": "Run tests"})
            assert outcome == ("review" if accepted else None)
        else:
            orch.activity["implementer"] = {"state": "tool", "last_event_at": time.time() - 600}
            adapter = SimpleNamespace(label="implementer", busy=True, interrupt=AsyncMock())
            monkeypatch.setattr("duet.core.jev_control.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()]))
            with pytest.raises(asyncio.CancelledError):
                await orch.jev.watch(adapter)
            assert orch.activity["implementer"].get("jev_health") == ("wait" if accepted else None)
            adapter.interrupt.assert_not_awaited()
    asyncio.run(run())


@pytest.mark.parametrize("field", ["scope", "effect"])
def test_reverse_approval_confirmation_also_respects_configured_threshold(tmp_path, field):
    orch, _ = make(tmp_path)
    orch.cfg.settings.update(jev_mode="on", jev_confidence_threshold=.8)
    orch.current_task["implementer"] = "Run local unit tests for the implementation"
    confirm = answer(scope="within", effect="routine")
    confirm.answers[field]["confidence"] = .79
    orch.jev._evaluate = AsyncMock(side_effect=[answer(scope="within", effect="routine"), confirm])
    request = ApprovalRequest("implementer", "command", "tests", command="python -m pytest")
    assert asyncio.run(orch.jev.approval(request, orch.policy, "review")) is None
    assert orch.jev._evaluate.await_count == 2


@pytest.mark.parametrize("action,expected_phase,expected_role", [("continue", "implement", "implementer"),
                                                               ("review", "verify", "architect"),
                                                               (None, "verify", "architect")])
def test_sequential_handoff_cannot_accept_task(tmp_path, action, expected_phase, expected_role):
    orch, _ = make(tmp_path)
    orch.cfg.state.task = new_task("implementer", "Implement the change", None, {})
    orch.cfg.state.task["phase"] = "implement"
    orch.jev.handoff = AsyncMock(return_value=action)
    turn = Turn("implementer", 4, "", 0, 0, "Report.\n<!-- duet: REPORT done -->")
    result = asyncio.run(orch._decide_task("implementer", "implement", turn))
    assert result[0] == expected_role and result[1] == expected_phase
    assert orch.cfg.state.task["phase"] == expected_phase
    assert orch.cfg.state.task is not None


@pytest.mark.parametrize("change", ["stop", "pause", "settings", "inbox", "turn"])
def test_inflight_jev_result_is_discarded_when_context_changes(tmp_path, change):
    orch, _ = make(tmp_path)
    orch.cfg.settings["jev_mode"] = "on"

    async def evaluate(state, questions):
        if change == "stop":
            orch.stop_requested = True
        elif change == "pause":
            orch.not_paused.clear()
        elif change == "settings":
            orch.cfg.settings["jev_approvals"] = False
        elif change == "inbox":
            orch.inbox.put_nowait((None, "User changed the task"))
        else:
            orch.cfg.state.last_n += 1
        return answer(scope="within")

    orch.jev.client_factory = Mock(return_value=SimpleNamespace(evaluate=evaluate))
    assert asyncio.run(orch.jev._evaluate("approval", {"task": "test"}, {})) is None
    assert orch.jev.calls == 1


@pytest.mark.parametrize("limit", ["budget", "hours"])
def test_jev_does_not_call_api_after_resource_limit(tmp_path, limit):
    orch, _ = make(tmp_path)
    orch.cfg.settings["jev_mode"] = "on"
    if limit == "budget":
        orch.budget_usd = 0
    else:
        orch.max_hours = 0
    orch.jev.client_factory = Mock(side_effect=AssertionError("resource limit"))
    assert asyncio.run(orch.jev.flow("implementer", "No progress")) is None
    orch.jev.client_factory.assert_not_called()


@pytest.mark.parametrize("state", ["tool", "awaiting_approval"])
def test_idle_assessment_never_interrupts_active_tool(tmp_path, monkeypatch, state):
    orch, _ = make(tmp_path)
    orch.cfg.settings["jev_mode"] = "on"
    orch.activity["implementer"] = {"state": state, "last_event_at": time.time() - 600}
    ad = SimpleNamespace(label="implementer", busy=True, interrupt=AsyncMock())
    orch.jev._evaluate = AsyncMock(return_value=answer(health="inspect"))
    monkeypatch.setattr("duet.core.jev_control.asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError()]))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(orch.jev.watch(ad))
    ad.interrupt.assert_not_awaited()
    if state == "tool":
        assert orch.activity["implementer"]["jev_health"] == "inspect"
    else:
        orch.jev._evaluate.assert_not_awaited()


@pytest.fixture
def board(tmp_path):
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles = {"architect": Role("architect", "claude", permissions="read_only"),
                 "implementer": Role("implementer", "codex")}
    paused = asyncio.Event()
    paused.set()
    orch = SimpleNamespace(cfg=cfg, project=tmp_path, git=Git(tmp_path, False), bus=EventBus(),
                           emit_status=lambda: None, inbox=asyncio.Queue(), not_paused=paused,
                           jev=SimpleNamespace(handoff=AsyncMock(return_value=None), record_turn=Mock()))
    obj = WorkBoard(orch)
    obj.wt = SimpleNamespace(in_merge=lambda path: False)
    obj._worker = AsyncMock(return_value=SimpleNamespace(label="implementer#api"))
    obj._verify = AsyncMock(return_value=None)
    obj._merge = AsyncMock(return_value=True)
    obj._consultable = AsyncMock(return_value=(None, "Implemented; test passed.", [("REPORT", "done")]))
    return obj


def item(board):
    it = WorkItem("api", "implementer", "Implement the API", agreed_files=["api.py"],
                  test_command="python -m pytest tests/test_api.py")
    board.items[it.id] = it
    return it


@pytest.mark.parametrize("decision", [None, "review"])
def test_work_handoff_always_runs_existing_verification(board, decision):
    it = item(board)
    board.orch.jev.handoff.return_value = decision
    asyncio.run(board._implement_loop(it, board.project))
    board._verify.assert_awaited_once()
    board._merge.assert_awaited_once()
    role, report, task = board.orch.jev.handoff.call_args.args
    assert role == "implementer#api"
    assert task["instruction"] == it.task
    assert task["agreed_files"] == ["api.py"]
    assert task["test_command"] == it.test_command
    assert task["phase"] == "implement"
    # JEV's review classification cannot itself accept a work item.
    assert it.status == "implement" and it.merged_commit is None


def test_work_handoff_requests_completion_before_review(board):
    it = item(board)
    board.orch.jev.handoff.side_effect = ["continue", "review"]
    asyncio.run(board._implement_loop(it, board.project))
    assert board._consultable.await_count == 2
    assert "아직 끝내지 않은" in board._consultable.call_args.args[3]
    assert it.reworks == 1
    board._verify.assert_awaited_once()
    board._merge.assert_awaited_once()


def test_work_handoff_cannot_loop_forever(board):
    it = item(board)
    board.orch.jev.handoff.return_value = "continue"
    asyncio.run(board._implement_loop(it, board.project))
    assert board.orch.jev.handoff.await_count == 2
    assert board._consultable.await_count == 3
    assert it.reworks == 2
    board._verify.assert_awaited_once()


def test_work_handoff_preserves_rework_limit(board):
    it = item(board)
    it.reworks = MAX_REWORK
    board.orch.jev.handoff.return_value = "continue"
    with pytest.raises(_Wait, match="보완 한도"):
        asyncio.run(board._implement_loop(it, board.project))
    board._verify.assert_not_awaited()
    board._merge.assert_not_awaited()


def test_work_handoff_does_not_override_failed_verification(board):
    it = item(board)
    board.orch.jev.handoff.return_value = "review"
    board._verify.return_value = "The agreed test failed"
    board._consultable.side_effect = [
        (None, "Done.", [("REPORT", "done")]),
        (None, "Cannot fix test.", [("REPORT", "blocked missing dependency")]),
    ]
    with pytest.raises(_Wait, match="blocked"):
        asyncio.run(board._implement_loop(it, board.project))
    board._verify.assert_awaited_once()
    board._merge.assert_not_awaited()


def test_work_explicit_blocker_skips_jev(board):
    it = item(board)
    board._consultable.return_value = (None, "Blocked.", [("REPORT", "blocked needs user input")])
    with pytest.raises(_Wait, match="blocked"):
        asyncio.run(board._implement_loop(it, board.project))
    board.orch.jev.handoff.assert_not_awaited()
    board._verify.assert_not_awaited()


@pytest.mark.parametrize("ok", [True, False])
def test_work_records_only_finished_turns_as_jev_evidence(board, ok):
    it = item(board)
    result = TurnResult("summary", full_text="Full evidence", ok=ok, error="failed" if not ok else None)
    ad = SimpleNamespace(label="implementer#api", run_turn=AsyncMock(return_value=result))
    if ok:
        asyncio.run(board._turn(ad, it, "work_implement", "work", "작업자"))
    else:
        with pytest.raises(RuntimeError, match="failed"):
            asyncio.run(board._turn(ad, it, "work_implement", "work", "작업자"))
    board.orch.jev.record_turn.assert_called_once_with("implementer#api", "work_implement", "Full evidence", ok=ok)


def test_work_approval_includes_agreed_task_scope(board):
    it = item(board)
    it.status = "implement"
    policy = Policy(board.project, board.cfg.policy, board.cfg.main)
    board.orch.handle_approval = AsyncMock(return_value=Decision(True, "allowed"))
    req = ApprovalRequest("implementer#api", "command", "pytest", command=it.test_command)
    asyncio.run(board._approve(it, policy, req))
    assert policy.task["instruction"] == it.task
    assert policy.task["agreed_files"] == it.agreed_files
    assert policy.task["test_command"] == it.test_command
    board.orch.handle_approval.assert_awaited_once_with(req, policy=policy)
