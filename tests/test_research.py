"""Research progress must be backed by evidence and actual user decisions."""
import asyncio
import copy
import json

import pytest

from duet.adapters.base import TurnResult
from duet.core.config import Config
from duet.core.dialogue import Turn
from duet.core.research import (MAX_REPAIRS, ResearchController, parse_report,
                                research_system_prompt, research_turn_prompt)
from duet.tests.test_autopilot_design import make


def setup(tmp_path):
    orch, ui = make(tmp_path, "autopilot")
    orch.cfg.state.workflow = "research"
    orch.cfg.settings["jev_mode"] = "off"
    events = []
    orch.bus.subscribe(events.append)
    n = orch._append_human(None, "두 조건을 만족하는 구체적인 해결 방법을 찾아줘")
    first = orch.research.begin(("explorer", "human", str(n)))
    return orch, ui, events, first


def body(**kwargs):
    report = {"summary": "새로운 결과를 검토했습니다.", **kwargs}
    return report["summary"] + "\n\n```duet-research\n" + json.dumps(report, ensure_ascii=False) + "\n```"


def decide(orch, text=None, role=None, **kwargs):
    s = orch.research.state
    role = role or s.get("active_role", "explorer")
    text = text if text is not None else body(**kwargs)
    n = orch.dialogue.max_number() + 1
    orch.dialogue.append_turn(role, n, text)
    orch.research.dispatched(role, n)
    tr = TurnResult(text, full_text=text)
    turn = orch.dialogue.get(n)
    return asyncio.run(orch.research.decide(role, "research", tr, turn))


def prepared(tmp_path):
    orch, ui, events, first = setup(tmp_path)
    decide(orch, goal="두 조건 충족", success_criteria=["조건 A", "조건 B"], constraints=["외부 변경 금지"])
    decide(orch, candidate="첫 독립 후보: 세부 실행 절차와 검증 방법")
    decide(orch, candidate="다른 독립 후보: 다른 문제 분해")
    return orch, ui, events


def evidence(ident="e1", result="A와 B를 만족하는 논증과 계산 결과"):
    return {"id": ident, "claim": "두 조건을 충족한다", "result": result,
            "source": "docs/proof.md의 계산 과정과 재현 명령", "outcome": "supports"}


def acceptance(ident="e1"):
    return [{"criterion": c, "status": "pass", "evidence_ids": [ident]} for c in ("조건 A", "조건 B")]


def issue(status="open", ids=None):
    return {"id": "i1", "claim": "경계 조건이 빠졌다", "impact": "목표 조건 B를 만족하지 못할 수 있다",
            "check": "경계 사례를 검증한다", "status": status, "evidence_ids": ids or []}


def test_initial_goal_then_independent_equal_peers(tmp_path):
    orch, _, _, first = setup(tmp_path)
    assert first[:2] == ("explorer", "research") and "clarify" in first[2]
    nxt = decide(orch, goal="두 조건 충족", success_criteria=["조건 A", "조건 B"])
    assert nxt[0] == "explorer" and "independent_explorer" in nxt[2]
    nxt = decide(orch, candidate="SECRET_EXPLORER_DRAFT")
    assert nxt[0] == "critic" and "independent_critic" in nxt[2]
    assert "SECRET_EXPLORER_DRAFT" not in nxt[2]
    nxt = decide(orch, candidate="다른 독립 후보")
    assert nxt[0] == "critic" and "critique" in nxt[2]
    assert "SECRET_EXPLORER_DRAFT" in nxt[2] and "다른 독립 후보" in nxt[2]
    assert orch.research.state["candidate_id"] == "c1"
    assert orch.research.state["initial_views"] == ["explorer", "critic"]


def test_required_questions_wait_even_in_full_auto_and_resume_real_answer(tmp_path):
    orch, ui, events, _ = setup(tmp_path)
    question = {"question": "두 조건 중 무엇을 우선할까요?", "why": "상충할 때 우선순위가 필요합니다", "owner": "user"}
    assert decide(orch, questions=[question]) is None
    assert orch.full_auto and not ui.asked
    assert orch.research.state["phase"] == "awaiting_user"
    assert len([e for e in events if e.kind == "ask"]) == 1
    tr = TurnResult(body(summary="제가 임의로 A로 정합니다"))
    assert asyncio.run(orch.research.decide("explorer", "research", tr, Turn("explorer", 5, "", 0, 0, tr.text))) is None
    assert orch.research.state["phase"] == "awaiting_user"
    n = orch._append_human(None, "조건 A를 우선해")
    nxt = orch.research.begin(("explorer", "human", str(n)))
    assert "조건 A를 우선해" in nxt[2] and question["question"] in nxt[2]
    decide(orch, goal="우선순위 A로 해결", success_criteria=["조건 A 충족"], constraints=["A 우선"])
    assert orch.research.state["questions"] == []
    cfg = Config(tmp_path)
    cfg.load()
    assert cfg.state.research["goal"] == "우선순위 A로 해결"


