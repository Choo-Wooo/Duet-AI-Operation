import asyncio
import pytest
from duet.tests.test_agreement import orch
from duet.adapters.fake import FakeAdapter
from duet.adapters.base import TurnResult
from duet.core.policy import ApprovalRequest, Decision, HUMAN


def test_activity_stream_and_terminal(orch, monkeypatch):
    async def run():
        assert orch.status()['activity']['implementer']['state'] == 'idle'
        class Stream(FakeAdapter):
            async def run_turn(self, prompt):
                assert orch.status()['activity'][self.label]['state'] == 'thinking'
                for kind, data, state in [('text', {'text': 'hi'}, 'thinking'),
                    ('tool', {'name': 'Read', 'detail': 'x' * 100}, 'tool'),
                    ('tool_output', {'text': 'ok'}, 'thinking'), ('usage', {'tokens': 2}, 'thinking')]:
                    self.emit(kind, **data)
                    a = orch.status()['activity'][self.label]
                    assert a['state'] == state and a['last_event_at'] >= a['turn_started_at']
                    assert len(a['detail']) <= 80
                return TurnResult('done')
        ad = Stream(orch.cfg.roles['implementer'], orch.project, orch.bus, None, None, '')
        await ad.start()
        orch.adapters['implementer'] = ad
        await orch._run_turn('implementer', 'system', '')
        assert orch.status()['activity']['implementer']['state'] == 'done'
        orch.bus.emit('error', 'implementer', text='failure')
        assert orch.status()['activity']['implementer']['state'] == 'error'
        assert isinstance(orch.status()['server_now'], float)
        await orch.close()
    asyncio.run(run())


@pytest.mark.parametrize('ending', ['allow', 'deny', 'cancel', 'error'])
def test_approval_restores_activity(orch, monkeypatch, ending):
    async def run():
        orch.cfg.policy['ask_architect_opinion_for_human'] = False
        monkeypatch.setattr(orch.policy, 'classify', lambda *a: (HUMAN, 'test'))
        gate = asyncio.Future()
        async def approve(*a): return await gate
        monkeypatch.setattr(orch.ui, 'ask_approval', approve)
        orch.bus.emit('turn_start', 'implementer', n=1, kind='system')
        pending = asyncio.create_task(orch.handle_approval(ApprovalRequest('implementer', 'command', 'test', command='test')))
        await asyncio.sleep(0)
        assert orch.status()['activity']['implementer']['state'] == 'awaiting_approval'
        orch.bus.emit('usage', 'implementer', tokens=1)
        assert orch.status()['activity']['implementer']['state'] == 'awaiting_approval'
        if ending == 'cancel': pending.cancel()
        elif ending == 'error': gate.set_exception(RuntimeError('test'))
        else: gate.set_result(Decision(ending == 'allow', 'test'))
        await asyncio.gather(pending, return_exceptions=True)
        assert orch.status()['activity']['implementer']['state'] == 'thinking'
        assert orch.pending_approvals == 0
        await orch.close()
    asyncio.run(run())


def test_status_throttle_delivers_last_state_and_closes(orch):
    async def run():
        events = []
        orch.bus.subscribe(lambda e: events.append((asyncio.get_running_loop().time(), e)) if e.kind == 'status' else None)
        orch.bus.emit('turn_start', 'implementer', n=1, kind='system')
        for _ in range(30): orch.bus.emit('text', 'implementer', text='hi')
        orch.bus.emit('turn_end', 'implementer', ok=True)
        assert len(events) == 1
        await asyncio.sleep(.55)
        assert len(events) == 2 and events[-1][1].data['activity']['implementer']['state'] == 'done'
        assert events[1][0] - events[0][0] >= .49
        orch.bus.emit('error', 'implementer', text='bad')
        await orch.close()
        assert events[-1][1].data['activity']['implementer']['state'] in ('error', 'idle')
        n = len(events)
        await asyncio.sleep(.55)
        assert len(events) == n
    asyncio.run(run())


