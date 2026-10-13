"""Blocker ownership and concurrent-routing guards use simulated decisions."""
import asyncio
import json

import pytest

from duet.core.jev import JevResult
from duet.core.jev_control import EVIDENCE_RULE
from duet.core.work import REPORT_TRIGGER, WorkItem
from duet.tests.test_jev_control import api, control, judgment


def blocker(control, text="REPORT blocked: 배포 전 병합이 필요합니다. 작업 순서를 조정해야 합니다.",
            role="implementer", **kwargs):
    return asyncio.run(control.route_blocker(role, text, **kwargs))


@pytest.mark.parametrize("owner", ["architect", "human", "dependency", "repair"])
def test_blocker_ownership_does_not_require_question_quality(control, owner):
    client = api(control, judgment(owner=owner))
    events = []
    control.orch.bus.subscribe(events.append)
    assert blocker(control, kind="implement", directives=[("REPORT", "blocked 병합 전 배포 불가")]) == owner
    state, questions = client.evaluate.call_args.args
    assert set(questions) == {"owner"}
    assert set(questions["owner"]["criteria"]) == {"architect", "human", "dependency", "repair", "abstain"}
    assert state["turn_kind"] == "implement" and state["parsed_directives"][0][0] == "REPORT"
    assert "question-format" in questions["owner"]["instructions"]
    assert questions["owner"]["instructions"].startswith(EVIDENCE_RULE)
    outcome, = [event.data for event in events if event.kind == "jev_outcome"]
    assert outcome["purpose"] == "blocker_route" and outcome["applied"] is False
    log = (control.orch.cfg.project / ".duet/logs/jev/DIALOGUE.md").read_text()
    assert '"관제 적용": false' in log and '"관제 적용": true' not in log


@pytest.mark.parametrize("result", [judgment(owner="abstain"), judgment(owner="approve_deploy"),
                                   judgment(confidence=.799, owner="human"), judgment(), JevResult(error="timeout")])
def test_inconclusive_blocker_never_creates_human_wait_or_permission(control, result):
    api(control, result)
    assert blocker(control) is None


def test_blocker_threshold_accepts_point_eight_but_does_not_lower_setting(control):
    control.orch.cfg.settings["jev_confidence_threshold"] = .8
    api(control, judgment(confidence=.8, owner="architect"))
    assert blocker(control) == "architect"
    control.orch.cfg.settings["jev_confidence_threshold"] = .9
    api(control, judgment(confidence=.8, owner="architect"))
    assert blocker(control) is None


@pytest.mark.parametrize("mode", ["off", "shadow", "flow_disabled"])
def test_disabled_and_shadow_blocker_do_not_actuate(control, mode):
    client = api(control, judgment(owner="architect"))
    if mode == "flow_disabled":
        control.orch.cfg.settings["jev_flow"] = False
    else:
        control.orch.cfg.settings["jev_mode"] = mode
    assert blocker(control) is None
    assert client.evaluate.await_count == (1 if mode == "shadow" else 0)


def test_blocker_redacts_evidence_and_includes_actual_declared_dependency_status(control):
    item = WorkItem("deploy", "implementer", "Deploy after merge", depends_on=["map", "unknown"], status="implement")
    control.orch.work.items.update({"deploy": item, "map": WorkItem("map", "implementer", "Map", status="merging")})
    client = api(control, judgment(owner="dependency"))
    assert blocker(control, role="implementer#deploy", text="Wait for map merge test-private-key " + "x" * 12000,
                   directives=[("REPORT", "blocked Bearer other-secret " + "x" * 2000)]) == "dependency"
    state = client.evaluate.call_args.args[0]
    assert state["declared_dependencies"] == [{"id": "map", "status": "merging"}, {"id": "unknown", "status": "missing"}]
    assert len(state["blocking_report"]) <= 8000
    assert len(state["parsed_directives"][0][1]) <= 500
    assert "test-private-key" not in json.dumps(state) and "other-secret" not in json.dumps(state)


@pytest.mark.parametrize("role", ["architect", "implementer#map"])
@pytest.mark.parametrize("when", ["before", "during"])
def test_internal_report_and_unrelated_work_completion_do_not_discard_route(control, monkeypatch, role, when):
    orch = control.orch
    orch.work.items.update({"map": WorkItem("map", "implementer", "Map", status="implement"),
                            "other": WorkItem("other", "implementer", "Other", status="implement")})
    monkeypatch.setattr(orch, "has_pending_user_input", lambda: any(text != REPORT_TRIGGER for _, text in orch.inbox._queue), raising=False)

    def notification():
        orch.work.items["other"].status = "merged"
        orch.work.items["other"].updated += 1
        orch.inbox.put_nowait((None, REPORT_TRIGGER))

    async def evaluate(state, questions):
        if when == "during":
            notification()
        return judgment(owner="architect")

    if when == "before":
        notification()
    api(control, side_effect=evaluate)
    assert blocker(control, role=role) == "architect"
    assert list(orch.inbox._queue) == [(None, REPORT_TRIGGER)]  # Notification is preserved for its consumer.


@pytest.mark.parametrize("change", ["cancel", "phase", "instruction", "remove", "inbox"])
def test_same_work_changes_still_discard_blocker_route(control, change):
    orch = control.orch
    item = WorkItem("map", "implementer", "Map", status="implement")
    orch.work.items["map"] = item

    async def evaluate(state, questions):
        if change == "cancel":
            item.status = "cancelled"
        elif change == "phase":
            item.status = "verify"
        elif change == "instruction":
            item.task = "Only inspect map, do not deploy"
        elif change == "remove":
            del orch.work.items["map"]
        else:
            item.inbox.append("Stop and inspect the map.")
        return judgment(owner="architect")

    api(control, side_effect=evaluate)
    assert blocker(control, role="implementer#map") is None


def test_parallel_route_ignores_unrelated_main_turn_but_approval_revision_stays_global(control):
    orch = control.orch
    item = WorkItem("map", "implementer", "Map", status="implement")
    orch.work.items["map"] = item

    async def evaluate(state, questions):
        orch.cfg.state.last_n += 1
        orch.cfg.state.task["phase"] = "verify"
        return judgment(owner="architect")

    api(control, side_effect=evaluate)
    assert blocker(control, role="implementer#map") == "architect"
    before = control._revision()
    item.status = "cancelled"
    assert control._revision() != before


def test_known_prerequisite_completion_discards_old_dependency_wait(control):
    orch = control.orch
    item = WorkItem("deploy", "implementer", "Deploy after map merge", depends_on=["map"], status="implement")
    prerequisite = WorkItem("map", "implementer", "Map", status="merging")
    orch.work.items.update({"deploy": item, "map": prerequisite})

    async def evaluate(state, questions):
        prerequisite.status = "merged"
        return judgment(owner="dependency")

    api(control, side_effect=evaluate)
    assert blocker(control, role="architect#deploy (심사)") is None


def test_new_user_message_still_invalidates_blocker_route(control):
    async def evaluate(state, questions):
        control.orch.inbox.put_nowait((None, "Stop deployment and inspect the permission settings."))
        return judgment(owner="architect")

    api(control, side_effect=evaluate)
    assert blocker(control) is None