def test_moderator_sends_researchable_question_to_verification(tmp_path):
    orch, _, events, _ = setup(tmp_path)
    seen = []

    async def route(context, options):
        seen.append((context, options))
        return "verify"

    orch.jev.route_research = route
    nxt = decide(orch, goal="속도 확인", success_criteria=["실측값 확인"],
                 questions=[{"question": "이 알고리즘은 충분히 빠른가요?", "why": "실제 실행 시간이 불명확합니다", "owner": "user"}])
    assert seen[0][1] == ["human", "repair", "verify"]
    assert nxt[0] == "critic" and "이 알고리즘" in nxt[2]
    assert orch.research.state["stage"] == "verify"
    assert not [e for e in events if e.kind == "ask"]


def test_malformed_question_is_repaired_before_user_wait(tmp_path):
    orch, _, events, _ = setup(tmp_path)
    nxt = decide(orch, questions=[{"question": "뭘까요", "why": "", "owner": "user"}])
    assert nxt[0] == "explorer" and orch.research.state["stage"] == "repair"
    assert not [e for e in events if e.kind == "ask"]


def test_question_routing_can_request_specific_repair(tmp_path):
    orch, _, events, _ = setup(tmp_path)

    async def route(context, options):
        return "repair"

    orch.jev.route_research = route
    nxt = decide(orch, questions=[{"question": "골라주세요", "why": "결정 필요", "owner": "user"}])
    assert nxt[0] == "explorer" and "질문 보완" in nxt[2]
    assert not [e for e in events if e.kind == "ask"]


def test_agreement_without_evidence_does_not_complete(tmp_path):
    orch, _, _ = prepared(tmp_path)

    async def route(context, options):
        assert "synthesize" not in options
        assert "human" not in options and "repair" not in options
        return "stop"

    orch.jev.route_research = route
    nxt = decide(orch, assessment="supported", summary="둘 다 동의합니다")
    assert "synthesize" in nxt[2]
    assert decide(orch, summary="합의했지만 검증하지 못했습니다") is None
    assert orch.research.state["phase"] == "paused"
    assert not orch.research.snapshot()["completion_ready"]


def test_unsupported_early_synthesis_choice_continues_investigation(tmp_path):
    orch, _, _ = prepared(tmp_path)

    async def route(context, options):
        assert "synthesize" not in options
        return "synthesize"  # A stale/invalid moderator choice is not executable.

    orch.jev.route_research = route
    nxt = decide(orch, summary="아직 근거가 필요합니다")
    assert nxt and orch.research.state["stage"] == "verify"
    assert orch.research.state["phase"] == "running"
    assert not orch.research.snapshot()["completion_ready"]


def test_current_candidate_criteria_evidence_and_both_reviews_allow_completion(tmp_path):
    orch, _, _ = prepared(tmp_path)
    decide(orch, evidence=[evidence()], assessment="supported", acceptance=acceptance())
    assert not orch.research.snapshot()["completion_ready"]
    nxt = decide(orch, assessment="supported", acceptance=acceptance())
    assert orch.research.snapshot()["completion_ready"]
    assert "synthesize" in nxt[2]
    assert decide(orch, summary="근거와 해결 절차, 한계를 최종 정리했습니다") is None
    assert orch.research.state["phase"] == "completed"
    assert orch.research.snapshot()["best_candidate"]["id"] == "c1"


