"""질문 콘솔: 본 세션을 복제한 읽기 전용 분신, 기록·pid 정리. 모든 쓰기는 tmp_path 안."""
import asyncio

import pytest

from duet import ask
from duet.adapters.base import AgentAdapter, TurnResult
from duet.core.config import Config, Role
from duet.core.policy import ApprovalRequest


class ForkAdapter(AgentAdapter):
    made = []

    async def start(self):
        self.started_from = self.session_id
        self.forked = self.fork_session
        self.session_id = "fork-1"
        self.prompts = []
        ForkAdapter.made.append(self)

    async def run_turn(self, prompt):
        self.prompts.append(prompt)
        return TurnResult("답: docs/design.md 참고")

    async def interrupt(self):
        pass

    async def close(self):
        self.closed = True


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = Config(tmp_path)
    c.dir.mkdir()
    c.roles = {"architect": Role("architect", "claude", permissions="read_only"),
               "implementer": Role("implementer", "codex")}
    c.state.sessions = {"architect": "arch-main"}
    c.save_roles()
    c.save_state()
    ForkAdapter.made = []

    def factory(role, project, bus, approver, session_id, system_append,
                reviewer=False, fake=False, fork_session=False):
        a = ForkAdapter(role, project, bus, approver, session_id, system_append, reviewer, fork_session)
        a.system_seen = system_append
        return a

    monkeypatch.setattr("duet.adapters.make_adapter", factory)
    return c


def test_ask_forks_main_session_and_logs(cfg, monkeypatch):
    answers = iter(["설계 요약해줘", "/quit"])

    def fake_input(loop, prompt):
        fut = loop.create_future()
        fut.set_result(next(answers))
        return fut
    monkeypatch.setattr(ask, "_ainput", fake_input)
    rc = asyncio.run(ask.run_ask(cfg))
    assert rc == 0
    a = ForkAdapter.made[0]
    assert a.started_from == "arch-main" and a.forked and a.plan_read_only
    assert a.role.permissions == "read_only" and "질문 콘솔" in a.system_seen
    assert a.prompts == ["설계 요약해줘"] and a.closed
    assert cfg.state.sessions["architect"] == "arch-main"  # 본 세션 id 는 그대로
    logs = list((cfg.project / ".duet" / "asks").glob("*.md"))
    assert logs and "설계 요약해줘" in logs[0].read_text()
    assert not list((cfg.project / ".duet" / "asks").glob("*.pid"))


def test_ask_is_read_only():
    d = asyncio.run(ask._read_only_approver(ApprovalRequest("architect", "file", "Write x", paths=["x"])))
    assert not d.allow


def test_launch_window_builds_terminal_command(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ask.sys, "platform", "darwin")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setattr(ask.subprocess, "run", lambda args, **kw: calls.append(args))
    msg = ask.launch_window(tmp_path / "duet", tmp_path, "architect")
    assert "질문 콘솔" in msg
    script = calls[0][2]
    assert calls[0][0] == "osascript" and 'tell application "Terminal"' in script
    assert "--ask architect" in script and "DUET_ASK_WINDOW=1" in script


def test_stop_all_signals_and_cleans(tmp_path, monkeypatch):
    d = tmp_path / ".duet" / "asks"
    d.mkdir(parents=True)
    (d / "4242.pid").write_text("4242")
    killed = []
    monkeypatch.setattr(ask.os, "kill", lambda pid, sig: killed.append(pid))
    assert ask.stop_all(tmp_path) == 1 and killed == [4242] and not list(d.glob("*.pid"))
