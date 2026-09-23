"""T11: 실제 어댑터와 정책을 사용하고 외부 SDK/RPC 수송만 대체한다."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from duet.adapters.claude import ClaudeAdapter
from duet.adapters.codex import CodexAdapter, RpcError
from duet.core.agreement import new_task
from duet.core.config import DEFAULT_POLICY, Role
from duet.core.events import EventBus
from duet.core.policy import AUTO, Decision, Policy


def test_T11_claude_pretool_blocks_bash_write_and_unknown_before_allow_settings(tmp_path, monkeypatch):
    role = Role("implementer", "claude")
    policy = Policy(tmp_path, DEFAULT_POLICY, "architect")
    policy.task = new_task(role.name, "plan", None, {})
    requests = []

    async def approve(req):
        requests.append(req)
        tier, reason = policy.classify(req, role)
        return Decision(tier == AUTO, reason)

    ad = ClaudeAdapter(role, tmp_path, EventBus(), approve, None, "")
    monkeypatch.setattr("duet.adapters.claude.which", lambda _: None)
    options = ad._options(None)
    hook = options.hooks["PreToolUse"][0].hooks[0]

    async def scenario():
        ad.plan_read_only = True
        for name in ("Bash", "Write", "Edit", "MultiEdit", "NotebookEdit", "Agent", "Skill", "mcp__write"):
            result = await hook({"tool_name": name, "tool_input": {"command": "touch x"}}, "tool", None)
            assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
            assert "Read/Grep/Glob" in result["hookSpecificOutput"]["permissionDecisionReason"]
        assert not requests  # 쓰기는 심사자 판단이나 can_use_tool로 넘기지 않는다.
        for name in ("Read", "Grep", "Glob"):
            assert await hook({"tool_name": name, "tool_input": {}}, "tool", None) == {}
        network = await hook({"tool_name": "WebSearch", "tool_input": {"query": "example"}}, "tool", None)
        assert requests[-1].tool == "WebSearch"
        assert network["hookSpecificOutput"]["permissionDecision"] == "deny"  # 기존 네트워크 심사 결과
        ad.plan_read_only = False
        assert await hook({"tool_name": "Bash", "tool_input": {"command": "touch x"}}, "tool", None) == {}

    asyncio.run(scenario())


def test_T11_claude_verify_hook_exact_command_and_full_response(tmp_path, monkeypatch):
    role = Role("architect", "claude", permissions="read_only")
    policy = Policy(tmp_path, DEFAULT_POLICY, "architect")
    policy.task = new_task("implementer", "task", None, {})
    policy.task.update(phase="verify", test_command="python -m pytest -q")

    async def approve(req):
        tier, reason = policy.classify(req, role)
        return Decision(tier == AUTO, reason)

    class Client:
        def __init__(self, options):
            self.options = options

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def query(self, prompt):
            pass

        async def receive_response(self):
            yield AssistantMessage(content=[TextBlock("계획 앞부분"), TextBlock("계획 뒷부분")], model="fake")
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                num_turns=1, session_id="new-id")

    monkeypatch.setattr("duet.adapters.claude.ClaudeSDKClient", Client)
    monkeypatch.setattr("duet.adapters.claude.which", lambda _: None)

    async def scenario():
        ad = ClaudeAdapter(role, tmp_path, EventBus(), approve, None, "")
        await ad.start()
        ad.verification_command = policy.task["test_command"]
        for cmd, verdict in (("python -m pytest -q", "allow"), ("python -m pytest -x", "deny")):
            result = await ad._pre_tool_use({"tool_name": "Bash", "tool_input": {"command": cmd}}, None, None)
            assert result["hookSpecificOutput"]["permissionDecision"] == verdict
        tr = await ad.run_turn("plan")
        assert tr.text == "계획 뒷부분"
        assert tr.full_text == "계획 앞부분\n\n계획 뒷부분"
        await ad.close()

    asyncio.run(scenario())


def test_T11_codex_turn_sandbox_switch_and_unsupported_stops(tmp_path, monkeypatch):
    monkeypatch.setattr("duet.adapters.codex.which", lambda _: "fake-codex")
    monkeypatch.setattr("duet.adapters.codex.asyncio.create_subprocess_exec",
                        AsyncMock(return_value=SimpleNamespace(returncode=0)))

    class Codex(CodexAdapter):
        async def _read_loop(self):
            pass

        async def _read_stderr(self):
            pass

        async def notify(self, *args):
            pass

        async def request(self, method, params, timeout=120):
            if method == "thread/start":
                return {"thread": {"id": "fake-thread"}}
            if method == "turn/start":
                self.calls.append(params)
                if self.fail:
                    raise RpcError({"code": -32602, "message": "unsupported sandboxPolicy"})
                self._texts.extend(["앞부분", "뒷부분"])
                self._turn_fut.set_result({"id": "turn", "status": "completed"})
                return {"turn": {"id": "turn"}}
            return {}

    async def scenario():
        ad = Codex(Role("implementer", "codex"), tmp_path, EventBus(), None, None, "")
        ad.calls, ad.fail = [], False
        await ad.start()
        for phase in ("plan", "implement", "plan", None):
            ad.agreement_phase = phase
            ad.plan_read_only = phase == "plan"
            tr = await ad.run_turn("test")
            assert tr.ok and tr.full_text == "앞부분\n\n뒷부분"
        assert [p["sandboxPolicy"]["type"] for p in ad.calls] == ["readOnly", "workspaceWrite", "readOnly", "workspaceWrite"]
        assert [p["approvalPolicy"] for p in ad.calls] == ["never", "untrusted", "never", "untrusted"]
        ad.plan_read_only, ad.fail = True, True
        count = len(ad.calls)
        tr = await ad.run_turn("test")
        assert not tr.ok and "REPORT deviation" in tr.error
        assert len(ad.calls) == count + 1  # 권한을 넓혀 재시도하는 fallback 없음
        await ad.close()

    asyncio.run(scenario())
