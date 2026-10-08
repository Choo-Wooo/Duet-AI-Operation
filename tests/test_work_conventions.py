"""Parallel work keeps one convention revision and cannot accept an unchecked report."""
import asyncio
import hashlib
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from duet.adapters.base import TurnResult
from duet.core.config import Config, Role
from duet.core.conventions import conventions_guide
from duet.core.events import EventBus
from duet.core.work import WorkBoard, WorkItem, _Wait


def snapshot(content="# Convention\nUse snake_case for functions.\n", *, enabled=True, error=None):
    return {"enabled": enabled, "path": "docs/CODING_CONVENTIONS.md", "content": content,
            "sha256": hashlib.sha256(content.encode()).hexdigest() if enabled and not error else "",
            "error": error}


def report(snap, *, status="pass", evidence="src/api.py:10: read_item follows snake_case", sha=None):
    body = {"sha256": snap["sha256"] if sha is None else sha, "status": status,
            "checks": [{"rule": "Use snake_case for functions", "status": status, "evidence": evidence}]}
    return "```duet-conventions\n" + json.dumps(body) + "\n```\n<!-- duet: ACCEPT -->"


@pytest.fixture
def board(tmp_path):
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles = {"architect": Role("architect", "claude", permissions="read_only"),
                 "implementer": Role("implementer", "codex")}
    ready = asyncio.Event()
    ready.set()
    orch = SimpleNamespace(cfg=cfg, project=tmp_path,
        git=SimpleNamespace(enabled=False, merge_lock=threading.Lock(), snapshot=Mock()),
        bus=EventBus(), inbox=asyncio.Queue(), emit_status=Mock(), notice=Mock(),
        not_paused=ready, work_sessions={}, activity={})
    result = WorkBoard(orch)
    result.schedule = Mock()
    result.base = "main"
    result.wt = SimpleNamespace(check_ready=lambda: None, current_branch=lambda: "main",
        install_pre_push=lambda: None, create=Mock(return_value=tmp_path),
        changed_files=lambda *_: ["src/api.py"], diff_stat=lambda *_: "src/api.py | 1 +")
    return result


def add_item(board, snap=None):
    it = WorkItem("api", "implementer", "Implement API", conventions=snap,
                  agreed_files=["src/api.py"], test_command="pytest tests/test_api.py")
    board.items[it.id] = it
    return it


@pytest.mark.parametrize("enabled", [True, False])
def test_submission_pins_and_persists_enabled_or_disabled_conventions(board, monkeypatch, enabled):
    current = snapshot(enabled=enabled)
    loader = Mock(return_value=current)
    monkeypatch.setattr("duet.core.work.load_conventions", loader)
    assert board.submit([{"id": "api", "role": "implementer", "task": "Implement API"}], 3) == []
    it = board.items["api"]
    assert it.conventions == current
    loader.return_value = snapshot("Changed later", enabled=not enabled)
    board.cfg.settings["coding_conventions_enabled"] = not enabled
    assert board._conventions(it) == current
    assert loader.call_count == 1
    restored = WorkBoard(board.orch)
    assert restored.items["api"].conventions == current


def test_legacy_item_captures_only_once(board, monkeypatch):
    it = add_item(board)
    initial = snapshot()
    loader = Mock(return_value=initial)
    monkeypatch.setattr("duet.core.work.load_conventions", loader)
    assert board._conventions(it) == initial
    loader.return_value = snapshot("new global version")
    assert board._conventions(it) == initial
    loader.assert_called_once_with(board.cfg)
    assert json.loads(board.path.read_text())["items"][0]["conventions"] == initial


@pytest.mark.parametrize("enabled", [True, False])
def test_turn_passes_pinned_snapshot_to_shared_runner(board, enabled):
    pinned = snapshot(enabled=enabled)
    it = add_item(board, pinned)
    ad = SimpleNamespace(label="implementer#api")
    board.orch.run_agent_turn = AsyncMock(return_value=TurnResult("done"))
    asyncio.run(board._turn(ad, it, "work_implement", "Implement", "implementer"))
    board.orch.run_agent_turn.assert_awaited_once_with(ad, "Implement", conventions=pinned)


def test_turn_fallback_adds_convention_guide(board):
    pinned = snapshot()
    it = add_item(board, pinned)
    ad = SimpleNamespace(label="implementer#api", run_turn=AsyncMock(return_value=TurnResult("done")))
    asyncio.run(board._turn(ad, it, "work_plan", "Plan", "implementer"))
    ad.run_turn.assert_awaited_once_with("Plan\n" + conventions_guide(pinned))


