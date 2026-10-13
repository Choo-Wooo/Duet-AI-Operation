"""Research selection, persistence and execution stay separate from development."""
import asyncio
import json

import pytest

from duet import commands
from duet.app import parse_args
from duet.core.config import Config, ensure_research_roles
from duet.core.policy import ApprovalRequest
from duet.core.work import WorkItem
from duet.tests.test_agreement import orch
from duet.tests.test_web import make_ui


def test_old_project_gets_independent_peer_settings_without_changing_development(orch):
    cfg = orch.cfg
    assert cfg.state.workflow == "development"
    assert cfg.roles["explorer"].cli == cfg.roles[cfg.main].cli
    assert cfg.roles["critic"].cli == cfg.roles["implementer"].cli
    cfg.roles["critic"].model = "custom-model"
    cfg.roles["explorer"].brief = "Custom exploration"
    assert ensure_research_roles(cfg) == []
    assert cfg.roles["critic"].model == "custom-model"
    assert cfg.roles["implementer"].model is None
    assert cfg.roles["explorer"].brief == "Custom exploration"
    assert cfg.roles["critic"].permissions == "read_only"


def test_workflow_selection_persists_without_dispatching_old_dialogue(orch):
    orch.dialogue.append_turn("architect", 4, "Continue the old implementation")
    orch.set_workflow("research")
    assert orch.inbox.empty() and not orch.running and not orch.adapters
    loaded = Config(orch.cfg.project)
    loaded.load()
    assert loaded.state.workflow == "research"
    assert orch.status()["main"] == "explorer"
    assert orch.cfg.main == "architect"
    assert orch.cfg.settings["full_auto"] is False
    assert orch.cfg.state.mode == "review"


@pytest.mark.parametrize("busy", ["running", "pending_approvals", "loading", "_turn_active"])
def test_workflow_cannot_switch_during_active_execution(orch, busy):
    setattr(orch, busy, True)
    with pytest.raises(ValueError):
        orch.set_workflow("research")
    assert orch.cfg.state.workflow == "development"


def test_queued_input_or_waiting_work_prevents_workflow_switch(orch):
    orch.submit("Implement this")
    with pytest.raises(ValueError):
        orch.set_workflow("research")
    orch.inbox.get_nowait()
    orch.work.items["task"] = WorkItem("task", "implementer", "build", status="waiting:implement")
    with pytest.raises(ValueError):
        orch.set_workflow("research")


def test_web_mode_and_both_role_sections_use_existing_settings_api(tmp_path):
    async def scenario():
        ui = make_ui(tmp_path)
        try:
            state = ui.state()
            peers = {r["name"]: r for r in state["roles"] if r["workflow"] == "research"}
            assert set(peers) == {"explorer", "critic"}
            response = await ui.handle(dict(type="workflow", name="research"))
            assert response["state"]["status"]["workflow"] == "research"
            assert response["state"]["main"] == "explorer"
            assert response["state"]["development_main"] == "architect"
            await ui.handle(dict(type="role_set", name="critic", field="model", value="custom"))
            assert ui.cfg.roles["critic"].model == "custom"
            await ui.handle(dict(type="role_set", name="critic", field="main", value=True))
            assert ui.cfg.main == "architect"
            await ui.handle(dict(type="role_remove", name="critic"))
            assert "critic" in ui.cfg.roles
            ui.orch.running = True
            denied = await ui.handle(dict(type="workflow", name="development"))
            assert denied.get("error") and ui.cfg.state.workflow == "research"
            ui.orch.running = False
        finally:
            await ui.close()
            await ui.orch.close()
    asyncio.run(scenario())


def test_research_state_is_part_of_named_saves_and_restored_without_dispatch(orch):
    async def scenario():
        orch.set_workflow("research")
        orch.cfg.state.research = {"phase": "awaiting_user", "goal": "Find the bound", "questions": [
            {"question": "Which domain?", "why": "Define the range", "owner": "user"}]}
        orch.save("research-session")
        saved = json.loads((orch.cfg.saves_dir / "research-session/state.json").read_text())
        orch.cfg.state.research = {"goal": "different"}
        await orch.load_save("research-session")
        assert orch.cfg.state.research == saved["research"]
        assert orch.research.state == saved["research"]
        assert not orch.running and orch.inbox.empty()
    asyncio.run(scenario())