def test_parallel_sessions_independent(orch):
    async def run():
        for role in ('implementer#one', 'architect#one', 'implementer#two'):
            orch.bus.emit('turn_start', role, kind='work_plan', info=role.split('#')[1])
        orch.bus.emit('tool', 'implementer#one', name='Read', detail='a')
        s = orch.status()['work_sessions']
        assert len(s) == 3 and len({x['session'] for x in s}) == 3
        assert next(x for x in s if x['session'] == 'implementer#one')['state'] == 'tool'
        orch.bus.emit('turn_end', 'implementer#one', ok=False)
        assert len(orch.status()['work_sessions']) == 2
        assert orch.status()['activity']['architect']['state'] == 'idle'
        await orch.close()
    asyncio.run(run())


def test_start_failure_and_review_completion(orch, monkeypatch):
    async def run():
        async def fail(name):
            assert orch.status()['activity'][name]['state'] == 'starting'
            raise RuntimeError('cannot start')
        monkeypatch.setattr(orch, 'adapter', fail)
        assert await orch._run_turn('implementer', 'system', '') is None
        assert orch.activity['implementer']['state'] == 'error'
        class Review(FakeAdapter):
            async def run_turn(self, prompt):
                assert orch.activity[self.label]['state'] == 'reviewing'
                self.emit('text', text='review')
                assert orch.activity[self.label]['state'] == 'reviewing'
                return TurnResult('ok')
        rev = Review(orch.cfg.main_role(), orch.project, orch.bus, None, None, '', reviewer=True)
        await orch._review_turn(rev, '', 1)
        assert orch.activity[rev.label]['state'] == 'done'
        assert orch.status()['activity']['architect']['state'] == 'idle'
        await orch.close()
    asyncio.run(run())


def test_fake_work_turns_cancel_and_finish(orch):
    from duet.core.config import Role
    from duet.core.work import WorkItem
    async def run():
        gates = {name: asyncio.Event() for name in ('implementer#one','architect#one')}
        class Worker(FakeAdapter):
            async def run_turn(self, prompt):
                self.emit('tool', name='Read', detail='docs')
                await gates[self.label].wait()
                return TurnResult('done')
        item = WorkItem('one','implementer','task')
        ads = [Worker(Role(n,'claude'),orch.project,orch.bus,None,None,'') for n in gates]
        tasks=[asyncio.create_task(orch.work._turn(ad,item,'work_plan','',ad.label)) for ad in ads]
        await asyncio.sleep(0)
        assert len(orch.status()['work_sessions']) == 2
        tasks[0].cancel()
        await asyncio.gather(tasks[0], return_exceptions=True)
        assert len(orch.status()['work_sessions']) == 1
        gates['architect#one'].set()
        await tasks[1]
        assert not orch.status()['work_sessions']
        await orch.close()
    asyncio.run(run())


def test_terminal_transition_survives_immediate_next_turn(orch):
    async def run():
        seen = []
        orch.bus.subscribe(lambda e: seen.append(e.data['activity']['implementer']['state']) if e.kind == 'status' else None)
        orch.bus.emit('turn_start', 'implementer', kind='system')
        orch.bus.emit('turn_end', 'implementer', ok=True)
        orch.bus.emit('turn_start', 'implementer', kind='system')
        await asyncio.sleep(1.1)
        assert 'done' in seen and seen[-1] == 'thinking'
        await orch.close()
    asyncio.run(run())


def test_review_phase_survives_stream_and_timestamps_advance(orch, monkeypatch):
    import duet.core.orchestrator as module
    now = [100.0]
    monkeypatch.setattr(module.time, 'time', lambda: now[0])
    orch.bus.emit('turn_start', 'architect', kind='plan_review')
    for kind in ['tool','text','notice','usage']:
        now[0] += 1
        orch.bus.emit(kind, 'architect', name='Read', text='delta')
        a = orch.status()['activity']['architect']
        assert a['last_event_at'] == now[0]
        assert a['turn_started_at'] == 100
        assert a['state'] == ('tool' if kind == 'tool' else 'reviewing')
