"""현재 턴 메시지: 실제 어댑터·훅·대화 기록을 쓰고 외부 수송만 대체한다."""
import asyncio
import json
import re
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from duet.adapters.agy import AgyAdapter, install_hook
from duet.adapters.base import TurnResult
from duet.adapters.claude import ClaudeAdapter
from duet.adapters.codex import CodexAdapter, RpcError
from duet.core.config import Role
from duet.core.events import EventBus
from duet.core.policy import Decision
from duet.core.work import WorkItem
from duet.tests.test_agy import fake_agy
from duet.tests.test_agreement import orch
from duet.tests.test_markdown import STATIC, node
from duet.tests.test_web import make_ui


def adapter(cls, tmp_path, cli="claude", approve=None):
    return cls(Role("architect", cli), tmp_path, EventBus(), approve, "session", "")


def test_codex_steers_exact_active_turn_and_rejects_completed_turn(tmp_path):
    async def run():
        ad = adapter(CodexAdapter, tmp_path, "codex")
        ad.busy = True
        ad._turn_id = "active"
        ad._turn_fut = asyncio.get_running_loop().create_future()
        ad.request = AsyncMock(return_value={"turnId": "active"})
        assert await ad.steer("범위를 줄여줘")
        args = ad.request.call_args
        assert args.args == ("turn/steer", {"threadId": "session", "expectedTurnId": "active",
                           "input": [{"type": "text", "text": "범위를 줄여줘", "text_elements": []}]})
        assert args.kwargs["timeout"] == 15
        ad._turn_fut.set_result({"status": "completed"})
        assert not await ad.steer("너무 늦은 메시지")
        assert ad.request.await_count == 1
    asyncio.run(run())


@pytest.mark.parametrize("event", ["PreToolUse", "PostToolUse", "PostToolUseFailure", "Stop"])
def test_claude_injects_without_querying_or_interrupting(tmp_path, event):
    async def run():
        approve = AsyncMock(return_value=Decision(False, "기존 권한 유지"))
        ad = adapter(ClaudeAdapter, tmp_path, approve=approve)
        ad.client = SimpleNamespace(query=AsyncMock(), interrupt=AsyncMock())
        ad.busy = True
        ad.verification_command = "pytest"
        pending = asyncio.create_task(ad.steer("변경 범위는 한 파일만"))
        await asyncio.sleep(0)
        assert not pending.done()
        data = {"hook_event_name": event, "tool_name": "Bash", "tool_input": {"command": "touch x"}}
        hook = ad._pre_tool_use if event == "PreToolUse" else ad._steering_stop if event == "Stop" else ad._steering_context
        result = await hook(data, "tool", None)
        assert await pending
        if event == "Stop":
            assert result["decision"] == "block" and "한 파일만" in result["reason"]
        else:
            assert result["hookSpecificOutput"]["hookEventName"] == event
            assert "한 파일만" in result["hookSpecificOutput"]["additionalContext"]
            if event == "PreToolUse":
                assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
                assert result["hookSpecificOutput"]["permissionDecisionReason"] == "기존 권한 유지"
        ad.client.query.assert_not_awaited()
        ad.client.interrupt.assert_not_awaited()
        assert await ad._steering_stop({}, None, None) == {}
    asyncio.run(run())


@pytest.mark.parametrize("cls,cli", [(ClaudeAdapter, "claude"), (AgyAdapter, "agy")])
@pytest.mark.parametrize("action", ["finish", "interrupt"])
def test_turn_end_and_interrupt_release_undelivered_message(tmp_path, cls, cli, action):
    async def run():
        ad = adapter(cls, tmp_path, cli)
        ad.client = SimpleNamespace(interrupt=AsyncMock())
        ad.busy = True
        # agy 실행 중 가드까지 통과시키되 실제 프로세스에 신호를 보내지 않는다.
        ad.proc = SimpleNamespace(returncode=None)
        pending = asyncio.create_task(ad._queue_steering("보관할 메시지"))
        await asyncio.sleep(0)
        if action == "interrupt":
            ad.proc = None
            await ad.interrupt()
        else:
            ad._finish_steering()
        assert not await pending
        assert not await ad._queue_steering("종료 후 새 메시지")
        assert ad._take_steering() == []
    asyncio.run(run())