def test_cli_workflow_does_not_change_autonomy(orch):
    assert parse_args(["--workflow", "research"]).workflow == "research"
    result = asyncio.run(commands.handle(orch, "/workflow research"))
    assert "연구" in result
    assert orch.cfg.state.workflow == "research"
    assert not orch.full_auto


def test_research_steering_waits_for_goal_reconciliation(orch):
    orch.set_workflow("research")
    asyncio.run(orch.steer("The budget is now ten", to="critic"))
    assert orch.inbox.get_nowait() == ("critic", "The budget is now ten")


def test_research_uses_dedicated_verification_and_real_dispatch_receipt(orch):
    from duet.tests.test_research import body
    orch.set_workflow("research")
    n = orch._append_human(None, "Find a counterexample")
    nxt = orch.research.begin(("explorer", "human", str(n)))
    orch.scripts["explorer"].append(body(goal="Find a counterexample", success_criteria=["Reproducible case"]))
    asyncio.run(orch._turn(*nxt))
    prompt = orch.prompts[-1][2]
    assert "[연구 검증]" in prompt
    assert "예외·오류·경계값·부하·장시간 동작은 테스트를 새로 만들거나 실행하지 말고" not in prompt
    outcomes = [e for e in orch.events if e.kind == "control_outcome"]
    assert any(e.data["stage"] == "dispatched" and e.data["applied"] for e in outcomes)


def test_research_clarification_cannot_write_even_with_full_auto(orch):
    orch.set_workflow("research")
    orch.cfg.settings["full_auto"] = True
    orch.cfg.roles["explorer"].permissions = "workspace_write"
    n = orch._append_human(None, "Solve a vague problem")
    orch.research.begin(("explorer", "human", str(n)))
    req = ApprovalRequest("explorer", "file", "Write implementation.py", paths=["implementation.py"])
    decision = asyncio.run(orch._handle_approval(req))
    assert not decision.allow


def test_research_reviewer_sessions_never_overwrite_development_reviewer(orch):
    async def scenario():
        orch.set_workflow("research")
        orch.cfg.state.reviewer_sessions["architect"] = "saved-development-reviewer"
        orch.scripts["explorer"].append('{"decision":"allow","reason":"routine investigation"}')
        decision, _ = await orch._ask_architect(ApprovalRequest("critic", "command", "$ python check.py", command="python check.py"), "investigate")
        assert decision.allow
        assert orch.cfg.state.reviewer_sessions["explorer"] == orch.reviewer.session_id
        assert orch.cfg.state.reviewer_sessions["architect"] == "saved-development-reviewer"
        await orch._fresh_reviewer("test rotation")
        assert "explorer" not in orch.cfg.state.reviewer_sessions
        assert orch.cfg.state.reviewer_sessions["architect"] == "saved-development-reviewer"
    asyncio.run(scenario())


def test_deferred_development_control_cannot_hijack_research_loop(orch):
    from duet.tests.test_research import body
    orch._deferred_controls.append(dict(role="architect", turn=1, kind="system", reason="old development command"))
    orch.set_workflow("research")
    n = orch._append_human(None, "Find a solution")
    orch.scripts["explorer"].extend([
        body(goal="Find a solution", success_criteria=["Works"]),
        body(candidate="A concrete candidate"),
    ])
    orch.scripts["critic"].append(body(questions=[dict(question="Which budget?", why="User-owned constraint", owner="user")]))
    asyncio.run(orch.run(("explorer", "human", str(n))))
    assert orch.research.state["phase"] == "awaiting_user"
    assert all(role in ("explorer", "critic") for role, _, _ in orch.prompts)
    assert orch._deferred_controls
