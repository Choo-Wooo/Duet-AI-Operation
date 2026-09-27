"""B2-11/13: actual lifecycle, mocked SDK/approval transport, no live service."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from collections import deque
import json
import pytest
from duet.adapters.claude import ClaudeAdapter
from duet.core.config import Role
from duet.core.events import EventBus
from duet.core.policy import ApprovalRequest, Decision
from duet.tests.test_agreement import orch


def test_reviewer_interrupt_precedes_busy_clear_and_connection_discard(orch):
    async def run():
        class Reviewer:
            busy = False
            context_tokens = 0
            async def run_turn(self, prompt):
                self.busy = True
                try: await asyncio.Event().wait()
                finally: self.busy = False
            async def interrupt(self):
                assert self.busy, 'interrupt called after busy cleared'
                self.interrupted = True
            async def close(self): self.closed = True
        rev = Reviewer()
        orch.reviewer = rev
        orch.cfg.policy['review_timeout_sec'] = .01
        result = await orch._ask_architect(ApprovalRequest('implementer', 'command', 'x', command='x'), 'test')
        assert getattr(rev, 'interrupted', False)
        assert getattr(rev, 'closed', False)
        assert orch.reviewer is None and result[0] is None
    asyncio.run(run())


def test_claude_hook_budget_and_long_human_approval(tmp_path, monkeypatch):
    async def run():
        async def approve(req):
            await asyncio.sleep(.07)  # scaled 70s, beyond the old SDK 60s
            return Decision(True, 'human')
        ad = ClaudeAdapter(Role('architect', 'claude'), tmp_path, EventBus(), approve, None, '')
        ad.approval_timeout = .2  # scaled human budget; hook budget must cover it
        ad.verification_command = 'pytest'
        monkeypatch.setattr('duet.adapters.claude.which', lambda _: None)
        matcher = ad._options(None).hooks['PreToolUse'][0]
        assert matcher.timeout is not None and matcher.timeout >= ad.approval_timeout
        result = await asyncio.wait_for(ad._pre_tool_use({'tool_name': 'Bash', 'tool_input': {'command': 'x'}}, '1', None), matcher.timeout)
        assert result['hookSpecificOutput']['permissionDecision'] == 'allow'
    asyncio.run(run())


def test_human_timeout_is_explicit_and_late_approval_isolated(tmp_path):
    async def run():
        calls = 0
        async def approve(req):
            nonlocal calls
            calls += 1
            if calls == 1: await asyncio.sleep(.2)
            return Decision(calls == 1, 'second denial')
        bus = EventBus(); events = []; bus.subscribe(events.append)
        ad = ClaudeAdapter(Role('architect', 'claude'), tmp_path, bus, approve, None, '')
        ad.approval_timeout = .01
        ad.verification_command = 'pytest'
        data = {'tool_name': 'Bash', 'tool_input': {'command': 'x'}}
        first = await ad._pre_tool_use(data, '1', None)
        assert first['hookSpecificOutput']['permissionDecision'] == 'deny'
        assert '사람 승인 대기 중 타임아웃' in first['hookSpecificOutput']['permissionDecisionReason']
        assert any(e.kind == 'notice' for e in events)
        second = await ad._pre_tool_use(data, '2', None)
        assert second['hookSpecificOutput']['permissionDecision'] == 'deny'
    asyncio.run(run())


def test_claude_late_result_stays_on_discarded_connection(orch):
    from claude_agent_sdk import AssistantMessage, TextBlock, ResultMessage
    async def run():
        class Client:
            def __init__(self): self.queue = asyncio.Queue(); self.closed = False
            async def query(self, prompt): pass
            async def receive_response(self):
                while True:
                    msg = await self.queue.get()
                    yield msg
                    if isinstance(msg, ResultMessage): return
            async def interrupt(self): assert first.busy; self.interrupted = True
            async def disconnect(self): self.closed = True
        def adapter(client):
            a = ClaudeAdapter(Role('architect', 'claude'), orch.project, orch.bus, None, None, '', reviewer=True)
            a.client = client; a._stderr = deque()
            return a
        old = Client(); first = adapter(old)
        orch.reviewer = first
        with pytest.raises(asyncio.TimeoutError): await orch._review_turn(first, 'first', .01)
        assert old.closed and old.interrupted and orch.reviewer is None
        current = Client(); second = adapter(current); orch.reviewer = second
        pending = asyncio.create_task(orch._review_turn(second, 'second', 1))
        def response(client, text):
            client.queue.put_nowait(AssistantMessage(content=[TextBlock(text)], model='mock'))
            client.queue.put_nowait(ResultMessage(subtype='success', duration_ms=1, duration_api_ms=1,
                                                 is_error=False, num_turns=1, session_id='test'))
        response(old, '{"decision":"allow"}')  # late result from timed-out request
        await asyncio.sleep(0)
        assert not pending.done()
        response(current, '{"decision":"deny"}')
        result = await pending
        assert json.loads(result.text)['decision'] == 'deny'
        assert 'allow' not in result.full_text
    asyncio.run(run())


def test_codex_timeout_interrupt_uses_active_turn_and_old_events_are_isolated(orch):
    from duet.adapters.codex import CodexAdapter
    async def run():
        def adapter():
            ad = CodexAdapter(Role('architect', 'codex'), orch.project, orch.bus, None, 'thread', '', reviewer=True)
            ad._tokens_total = 0; ad._items = {}; ad._stderr = deque()
            ad._pending = {}; ad._turn_fut = None
            return ad
        first = adapter(); calls = []
        async def request(method, params, **kwargs):
            calls.append((method, params, first.busy))
            return {'turn': {'id': 'old'}}
        first.request = request
        orch.reviewer = first
        with pytest.raises(asyncio.TimeoutError): await orch._review_turn(first, 'old', .01)
        assert ('turn/interrupt', {'threadId': 'thread', 'turnId': 'old'}, True) in calls
        assert first._closed and orch.reviewer is None
        second = adapter()
        second.request = AsyncMock(return_value={'turn': {'id': 'new'}})
        pending = asyncio.create_task(second.run_turn('new'))
        await asyncio.sleep(0)
        first._on_notification('turn/completed', {'turn': {'id': 'old', 'status': 'completed'}})
        assert not pending.done()
        second._on_notification('item/completed', {'item': {'id': 'answer', 'type': 'agentMessage', 'text': 'deny'}})
        second._on_notification('turn/completed', {'turn': {'id': 'new', 'status': 'completed'}})
        assert (await pending).text == 'deny'
    asyncio.run(run())


def test_verify_command_auto_never_waits_for_human(orch):
    from duet.core.agreement import new_task
    async def run():
        command = 'cd /Users/mac/Workspace/test && /tmp/duetvenv/bin/python -m pytest duet/tests -q'
        orch.cfg.state.task = new_task('implementer', 'test', None, {})
        orch.cfg.state.task.update(phase='verify', test_command=command)
        orch.ui.ask_approval = AsyncMock(side_effect=AssertionError('AUTO must not prompt'))
        ad = ClaudeAdapter(orch.cfg.roles['architect'], orch.project, orch.bus, orch.handle_approval, None, '')
        ad.verification_command = command
        result = await ad._pre_tool_use({'tool_name': 'Bash', 'tool_input': {'command': command}}, 'auto', None)
        assert result['hookSpecificOutput']['permissionDecision'] == 'allow'
        orch.ui.ask_approval.assert_not_awaited()
        assert orch.pending_approvals == 0
        assert ad._options(None).hooks['PreToolUse'][0].timeout >= 3600
    asyncio.run(run())


def test_tui_timeout_removes_only_its_screen_and_late_answer_is_ignored(orch):
    from textual.app import App
    from duet.tui.app import DuetApp
    from duet.tui.screens import ApprovalScreen
    class ApprovalApp(App):
        ask_approval = DuetApp.ask_approval
    async def run():
        app = ApprovalApp()
        async with app.run_test() as pilot:
            orch.ui = app
            ad = ClaudeAdapter(orch.cfg.roles['architect'], orch.project, orch.bus, orch.handle_approval, None, '')
            ad.approval_timeout = .15
            first = await ad._can_use_tool('Bash', {'command': 'rm -rf x'}, None)
            assert '사람 승인 대기 중 타임아웃' in first.message
            await pilot.pause()
            assert orch.pending_approvals == 0
            assert not any(isinstance(s, ApprovalScreen) for s in app.screen_stack)
            # A distinct request owns a fresh future; completing it cannot reuse old approval.
            req = ApprovalRequest('architect', 'command', 'second', command='rm -rf y')
            pending = asyncio.create_task(orch._ask_human(req, 'human', None))
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, ApprovalScreen)
            screen.dismiss(Decision(False, 'second denied'))
            assert not (await pending).allow
            assert orch.pending_approvals == 0
    asyncio.run(run())


def test_agy_timeout_interrupts_and_reaps_process_before_discard(orch, monkeypatch):
    import signal
    from duet.adapters.agy import AgyAdapter
    async def run():
        class Stream:
            def __aiter__(self): return self
            async def __anext__(self): await asyncio.Event().wait()
        proc = SimpleNamespace(pid=123456, returncode=None, stdout=Stream(), stderr=Stream(), wait=AsyncMock(return_value=0))
        ad = AgyAdapter(Role('architect', 'agy'), orch.project, orch.bus, None, None, '', reviewer=True)
        ad._system_sent = False; ad._sock = ''; ad._token = ''; ad._stderr = deque()
        ad._argv = lambda prompt: ['mock-agy']
        monkeypatch.setattr('duet.adapters.agy.asyncio.create_subprocess_exec', AsyncMock(return_value=proc))
        signals = []
        monkeypatch.setattr('duet.adapters.agy.os.killpg', lambda pid, sig: signals.append((pid, sig, ad.busy)))
        orch.reviewer = ad
        with pytest.raises(asyncio.TimeoutError): await orch._review_turn(ad, 'old', .01)
        assert signals[0] == (proc.pid, signal.SIGINT, True)
        proc.wait.assert_awaited()
        assert any(sig == signal.SIGKILL for _, sig, _ in signals)
        assert ad.proc is None and orch.reviewer is None
    asyncio.run(run())