@pytest.mark.parametrize("event", ["PreInvocation", "PostInvocation"])
def test_agy_authenticated_hook_injects_current_turn(tmp_path, event):
    async def run():
        ad = adapter(AgyAdapter, tmp_path, "agy")
        ad.busy = True
        ad.proc = SimpleNamespace(returncode=None)
        ad._token = "tok"
        ad._hook_calls = 0
        ad._decide = AsyncMock()

        class Writer:
            def write(self, data): self.data = json.loads(data)
            async def drain(self): pass
            def close(self): pass

        async def hook(token):
            reader = asyncio.StreamReader()
            reader.feed_data(json.dumps({"token": token, "event": event, "payload": {}}).encode() + b"\n")
            writer = Writer()
            await ad._on_hook(reader, writer)
            return writer.data

        pending = asyncio.create_task(ad.steer("새 요구를 반영해줘"))
        await asyncio.sleep(0)
        assert (await hook("wrong"))["decision"] == "deny"
        assert not pending.done()
        result = await hook("tok")
        assert result["injectSteps"] == [{"userMessage": "새 요구를 반영해줘"}]
        assert (result.get("terminationBehavior") == "force_continue") == (event == "PostInvocation")
        assert await pending
        assert await hook("tok") == {}
        assert ad._hook_calls == 0  # 모델 훅 호출을 권한 훅 정상 동작으로 오인하지 않는다.
        ad._decide.assert_not_awaited()
    asyncio.run(run())


def test_agy_installs_invocation_hooks_without_losing_policy(tmp_path):
    install_hook(tmp_path)
    hooks = json.loads((tmp_path / ".agents/hooks.json").read_text())["duet-policy"]
    assert hooks["PreToolUse"][0]["matcher"] == "*"
    for event in ("PreInvocation", "PostInvocation"):
        assert hooks[event][0]["command"].endswith(" " + event)


def test_agy_invocation_script_reaches_real_bridge(tmp_path, fake_agy):
    from duet.adapters.agy import HOOK_SCRIPT

    async def run():
        ad = adapter(AgyAdapter, tmp_path, "agy")
        await ad.start()
        try:
            ad.busy = True
            ad.proc = SimpleNamespace(returncode=None)
            pending = asyncio.create_task(ad.steer("실제 훅 수송 확인"))
            await asyncio.sleep(0)
            env = {**os.environ, "DUET_AGY_BRIDGE": ad._bridge, "DUET_AGY_TOKEN": ad._token}
            proc = await asyncio.create_subprocess_exec(sys.executable, str(HOOK_SCRIPT), "PostInvocation",
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, env=env)
            out, _ = await proc.communicate(b'{"invocationNum":1}')
            assert proc.returncode == 0
            assert json.loads(out) == {"injectSteps": [{"userMessage": "실제 훅 수송 확인"}],
                                       "terminationBehavior": "force_continue"}
            assert await pending
        finally:
            ad.proc = None
            await ad.close()
    asyncio.run(run())


@pytest.mark.parametrize("suffix", ["", "@arch"])
def test_work_steering_routes_to_active_session_and_records_once(orch, suffix):
    async def run():
        item = WorkItem("api", "implementer", "API 작업", status="implement")
        orch.work.items[item.id] = item
        ad = adapter(ClaudeAdapter, orch.project)
        ad.client = SimpleNamespace()
        ad.busy = True
        orch.work.adapters[item.id + suffix] = ad
        pending = asyncio.create_task(orch.work.steer(item.id, "작업 중 추가 요청"))
        await asyncio.sleep(0)
        await ad._steering_stop({}, None, None)
        assert "현재 턴에 전달" in await pending
        assert not item.inbox
        body = (orch.project / item.thread).read_text()
        assert body.count("작업 중 추가 요청") == 1
        ad.busy = False
        ad._finish_steering()
        assert "다음 턴" in await orch.work.steer(item.id, "끝난 뒤 추가 요청")
        assert item.inbox == ["끝난 뒤 추가 요청"]
    asyncio.run(run())


