import asyncio
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock
from duet.core.work import WorkItem, _Wait
from duet.tests.test_work_hardening import board


@pytest.mark.parametrize('state', ['failed', 'cancelled', 'waiting:implement', 'missing'])
def test_blocked_reason_only_actual_blockers(tmp_path, state):
    b = board(tmp_path)
    b.items['ok'] = WorkItem('ok', 'implementer', 'ok', status='merged')
    if state != 'missing': b.items['bad'] = WorkItem('bad', 'implementer', 'bad', status=state)
    b.items['next'] = WorkItem('next', 'implementer', 'next', depends_on=['ok', 'bad'])
    b.schedule()
    reason = b.items['next'].wait_reason
    assert 'ok' not in reason and 'bad' in reason and state in reason


def test_work_seventh_pair_requires_ask(tmp_path):
    """One submission and review form one negotiation; malformed submissions count too."""
    from duet.tests.test_agreement import PLAN
    async def run():
        b = board(tmp_path)
        b.cfg.settings['plan_rounds'] = 100
        item = WorkItem('test', 'implementer', 'test')
        b.items[item.id] = item
        calls = []
        async def turn(*args, **kwargs):
            calls.append(args)
            if len(calls) > 12: raise AssertionError('seventh negotiation started')
            return ('', PLAN, [('PLAN', 'ready')]) if len(calls) % 2 else ('', '', [('REVISE', 'change')])
        b._worker = AsyncMock(return_value=SimpleNamespace())
        b._architect = AsyncMock(return_value=SimpleNamespace())
        b._consultable = turn
        b._turn = turn
        with pytest.raises(_Wait): await b._plan(item, tmp_path)
        assert len(calls) == 12
        assert item.negotiations == 6
    asyncio.run(run())


def test_blocked_transitive_reason_refresh_and_recovery(tmp_path):
    async def run():
        b = board(tmp_path)
        # Reverse insertion order exercises fixed-point labels.
        b.items['last'] = WorkItem('last', 'implementer', 'last', depends_on=['middle'])
        b.items['middle'] = WorkItem('middle', 'implementer', 'middle', depends_on=['first'])
        b.items['first'] = WorkItem('first', 'implementer', 'first', status='failed')
        b.schedule()
        assert 'middle (blocked)' in b.items['last'].wait_reason
        assert 'first (failed)' in b.items['middle'].wait_reason
        b.items['first'].status = 'waiting:verify'
        b.schedule()
        assert 'first (waiting:verify)' in b.items['middle'].wait_reason
        started = []
        async def worker(it): started.append(it.id)
        b._run = worker
        b.items['first'].status = 'merged'
        b.schedule()
        await asyncio.sleep(0)
        assert started == ['middle']
        assert b.items['last'].status == 'queued' and not b.items['last'].wait_reason
        await b.close()
    asyncio.run(run())


def test_work_malformed_pairs_persist_and_resume_grants_six(tmp_path):
    async def run():
        b = board(tmp_path); b.cfg.settings['plan_rounds'] = 100
        it = WorkItem('one', 'implementer', 'task'); b.items[it.id] = it
        b._worker = AsyncMock(return_value=SimpleNamespace())
        b._consultable = AsyncMock(return_value=('', 'bad', []))
        with pytest.raises(_Wait): await b._plan(it, tmp_path)
        assert b._consultable.await_count == 6
        assert board(tmp_path).items['one'].negotiations == 6
        it.status = 'waiting:plan'
        b.closing = True  # avoid starting a real worker on resume
        b.resume('one')
        assert it.negotiation_limit == 12
    asyncio.run(run())