@pytest.mark.parametrize("withdrawal", [
    {"assessment": "needs_work"},
    {"acceptance": [{"criterion": "조건 A", "status": "fail", "evidence_ids": []}]},
    {"acceptance": [{"criterion": "조건 B", "status": "unknown", "evidence_ids": []}]},
])
def test_synthesis_can_withdraw_prior_supported_review(tmp_path, withdrawal):
    orch, _, _ = prepared(tmp_path)
    decide(orch, evidence=[evidence()], assessment="supported", acceptance=acceptance())
    decide(orch, assessment="supported", acceptance=acceptance())
    assert orch.research.snapshot()["completion_ready"]
    assert orch.research.state["stage"] == "synthesize"
    assert decide(orch, summary="최종 검토에서 충족하지 못한 조건을 발견했습니다", **withdrawal) is None
    assert orch.research.state["phase"] == "paused"
    assert not orch.research.snapshot()["completion_ready"]
    assert orch.research.snapshot()["best_candidate"] is None
    assert orch.research.snapshot()["candidate"]["id"] == "c1"


def test_new_refutation_in_synthesis_invalidates_previous_peer_reviews(tmp_path):
    orch, _, _ = prepared(tmp_path)
    decide(orch, evidence=[evidence()], assessment="supported", acceptance=acceptance())
    decide(orch, assessment="supported", acceptance=acceptance())
    refuted = {**evidence("e2", "경계 입력에서 조건 B를 충족하지 못했습니다"), "outcome": "refutes"}
    assert decide(orch, summary="최종 확인 중 실제 반례 발견", evidence=[refuted]) is None
    assert orch.research.state["phase"] == "paused"
    assert orch.research.state["reviews"] == {}
    assert not orch.research.snapshot()["completion_ready"]
    assert orch.research.snapshot()["best_candidate"] is None
    assert [e["outcome"] for e in orch.research.state["evidence"]] == ["supports", "refutes"]


def test_unsupported_pass_and_partial_mutation_are_rejected_atomically(tmp_path):
    orch, _, _ = prepared(tmp_path)
    before = copy.deepcopy(orch.research.state)
    nxt = decide(orch, candidate="검증 없이 바꾼 후보", acceptance=acceptance("nonexistent"))
    assert nxt and orch.research.state["stage"] == "repair"
    for key in ("candidates", "candidate_id", "reviews", "evidence", "turns"):
        assert orch.research.state[key] == before[key]


def test_changed_candidate_reopens_resolved_issues_and_preserves_best(tmp_path):
    orch, _, _ = prepared(tmp_path)
    decide(orch, evidence=[evidence()], issues=[issue("resolved", ["e1"])],
           assessment="supported", acceptance=acceptance())
    decide(orch, assessment="supported", acceptance=acceptance())
    assert orch.research.snapshot()["completion_ready"]
    # A fresh alternative is a discussion step, not a replacement for best.
    orch.research._next("explorer", "explore")
    decide(orch, candidate="새롭지만 미검증인 후보")
    assert orch.research.state["issues"][0]["status"] == "open"
    assert orch.research.state["issues"][0]["evidence_ids"] == []
    assert not orch.research.snapshot()["completion_ready"]
    assert orch.research.snapshot()["best_candidate"]["id"] == "c1"
    assert orch.research.snapshot()["candidate"]["id"] == "c3"


def test_successful_criteria_cannot_be_weakened_by_peer(tmp_path):
    orch, _, _ = prepared(tmp_path)
    nxt = decide(orch, goal="쉬운 목표", success_criteria=["대충 답하기"])
    assert nxt and "목표" in nxt[2]
    assert orch.research.state["goal"] == "두 조건 충족"
    assert orch.research.state["success_criteria"] == ["조건 A", "조건 B"]


def test_protocol_failures_have_bounded_recovery(tmp_path):
    orch, _, _, _ = setup(tmp_path)
    for _ in range(MAX_REPAIRS):
        assert decide(orch, text="이전 대화처럼 STATUS done")
    assert decide(orch, text="여전히 형식 없음") is None
    assert orch.research.state["phase"] == "paused"
    assert orch.research.state["turns"] == 0


def test_repeated_discussion_reframes_then_pauses_unresolved(tmp_path):
    orch, _, _ = prepared(tmp_path)
    stages = []
    for _ in range(12):
        nxt = decide(orch, summary="같은 주장을 반복합니다")
        stages.append(orch.research.state["stage"])
        if nxt is None:
            break
    assert "reframe" in stages and "synthesize" in stages
    assert orch.research.state["phase"] == "paused"
    assert not orch.research.snapshot()["completion_ready"]


def test_old_model_turn_and_moderator_choice_are_not_applied_after_user_input(tmp_path):
    orch, _, events = prepared(tmp_path)
    before = orch.research.state["turns"]
    orch.submit("새 제약을 추가해")
    assert decide(orch, candidate="이전 목표의 후보") is None
    assert orch.research.state["turns"] == before
    assert any(e.kind == "control_outcome" and e.data["stage"] == "superseded" for e in events)