def test_steering_records_unique_human_number_during_active_turn(orch):
    async def run():
        ad = adapter(ClaudeAdapter, orch.project)
        ad.client = SimpleNamespace()
        entered, finish = asyncio.Event(), asyncio.Event()

        async def turn(prompt):
            ad.busy = True
            entered.set()
            await finish.wait()
            ad.busy = False
            ad._finish_steering()
            return TurnResult("추가 메시지 반영 완료")

        ad.run_turn = turn
        orch.adapters["architect"] = ad
        running = asyncio.create_task(orch._turn("architect", "system", "첫 요청"))
        await entered.wait()
        n = orch._active_turn_n
        steer = asyncio.create_task(orch.steer("현재 턴의 추가 요구"))
        await asyncio.sleep(0)
        await ad._steering_stop({}, None, None)
        assert "현재 턴에 전달" in await steer
        assert orch.inbox.empty()
        assert orch.dialogue.find("human", n + 1)
        finish.set()
        _, turn = await running
        assert turn.n == n
        assert orch.dialogue.find("architect", n)
        assert orch._next_number() == n + 2
        assert orch._active_turn_n is None
        assert orch.cfg.state.seen["architect"] == n + 1
    asyncio.run(run())


def test_failed_steering_queues_once_and_respects_target(orch):
    async def run():
        ad = adapter(CodexAdapter, orch.project, "codex")
        ad.busy = True
        ad.steer = AsyncMock(side_effect=RpcError({"message": "turn already finished"}))
        orch.running_role = "implementer"
        orch.adapters["implementer"] = ad
        result = await orch.steer("늦은 추가 요청", "implementer")
        assert "다음 턴" in result
        assert orch.inbox.get_nowait() == ("implementer", "늦은 추가 요청")
        assert orch.inbox.empty()
        assert not orch.dialogue.turns()
        # 메인 선택을 진행 중 작업자에게 임의로 바꾸지 않는다.
        await orch.steer("메인에게만 보낼 내용")
        assert orch.inbox.get_nowait() == (None, "메인에게만 보낼 내용")
        assert ad.steer.await_count == 1
    asyncio.run(run())


def test_web_steering_wait_does_not_block_approval_reply(tmp_path):
    async def run():
        ui = make_ui(tmp_path)
        ready, release = asyncio.Event(), asyncio.Event()

        async def steer(text, to=None):
            ready.set()
            await release.wait()
            return "현재 턴에 전달했습니다."

        ui.orch.steer = steer
        response = await asyncio.wait_for(ui.handle({"type": "input", "text": "추가", "immediate": True}), 1)
        assert "전달하는 중" in response["text"]
        await ready.wait()
        approval = asyncio.get_running_loop().create_future()
        ui.pending["approval"] = {"future": approval}
        await ui.handle({"type": "answer", "id": "approval", "answer": "y"})
        assert (await approval)["answer"] == "y"
        tasks = list(ui._steer_tasks)
        release.set()
        await asyncio.gather(*tasks)
        assert any("현재 턴에 전달" in m.get("data", {}).get("text", "") for m in ui.history)
        await ui.close()
    asyncio.run(run())


def test_keyboard_dispatch_preserves_enter_shift_ime_and_commands():
    html = (STATIC / "index.html").read_text()
    script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
    start = script.index("function sendInput(")
    end = script.index('$("#askSend").onclick', start)
    node(r'''
const assert=require('assert'),vm=require('vm');
const listeners={},sent=[],els={'#input':{value:''},'#target':{value:'implementer'},'#send':{}};
els['#input'].addEventListener=(type,fn)=>listeners[type]=fn;
vm.runInNewContext(process.argv[1],{$:s=>els[s],send:m=>sent.push(m)});
function key(extra={}){els['#input'].value='메시지';let prevented=false;
listeners.keydown({key:'Enter',preventDefault(){prevented=true},...extra});return prevented;}
assert(key());assert.deepEqual(sent.pop(),{type:'input',text:'메시지',to:'implementer'});
assert(key({ctrlKey:true}));assert.equal(sent.pop().immediate,true);
assert(key({metaKey:true}));assert.equal(sent.pop().immediate,true);
assert(!key({shiftKey:true}));assert(!key({isComposing:true}));assert(!key({keyCode:229}));
assert.equal(sent.length,0);assert.equal(els['#input'].value,'메시지');
els['#input'].value='/stop';listeners.keydown({key:'Enter',ctrlKey:true,preventDefault(){}});
assert.deepEqual(sent.pop(),{type:'input',text:'/stop'});
els['#input'].value='클릭 전송';els['#send'].onclick({ctrlKey:true});
assert.equal(sent.pop().immediate,undefined);
''', script[start:end])
