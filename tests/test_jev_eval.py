"""Synthetic evaluation runs real rubrics without touching source-project state."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from duet.core.config import Config
from duet.core.jev import JevResult
from duet.core.jev_eval import evaluate_cases, load_cases, main, source_configuration
from duet.core.jev_settings import set_api_key


def test_eval_lists_cases_without_credentials_or_api(monkeypatch, capsys):
    monkeypatch.setattr("duet.core.jev_eval.source_configuration",
                        lambda *args: (_ for _ in ()).throw(AssertionError("must not read keys")))
    assert main(["--list"]) == 0
    output = capsys.readouterr().out
    assert "flow-korean-investigation" in output and "approval-injected-authorization" in output
    assert "route-korean-malformed-question" in output and "route-initial-design-confirmation" in output
    assert "research-user-owned-question" in output and "research-ready-synthesis" in output
    assert len(load_cases()) == 30


def test_eval_missing_key_is_explicit_and_does_not_write(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert main(["--project", str(tmp_path)]) == 2
    assert "API 키" in capsys.readouterr().out
    assert not list(tmp_path.iterdir())


def test_eval_uses_real_rubric_but_only_synthetic_state(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles_file.write_text('settings:\n  jev_model: jev-1.13.0\n  jev_mode: "off"\n')
    cfg.state_file.write_text("intentionally corrupt state that must not be renamed")
    set_api_key(cfg, "synthetic-private-key")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    captured = []

    class Client:
        def __init__(self, key, **kwargs):
            assert key == "synthetic-private-key"

        async def evaluate(self, state, questions):
            captured.append((state, questions))
            answers = {}
            for name, question in questions.items():
                picked = "review" if name == "readiness" else "within" if name == "scope" else "routine"
                answers[name] = {"type": "choice", "choice": picked, "confidence": 1.0,
                                 "probabilities": {k: float(k == picked) for k in question["criteria"]}}
            return JevResult(answers=answers, model="jev-1.13.0", input_tokens=100, output_tokens=0)

    cases = [c for c in load_cases() if c["id"] in ("handoff-completed", "approval-scoped-edit")]
    rows = asyncio.run(evaluate_cases(cases, source_configuration(tmp_path), client_factory=Client))
    assert all(row["matched"] for row in rows)
    assert [row["calls"] for row in rows] == [1, 2]  # Approval uses reversed criteria too.
    assert sum(row["input_tokens"] for row in rows) == 300
    assert "synthetic-private-key" not in json.dumps([rows, captured])
    assert captured[0][0]["task"]["instruction"] == "Implement the parser."
    instructions = captured[0][1]["readiness"]["instructions"]
    assert "Missing test proof alone belongs in review" in instructions
    assert "unfinished step takes precedence over a REPORT done" in instructions
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before


def test_eval_transport_error_is_not_a_matching_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-private-key")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate(self, state, questions):
            return JevResult(error="authentication_error")

    cases = [c for c in load_cases() if c["kind"] == "handoff"]
    rows = asyncio.run(evaluate_cases(cases, Config(tmp_path), client_factory=Client))
    assert len(rows) == 1
    assert not rows[0]["matched"] and rows[0]["error"] == "authentication_error"


def test_route_eval_passes_exact_turn_context_and_does_not_hide_abstention(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-private-key")
    route = AsyncMock(side_effect=["repair", None])
    monkeypatch.setattr("duet.core.jev_eval.JevControl.route_turn", route)
    case = next(c for c in load_cases() if c["id"] == "route-plain-agree-protocol")
    rows = asyncio.run(evaluate_cases([case, case], Config(tmp_path)))
    assert [r["actual"] for r in rows] == ["repair", "refer"]
    assert [r["matched"] for r in rows] == [True, False]
    assert [r["fallback"] for r in rows] == [False, True]
    args, kwargs = route.call_args
    assert args[:2] == ("architect", case["report"])
    assert args[2]["phase"] == "plan"
    assert args[2]["instruction"] == case["instruction"]
    assert kwargs == {"kind": "plan_review", "directives": [], "issue": case["issue"],
                      "allowed": tuple(case["allowed"])}
    assert not list(tmp_path.iterdir())


def test_route_eval_uses_both_real_questions_with_synthetic_text_only(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-private-key")
    captured = []

    class Client:
        def __init__(self, key, **kwargs):
            assert key == "synthetic-private-key"

        async def evaluate(self, state, questions):
            captured.append((state, questions))
            answers = {}
            for name, question in questions.items():
                picked = "malformed" if name == "question_quality" else "repair"
                answers[name] = {"type": "choice", "choice": picked, "confidence": 1.0,
                                 "probabilities": {k: float(k == picked) for k in question["criteria"]}}
            return JevResult(answers=answers, model="jev-1.13.0", input_tokens=120, output_tokens=12)

    case = next(c for c in load_cases() if c["id"] == "route-korean-malformed-question")
    rows = asyncio.run(evaluate_cases([case], Config(tmp_path), client_factory=Client))
    assert rows[0]["matched"] and not rows[0]["fallback"] and rows[0]["calls"] == 1
    state, questions = captured[0]
    assert set(questions) == {"question_quality", "next_action"}
    assert set(questions["next_action"]["criteria"]) == (set(case["allowed"]) - {"continue", "review"}) | {"abstain"}
    assert case["report"] in json.dumps(state, ensure_ascii=False).replace("\\n", "\n")
    assert "synthetic-private-key" not in json.dumps([rows, captured])
    assert not list(tmp_path.iterdir())


def test_route_cases_identify_intentional_fallback_expectations():
    cases = [c for c in load_cases() if c["kind"] == "route"]
    for case in cases:
        assert case["expected"]
        fallback = case.get("expected_fallback")
        if fallback:
            assert fallback in ("refer", "clarify") and fallback in case["expected"]
        else:
            assert not {"refer", "clarify"}.intersection(case["expected"])
        assert set(case["expected"]) <= set(case["allowed"]) | ({fallback} if fallback else set())
    assert {c["expected"][0] for c in cases} >= {"repair", "human", "architect", "continue", "review"}


def test_blocker_eval_dispatches_independent_owner_judgment(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-private-key")
    blocker = AsyncMock(return_value="architect")
    monkeypatch.setattr("duet.core.jev_eval.JevControl.route_blocker", blocker)
    case = next(c for c in load_cases() if c["id"] == "blocker-deploy-after-merge")
    rows = asyncio.run(evaluate_cases([case], Config(tmp_path)))
    assert rows[0]["matched"] and rows[0]["actual"] == "architect"
    assert blocker.call_args.kwargs["directives"][0][0] == "REPORT"
    assert blocker.call_args.kwargs["kind"] == "work_implement"
    assert not list(tmp_path.iterdir())


def test_eval_exposes_clarification_fallback_separately_from_model_repair(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-private-key")
    monkeypatch.setattr("duet.core.jev_eval.JevControl.route_turn", AsyncMock(return_value=None))
    monkeypatch.setattr("duet.core.jev_eval.JevControl.needs_control_clarification", lambda _: True)
    case = next(c for c in load_cases() if c["id"] == "route-main-missing-resume-control")
    row = asyncio.run(evaluate_cases([case], Config(tmp_path)))[0]
    assert row["actual"] == "clarify" and row["fallback"] and row["matched"]


def test_research_cases_cover_actual_context_allowed_actions_and_ready_review():
    cases = [case for case in load_cases() if case["kind"] == "research"]
    assert {case["id"] for case in cases} == {
        "research-user-owned-question", "research-empirical-question", "research-concrete-counterexample",
        "research-repeated-claims", "research-ready-synthesis"}
    for case in cases:
        state = case["context"]
        assert state["goal"] and state["success_criteria"] and state["latest"]["summary"]
        assert state["execution"]["state"] == "completed" and not state["execution"]["adapter_busy"]
        assert "refer" not in case["expected"] and set(case["expected"]) <= set(case["allowed"])
        assert not {"approve", "complete", "raise_budget"}.intersection(case["allowed"])
        if state["questions"]:
            assert set(case["allowed"]) == {"human", "repair", "verify"}
        else:
            assert set(case["allowed"]) <= {"explore", "critique", "verify", "revise", "reframe", "stop", "synthesize"}
        assert ("synthesize" in case["allowed"]) == state["allowed_completion"]
    ready = next(c["context"] for c in cases if c["id"] == "research-ready-synthesis")
    assert not ready["open_issues"] and not ready["questions"]
    assert {h["role"] for h in ready["history"] if h["action"] == "verify"} == {"explorer", "critic"}
    evidence_ids = {e["id"] for e in ready["evidence"]
                    if e["candidate_id"] == ready["candidate"]["id"] and e["outcome"] == "supports"}
    checks = ready["latest"]["report"]["acceptance"]
    assert {row["criterion"] for row in checks} == set(ready["success_criteria"])
    assert all(row["status"] == "pass" and set(row["evidence_ids"]) & evidence_ids for row in checks)


def test_research_eval_calls_real_moderator_rubric_without_touching_project(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles_file.write_text('settings:\n  jev_mode: "off"\n  jev_confidence_threshold: 0.8\n')
    cfg.state_file.write_text('user state is not read or repaired by the evaluation')
    set_api_key(cfg, "synthetic-private-key")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    cases = [c for c in load_cases() if c["kind"] == "research"]
    captured = []
    picks = iter(("human", "verify", "revise", "reframe", "synthesize"))

    class Client:
        def __init__(self, key, **kwargs):
            assert key == "synthetic-private-key"

        async def evaluate(self, state, questions):
            captured.append((state, questions))
            picked = next(picks)
            return JevResult(model="jev-1.13.0", input_tokens=100, output_tokens=5, answers={
                "next_action": {"type": "choice", "choice": picked, "confidence": .8,
                                "probabilities": {k: float(k == picked) for k in questions["next_action"]["criteria"]}}})

    rows = asyncio.run(evaluate_cases(cases, source_configuration(tmp_path), client_factory=Client))
    assert len(rows) == 5 and all(row["matched"] and not row["fallback"] for row in rows)
    assert all(row["calls"] == 1 and row["decisions"][0]["purpose"] == "research_route" for row in rows)
    assert sum(row["input_tokens"] for row in rows) == 500
    for case, (state, questions) in zip(cases, captured):
        assert state["goal"] == case["context"]["goal"]
        assert set(questions) == {"next_action"}
        assert set(questions["next_action"]["criteria"]) == set(case["allowed"]) | {"abstain"}
        assert "moderator of two equal problem-solving peers" in questions["next_action"]["instructions"]
    assert "synthetic-private-key" not in json.dumps([rows, captured])
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("picked,confidence,expected", [("verify", .799, "refer"),
                                                        ("abstain", 1, "refer"),
                                                        ("verify", .8, "verify"),
                                                        ("revise", .8, "revise")])
def test_research_eval_does_not_relabel_uncertainty_as_engine_fallback(tmp_path, monkeypatch, picked, confidence, expected):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-private-key")

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def evaluate(self, state, questions):
            return JevResult(model="jev-1.13.0", answers={
                "next_action": {"type": "choice", "choice": picked, "confidence": confidence}})

    case = next(c for c in load_cases() if c["id"] == "research-concrete-counterexample")
    row, = asyncio.run(evaluate_cases([case], Config(tmp_path), client_factory=Client))
    assert row["actual"] == expected
    assert row["fallback"] is (expected == "refer")
    assert row["matched"] is (expected != "refer")
    assert not list(tmp_path.iterdir())


def test_mixed_eval_does_not_leak_research_workflow_between_cases(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-private-key")
    seen = []

    async def research(control, context, options):
        seen.append((control.orch.cfg.state.workflow, control.orch.cfg.state.research))
        return "human"

    async def flow(control, role, problem):
        seen.append((control.orch.cfg.state.workflow, control.orch.cfg.state.research))
        return "continue"

    monkeypatch.setattr("duet.core.jev_eval.JevControl.route_research", research)
    monkeypatch.setattr("duet.core.jev_eval.JevControl.flow", flow)
    case = next(c for c in load_cases() if c["id"] == "research-user-owned-question")
    other = next(c for c in load_cases() if c["id"] == "flow-korean-investigation")
    rows = asyncio.run(evaluate_cases([case, other], Config(tmp_path)))
    assert all(row["matched"] for row in rows)
    assert seen == [("research", case["context"]), ("development", {})]