def test_stale_moderator_result_cannot_dispatch_or_mark_applied(tmp_path):
    orch, _, events = prepared(tmp_path)

    async def route(context, options):
        orch.stop_requested = True
        return "explore"

    orch.jev.route_research = route
    assert decide(orch, summary="검토 결과") is None
    assert orch.research.state["phase"] == "paused"
    assert not any(e.kind == "control_outcome" and e.data["action"] == "research_explore"
                   and e.data["applied"] for e in events)


def test_pause_during_moderation_waits_then_discards_and_rechecks_choice(tmp_path):
    orch, _, events = prepared(tmp_path)

    async def run():
        paused = asyncio.Event()
        calls = []

        async def route(context, options):
            calls.append(context)
            if len(calls) == 1:
                orch.pause()
                paused.set()
                return "stop"
            return "verify"

        orch.jev.route_research = route
        text = body(summary="두 주장을 판별할 실험이 필요합니다")
        turn = Turn("critic", 10, "", 0, 0, text)
        task = asyncio.create_task(orch.research.decide("critic", "research", TurnResult(text), turn))
        await paused.wait()
        assert not task.done() and orch.research.state["phase"] == "running"
        orch.resume()
        nxt = await task
        assert len(calls) == 2 and calls[0] == calls[1]
        assert nxt and orch.research.state["stage"] == "verify"
        assert not orch.research.state.get("requested_stop")

    asyncio.run(run())
    assert any(e.kind == "control_outcome" and e.data["stage"] == "superseded"
               and "일시정지" in e.data["reason"] for e in events)


def test_pause_during_question_moderation_does_not_ask_stale_question(tmp_path):
    orch, _, events, _ = setup(tmp_path)

    async def run():
        paused = asyncio.Event()
        calls = 0

        async def route(context, options):
            nonlocal calls
            calls += 1
            if calls == 1:
                orch.pause()
                paused.set()
                return "human"
            return "verify"

        orch.jev.route_research = route
        text = body(goal="실측 확인", success_criteria=["실제 측정"],
                    questions=[{"question": "실제 성능은 어떤가요?", "why": "아직 측정하지 않았습니다", "owner": "user"}])
        task = asyncio.create_task(orch.research.decide("explorer", "research", TurnResult(text),
                                                       Turn("explorer", 2, "", 0, 0, text)))
        await paused.wait()
        assert not task.done()
        orch.resume()
        assert await task
        assert calls == 2 and orch.research.state["stage"] == "verify"

    asyncio.run(run())
    assert not [e for e in events if e.kind == "ask"]


@pytest.mark.parametrize("supersede", ["stop", "user_input"])
def test_stop_or_new_input_during_moderator_pause_supersedes_request(tmp_path, supersede):
    orch, _, _ = prepared(tmp_path)

    async def run():
        paused = asyncio.Event()
        calls = 0

        async def route(context, options):
            nonlocal calls
            calls += 1
            orch.pause()
            paused.set()
            return "verify"

        orch.jev.route_research = route
        text = body(summary="검증할 내용")
        task = asyncio.create_task(orch.research.decide("critic", "research", TurnResult(text),
                                                       Turn("critic", 10, "", 0, 0, text)))
        await paused.wait()
        if supersede == "stop":
            await orch.stop()
        else:
            orch.submit("새로운 제약을 반영해")
            orch.resume()
        assert await task is None
        assert calls == 1 and orch.research.state["phase"] == "paused"

    asyncio.run(run())


def test_repeated_moderator_pauses_have_bounded_reevaluation(tmp_path):
    orch, _, _ = prepared(tmp_path)

    async def run():
        paused = asyncio.Queue()
        calls = 0

        async def route(context, options):
            nonlocal calls
            calls += 1
            orch.pause()
            paused.put_nowait(calls)
            return "stop"

        orch.jev.route_research = route
        text = body(summary="검증이 필요합니다")
        task = asyncio.create_task(orch.research.decide("critic", "research", TurnResult(text),
                                                       Turn("critic", 10, "", 0, 0, text)))
        for expected in (1, 2):
            assert await paused.get() == expected
            assert not task.done()
            orch.resume()
        assert await task
        assert calls == 2 and orch.research.state["stage"] == "verify"
        assert orch.research.state["moderator"] == "fallback"

    asyncio.run(run())


