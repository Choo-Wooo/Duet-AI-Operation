"""B1 regressions: isolated real git, asyncio lifecycle, and lock/process probes."""
import asyncio
import os
import signal
import shlex
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from duet.core.config import Config, Role
from duet.core.events import EventBus
from duet.core.gitops import Git
from duet.core.orchestrator import Orchestrator
from duet.core.work import WorkBoard, WorkItem, REPORT_TRIGGER
from duet.core.worktrees import Worktrees, GitError
from duet.core.fsutil import is_link
from duet.core.procs import group_kwargs, kill_tree, pid_alive


def git(path, *args):
    return subprocess.run(['git', *args], cwd=path, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo():
    with tempfile.TemporaryDirectory(prefix='duet-b1-', dir='/tmp' if os.name != 'nt' else None) as directory:
        path = Path(directory).resolve()
        git(path, 'init', '-q', '-b', 'main')
        git(path, 'config', 'user.email', 'test@example.com')
        git(path, 'config', 'user.name', 'Test')
        (path / '.gitignore').write_text('.duet/\nnode_modules/\n.venv/\n')
        (path / 'seed').write_text('seed\n')
        git(path, 'add', '-A')
        git(path, 'commit', '-qm', 'initial')
        yield path


def board(path):
    cfg = Config(path)
    cfg.dir.mkdir(exist_ok=True)
    cfg.roles = {'architect': Role('architect', 'claude', permissions='read_only'),
                 'implementer': Role('implementer', 'codex')}
    orch = SimpleNamespace(cfg=cfg, project=path, git=Git(path), bus=EventBus(),
                           emit_status=lambda: None, inbox=asyncio.Queue())
    return WorkBoard(orch)


@pytest.mark.parametrize('name', ['node_modules', '.venv'])
@pytest.mark.parametrize('staged', [False, True])
def test_h6_links_never_committed(repo, name, staged):
    (repo / name).mkdir()
    (repo / name / 'keep').write_text('dependency')
    wt = Worktrees(repo)
    path = wt.create('deps', 'main')
    assert is_link(path / name)  # Windows 는 정션
    if staged:
        git(path, 'add', '-f', name)
    (path / 'code').write_text('change')
    wt.create('deps', 'main')  # restart/reuse
    wt.commit_all(path, 'work')
    wt.squash_into_base('deps', 'main', 'merge')
    assert name not in git(repo, 'ls-tree', '--name-only', 'HEAD').splitlines()
    assert not is_link(repo / name)
    assert (repo / name / 'keep').read_text() == 'dependency'


def test_diff_ignores_base_only_changes(repo):
    wt = Worktrees(repo)
    path = wt.create('diff', 'main')
    (repo / 'other').write_text('another worker')
    git(repo, 'add', 'other')
    git(repo, 'commit', '-qm', 'base advanced')
    (path / 'mine').write_text('my work')
    assert wt.changed_files(path, 'main') == ['mine']
    assert 'other' not in wt.diff_stat(path, 'main')
    wt.commit_all(path, 'worker commit')
    (path / 'seed').write_text('modified')
    assert wt.changed_files(path, 'main') == ['mine', 'seed']
    with pytest.raises(GitError):
        wt.changed_files(path, 'nonexistent-base')
    with pytest.raises(GitError):
        wt.diff_stat(path, 'nonexistent-base')


@pytest.mark.symlink
def test_h6_preserves_other_files_and_exclude(repo):
    (repo / 'node_modules').mkdir()
    wt = Worktrees(repo)
    exclude = repo / '.git/info/exclude'
    exclude.write_text('# keep existing\n/untouched\n')
    path = wt.create('preserve', 'main')
    wt.create('preserve', 'main')
    assert exclude.read_text().count('/node_modules\n') == 1
    assert '/.venv' not in exclude.read_text()
    assert exclude.read_text().startswith('# keep existing\n/untouched\n')
    (path / 'node_modules').unlink()
    (path / 'node_modules').write_text('ordinary tracked file')
    (path / '.venv').symlink_to('seed')
    git(path, 'add', '-f', 'node_modules', '.venv')
    wt.commit_all(path, 'ordinary files')
    assert git(path, 'show', 'HEAD:node_modules') == 'ordinary tracked file'
    assert git(path, 'show', 'HEAD:.venv') == 'seed'


class ProbeLock:
    def __init__(self):
        self.held = False
        self.entries = 0

    def __enter__(self):
        assert not self.held
        self.held = True
        self.entries += 1

    def __exit__(self, *exc):
        self.held = False


def test_h7_snapshot_locks_all_git_calls(repo, monkeypatch):
    obj = Git(repo)
    lock = ProbeLock()
    obj.merge_lock = lock
    original = obj._run
    def run(*args, **kwargs):
        assert lock.held, args
        return original(*args, **kwargs)
    monkeypatch.setattr(obj, '_run', run)
    (repo / 'new').write_text('new')
    assert obj.snapshot('snapshot')
    assert lock.entries == 1


def test_h7_merge_transaction_owns_same_lock(repo, monkeypatch):
    b = board(repo)
    lock = ProbeLock()
    b.orch.git.merge_lock = b.git_lock = lock
    calls = []
    def snapshot(message):
        assert lock.held
        calls.append('snapshot')
    def squash(*args):
        assert lock.held
        calls.append('squash')
        return 'sha'
    monkeypatch.setattr(b.orch.git, '_snapshot_locked', snapshot)
    monkeypatch.setattr(b.wt, 'squash_into_base', squash)
    assert b._snapshot_and_squash(WorkItem('merge', 'implementer', 'merge')) == 'sha'
    assert calls == ['snapshot', 'squash']
    assert lock.entries == 1


def test_h7_snapshot_waits_for_merge_thread(repo, monkeypatch):
    b = board(repo)
    assert b.git_lock is b.orch.git.merge_lock
    entered, release, attempted, snapshot_entered = (threading.Event() for _ in range(4))
    errors = []
    def snapshot(message):
        if message == 'concurrent':
            snapshot_entered.set()
    def squash(*args):
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(b.orch.git, '_snapshot_locked', snapshot)
    monkeypatch.setattr(b.wt, 'squash_into_base', squash)
    def merge():
        try:
            b._snapshot_and_squash(WorkItem('merge', 'implementer', 'merge'))
        except BaseException as exc:
            errors.append(exc)
    def concurrent():
        attempted.set()
        b.orch.git.snapshot('concurrent')
    first, second = threading.Thread(target=merge), threading.Thread(target=concurrent)
    first.start()
    try:
        assert entered.wait(3)
        second.start()
        assert attempted.wait(3)
        assert not snapshot_entered.wait(0.05)
    finally:
        release.set()
        first.join(3)
        if second.ident is not None:
            second.join(3)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert snapshot_entered.is_set()


@pytest.mark.parametrize('status', ['waiting:implement', 'failed', 'cancelled'])
def test_h8_blocked_reports(repo, status):
    b = board(repo)
    b.items = {'a': WorkItem('a', 'implementer', 'a', status=status),
               'b': WorkItem('b', 'implementer', 'b', depends_on=['a']),
               'c': WorkItem('c', 'implementer', 'c', depends_on=['b'])}
    async def go():
        b.schedule()
        assert b.items['b'].status == b.items['c'].status == 'blocked'
        assert not b.busy()
        assert all(row['wait_reason'] for row in b.snapshot() if row['status'] == 'blocked')
        assert b.orch.inbox.get_nowait() == (None, REPORT_TRIGGER)
        b.schedule()
        assert b.orch.inbox.empty()
    asyncio.run(go())


def test_h8_restart_starts_once(repo, monkeypatch):
    b = board(repo)
    b.items['ready'] = WorkItem('ready', 'implementer', 'ready')
    b.save()
    orch = Orchestrator(b.cfg, EventBus(), SimpleNamespace(), fake=True)
    calls = []
    async def worker(item):
        calls.append(item.id)
    monkeypatch.setattr(orch.work, '_run', worker)
    monkeypatch.setattr(orch, '_run_loop', AsyncMock())
    async def go():
        await orch.run(None)
        await asyncio.sleep(0)
        await orch.run(None)
        assert calls == ['ready']
        await orch.work.close()
    asyncio.run(go())


def test_h8_dependency_recovery_and_live_reporting(repo, monkeypatch):
    b = board(repo)
    b.items = {'c': WorkItem('c', 'implementer', 'c', depends_on=['b']),
               'b': WorkItem('b', 'implementer', 'b', depends_on=['a']),
               'a': WorkItem('a', 'implementer', 'a', status='waiting:verify')}
    started = []
    async def worker(item):
        started.append(item.id)
        await asyncio.Event().wait()
    monkeypatch.setattr(b, '_run', worker)
    async def go():
        b.schedule()
        assert b.items['c'].status == 'blocked'
        b.orch.inbox.get_nowait()
        b._report_pending = False
        b._touch(b.items['a'], 'merged')
        b.schedule()
        await asyncio.sleep(0)
        assert started == ['b']
        assert b.busy() and b.orch.inbox.empty()
        await b.close()
    asyncio.run(go())


def test_h9_close_does_not_schedule(repo, monkeypatch):
    b = board(repo)
    b.items['next'] = WorkItem('next', 'implementer', 'next')
    started = []
    async def worker(item):
        started.append(item.id)
    monkeypatch.setattr(b, '_run', worker)
    async def go():
        async def running():
            try:
                await asyncio.Event().wait()
            finally:
                b.schedule()
        b.tasks['old'] = asyncio.create_task(running())
        await asyncio.sleep(0)
        await b.close()
        b.schedule()
        await asyncio.sleep(0)
        assert started == []
    asyncio.run(go())


@pytest.mark.parametrize('timeout', [False, True])
def test_h9_test_process_group_cleanup(repo, monkeypatch, timeout):
    b = board(repo)
    b.cfg.settings['work_test_timeout'] = 0.01
    started = asyncio.Event()
    async def communicate():
        started.set()
        await asyncio.Event().wait()
    proc = SimpleNamespace(pid=123456, communicate=communicate, wait=AsyncMock(), kill=lambda: None)
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr('duet.core.work.create_shell', spawn)
    killed = []
    monkeypatch.setattr('duet.core.work.kill_tree', killed.append)
    async def go():
        task = asyncio.create_task(b._run_test(WorkItem('test', 'implementer', 'test'), repo, 'pwd'))
        await started.wait()
        if timeout:
            code, _ = await task
            assert code == 124
        else:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert killed == [proc.pid]
        proc.wait.assert_awaited_once()
        assert spawn.call_args.args == ('pwd',)
    asyncio.run(go())


@pytest.mark.parametrize('timeout', [False, True])
def test_h9_real_descendants_stop(repo, timeout):
    b = board(repo)
    b.cfg.settings['work_test_timeout'] = 0.6 if timeout else 10
    b.orch._ask_human = AsyncMock(return_value=SimpleNamespace(allow=True))
    script = repo / 'child.py'
    script.write_text('import os, time\nfrom pathlib import Path\n'
                      'Path("child.pid").write_text(str(os.getpid()))\n'
                      'while True:\n    time.sleep(0.05)\n')
    command = f'{shlex.quote(sys.executable)} {shlex.quote(str(script))} & wait'
    async def go():
        task = asyncio.create_task(b._run_test(WorkItem('real', 'implementer', 'real'), repo, command))
        pid = None
        try:
            for _ in range(200):
                if (repo / 'child.pid').exists():
                    pid = int((repo / 'child.pid').read_text())
                    break
                await asyncio.sleep(0.01)
            assert pid is not None
            if timeout:
                code, _ = await task
                assert code == 124
            else:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            for _ in range(200):
                if not pid_alive(pid):
                    pid = None
                    break
                await asyncio.sleep(0.01)
            else:
                pytest.fail('descendant survived cancellation/timeout')
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if pid:
                kill_tree(pid)
    asyncio.run(go())


def test_process_group_kwargs_per_platform():
    kw = group_kwargs()
    if os.name == 'nt':
        assert kw['creationflags'] & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kw == {'start_new_session': True}
