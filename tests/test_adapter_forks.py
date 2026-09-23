"""실제 CLI 프로세스/API 대신 SDK client와 JSON-RPC 응답을 fake로 대체한다."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from duet.adapters.claude import ClaudeAdapter
from duet.adapters.codex import CodexAdapter, RpcError
from duet.core.config import Role
from duet.core.events import EventBus


def test_claude_options_fork_and_recovery_prompt_once(tmp_path, monkeypatch):
    clients = []

    class Client:
        def __init__(self, options):
            self.options = options
            self.prompts = []
            self.disconnected = False
            clients.append(self)

        async def connect(self):
            if self.options.resume:
                raise RuntimeError("missing session")

        async def disconnect(self):
            self.disconnected = True

        async def query(self, prompt):
            self.prompts.append(prompt)

        async def receive_response(self):
            for msg in []:
                yield msg

    monkeypatch.setattr("duet.adapters.claude.ClaudeSDKClient", Client)
    monkeypatch.setattr("duet.adapters.claude.which", lambda _: None)

    async def scenario():
        ad = ClaudeAdapter(Role("architect", "claude"), tmp_path, EventBus(), None,
                           "original", "system", fork_session=True)
        await ad.start()
        assert clients[0].options.resume == "original"
        assert clients[0].options.fork_session is True
        assert clients[0].disconnected
        assert clients[1].options.resume is None and not clients[1].options.fork_session
        assert ad.session_id is None and ad.restore_failed
        await ad.run_turn("첫 턴")
        await ad.run_turn("다음 턴")
        assert clients[1].prompts == ["세션 복원 실패: DIALOGUE.md 전체를 읽고 맥락을 복구하라.\n\n첫 턴", "다음 턴"]
        await ad.close()

    asyncio.run(scenario())


def test_claude_fork_session_id_returned_after_first_response(tmp_path, monkeypatch):
    from claude_agent_sdk import ResultMessage

    class Client:
        def __init__(self, options):
            assert options.fork_session and options.resume == "original"

        async def connect(self):
            pass

        async def query(self, prompt):
            pass

        async def receive_response(self):
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="forked", result="완료")

        async def disconnect(self):
            pass

    monkeypatch.setattr("duet.adapters.claude.ClaudeSDKClient", Client)
    monkeypatch.setattr("duet.adapters.claude.which", lambda _: None)

    async def scenario():
        ad = ClaudeAdapter(Role("architect", "claude"), tmp_path, EventBus(), None,
                           "original", "system", fork_session=True)
        await ad.start()
        assert ad.fork_session  # id가 나오기 전에는 분기 예약을 유지
        tr = await ad.run_turn("계속")
        assert tr.ok and ad.session_id == "forked" and not ad.fork_session
        await ad.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure, expected_method, expected_id", [
    (None, "thread/fork", "forked"),
    (-32601, "thread/resume", "original"),
    (-32000, "thread/start", "fresh"),
])
def test_codex_fork_rpc_and_fallbacks(tmp_path, monkeypatch, failure, expected_method, expected_id):
    proc = SimpleNamespace(returncode=0)
    monkeypatch.setattr("duet.adapters.codex.which", lambda _: "fake-codex")
    launch = AsyncMock(return_value=proc)
    monkeypatch.setattr("duet.adapters.codex.asyncio.create_subprocess_exec", launch)

    class Codex(CodexAdapter):
        async def _read_loop(self):
            pass

        async def _read_stderr(self):
            pass

        async def notify(self, *args):
            pass

        async def request(self, method, params, timeout=120):
            self.calls.append((method, params))
            if method == "thread/fork":
                if failure:
                    raise RpcError({"code": failure, "message": "test error"})
                return {"thread": {"id": "forked"}}
            if method == "thread/resume":
                return {"thread": {"id": "original"}}
            if method == "thread/start":
                return {"thread": {"id": "fresh"}}
            if method == "turn/start":
                self._turn_fut.set_result({"id": "turn", "status": "completed"})
                return {"turn": {"id": "turn"}}
            return {}

    async def scenario():
        bus, events = EventBus(), []
        bus.subscribe(events.append)
        ad = Codex(Role("implementer", "codex"), tmp_path, bus, None, "original", "system", fork_session=True)
        ad.calls = []
        await ad.start()
        assert ad.session_id == expected_id and not ad.fork_session
        assert ad.calls[-1][0] == expected_method
        params = next(params for method, params in ad.calls if method == "thread/fork")
        assert params["threadId"] == "original"
        assert params["cwd"] == str(tmp_path) and params["sandbox"] == "workspace-write"
        assert params["developerInstructions"] == "system"
        if failure == -32601:
            assert any("지원하지 않아" in e.data.get("text", "") for e in events)
        await ad.run_turn("first")
        await ad.run_turn("second")
        prompts = [p["input"][0]["text"] for m, p in ad.calls if m == "turn/start"]
        assert ("세션 복원 실패" in prompts[0]) == (failure == -32000)
        assert prompts[1] == "second"
        assert sum(m == "thread/fork" for m, _ in ad.calls) == 1
        await ad.close()

    asyncio.run(scenario())