def test_route_selection_is_not_logged_as_dispatch(tmp_path):
    orch, _, events = prepared(tmp_path)
    nxt = decide(orch, summary="반례를 확인해야 합니다")
    routed = [e for e in events if e.kind == "control_outcome" and e.data["stage"] == "routed"]
    assert routed and all(not e.data["applied"] for e in routed)
    count = len([e for e in events if e.kind == "control_outcome" and e.data["stage"] == "dispatched"])
    orch.research.pause("사용자 중단")
    orch.research.dispatched(nxt[0], 99)
    assert count == len([e for e in events if e.kind == "control_outcome" and e.data["stage"] == "dispatched"])


def test_dispatch_and_report_result_keep_original_moderator_log_entry(tmp_path):
    orch, _, _ = prepared(tmp_path)
    records = []
    orch.record_control_outcome = lambda action, stage, **kw: records.append(
        (action, stage, orch.jev._log_entry.get(), kw.get("applied")))

    async def route(context, options):
        orch.jev._log_entry.set("original-decision")
        return "verify"

    orch.jev.route_research = route
    nxt = decide(orch, summary="판별 실험이 필요합니다")
    orch.jev._log_entry.set("unrelated-decision")
    assert decide(orch, text="형식이 잘못된 응답", role=nxt[0])
    assert ("research_verify", "dispatched", "original-decision", True) in records
    assert ("research_turn", "rejected", "original-decision", False) in records
    assert orch.jev._log_entry.get() == "unrelated-decision"


def test_restart_and_empty_state_never_start_work(tmp_path):
    orch, _, _ = prepared(tmp_path)
    restored = ResearchController(orch)
    assert restored.snapshot()["phase"] == "paused"
    assert "재시작" in restored.snapshot()["wait_reason"]
    assert restored.begin(None) is None
    orch.cfg.state.research = {}
    restored.pause("일반 종료")
    assert restored.snapshot()["phase"] == "idle"


def test_malformed_saved_state_is_paused_with_diagnostic(tmp_path):
    orch, _, _, _ = setup(tmp_path)
    orch.cfg.state.research = {"phase": "running", "evidence": [None], "reviews": 3}
    restored = ResearchController(orch)
    assert restored.snapshot()["phase"] == "paused"
    assert "형식" in restored.snapshot()["wait_reason"]
    assert restored.snapshot()["evidence"] == []


def test_failed_model_call_retries_original_purpose_then_stops(tmp_path):
    orch, _, _, _ = setup(tmp_path)
    tr = TurnResult("", ok=False, error="CLI terminated")
    turn = Turn("explorer", 2, "", 0, 0, "")
    for _ in range(MAX_REPAIRS):
        nxt = asyncio.run(orch.research.decide("explorer", "research", tr, turn))
        assert nxt and "CLI terminated" in nxt[2]
    assert asyncio.run(orch.research.decide("explorer", "research", tr, turn)) is None
    assert orch.research.state["phase"] == "paused"


@pytest.mark.parametrize("text", ["plain answer", body() + "\n" + body(),
                                  body() + "\n<!-- duet: STATUS done -->",
                                  '```duet-research\n{"summary": "x", "questions": "bad"}\n```'])
def test_invalid_protocol_is_not_silently_accepted(text):
    with pytest.raises(ValueError):
        parse_report(text)


def test_research_prompts_use_peer_protocol_and_no_automatic_dialogue_read(tmp_path):
    orch, _, _, _ = setup(tmp_path)
    role = orch.cfg.roles["critic"]
    system = research_system_prompt(orch.cfg, role)
    prompt = research_turn_prompt(orch.cfg, role, 2, 1, "research", "independent_critic")
    assert "동등한 책임" in system and "duet-research" in prompt
    assert "#1 이후 내용을 읽으세요" not in prompt
    assert "직접 쓰지 마세요" in prompt


def test_fake_research_demo_reports_unresolved_not_fake_success(tmp_path, monkeypatch):
    import duet.adapters.fake as fake_module
    monkeypatch.setattr(fake_module, "DELAY", 0)
    orch, _, _, first = setup(tmp_path)
    orch.cfg.state.max_turns = None
    asyncio.run(orch.run(first))
    assert orch.research.state["phase"] == "paused"
    assert not orch.research.snapshot()["completion_ready"]
    assert any("시뮬레이션" in t.body for t in orch.dialogue.turns() if t.role == "critic")