@pytest.mark.parametrize("case", ["missing", "failed", "no_evidence", "wrong_version", "baseline"])
def test_accept_requires_independent_complete_convention_report(board, case):
    pinned = snapshot()
    it = add_item(board, pinned)
    full = report(pinned)
    if case == "missing":
        full = "Looks fine.\n<!-- duet: ACCEPT -->"
    elif case == "failed":
        full = report(pinned, status="fail")
    elif case == "no_evidence":
        full = report(pinned, evidence="")
    elif case == "wrong_version":
        full = report(pinned, sha="0" * 64)
    else:
        full = "<!-- duet: ACCEPT baseline -->"
        it.baseline_code = 1
    board._run_test = AsyncMock(return_value=(1 if case == "baseline" else 0, "test output"))
    board._architect = AsyncMock()
    board._turn = AsyncMock(return_value=(None, full, [("ACCEPT", "baseline" if case == "baseline" else "")]))
    reason = asyncio.run(board._verify(it, board.project, "Worker says conventions pass"))
    assert reason.startswith("코딩 컨벤션 검증 보완 필요:")
    assert not it.baseline_waived
    assert "코딩 컨벤션" in (board.project / it.thread).read_text()


def test_valid_reviewer_convention_report_allows_existing_verification(board):
    pinned = snapshot()
    it = add_item(board, pinned)
    full = report(pinned)
    board._run_test = AsyncMock(return_value=(0, "passed"))
    board._architect = AsyncMock()
    board._turn = AsyncMock(return_value=(None, full, [("ACCEPT", "")]))
    assert asyncio.run(board._verify(it, board.project, "Implementation done")) is None


def test_disabled_item_retains_existing_acceptance_after_setting_enabled(board):
    it = add_item(board, snapshot(enabled=False))
    board.cfg.settings["coding_conventions_enabled"] = True
    board._run_test = AsyncMock(return_value=(0, "passed"))
    board._architect = AsyncMock()
    board._turn = AsyncMock(return_value=(None, "ACCEPT", [("ACCEPT", "")]))
    assert asyncio.run(board._verify(it, board.project, "Implementation done")) is None


def test_convention_pass_does_not_bypass_tests_or_rework(board):
    pinned = snapshot()
    it = add_item(board, pinned)
    full = report(pinned)
    board._run_test = AsyncMock(return_value=(1, "new failure"))
    board._architect = AsyncMock()
    board._turn = AsyncMock(return_value=(None, full, [("ACCEPT", "")]))
    assert "합의 테스트가 실패" in asyncio.run(board._verify(it, board.project, "done"))
    board._run_test.return_value = (0, "passed")
    board._turn.return_value = (None, full, [("ACCEPT", ""), ("REWORK", "Fix requirement")])
    assert asyncio.run(board._verify(it, board.project, "done")) == "Fix requirement"


def test_missing_conventions_pause_before_worktree_or_model_and_reload_on_resume(board, monkeypatch):
    it = add_item(board, snapshot(error="Convention file missing"))
    board._plan = AsyncMock()
    board._implement_loop = AsyncMock()
    asyncio.run(board._run(it))
    assert it.status == "waiting:plan"
    assert "Convention file missing" in it.wait_reason
    board.wt.create.assert_not_called()
    board._implement_loop.assert_not_awaited()
    repaired = snapshot("# Repaired\nUse snake_case.")
    loader = Mock(return_value=repaired)
    monkeypatch.setattr("duet.core.work.load_conventions", loader)
    assert board.resume(it.id) == "api 재개"
    assert it.conventions == repaired
    loader.assert_called_once_with(board.cfg)


def test_valid_snapshot_survives_pause_and_resume(board, monkeypatch):
    initial = snapshot()
    it = add_item(board, initial)
    it.status = "waiting:verify"
    loader = Mock(return_value=snapshot("replacement"))
    monkeypatch.setattr("duet.core.work.load_conventions", loader)
    board.resume(it.id)
    assert it.conventions == initial
    assert it.resume_at == "verify"
    loader.assert_not_called()


def test_read_only_peer_consult_uses_peers_convention_snapshot(board, monkeypatch):
    requester = add_item(board, snapshot("Requester conventions"))
    peer_rules = snapshot("Peer conventions")
    peer = WorkItem("db", "implementer", "Database", conventions=peer_rules)
    board.items[peer.id] = peer
    board.wt.path_for = lambda _wid: board.project
    board.orch.fake = True
    board._start_activity = AsyncMock()
    ad = SimpleNamespace(label="implementer#db(답변:api)", close=AsyncMock(),
                         run_turn=AsyncMock(return_value=TurnResult("Use db API")))
    monkeypatch.setattr("duet.adapters.make_adapter", Mock(return_value=ad))
    result = asyncio.run(board._answer(requester, "db", "Which API?", board.project))
    assert "Use db API" in result
    sent = ad.run_turn.call_args.args[0]
    assert conventions_guide(peer_rules) in sent
    assert "Requester conventions" not in sent
    ad.close.assert_awaited_once()
