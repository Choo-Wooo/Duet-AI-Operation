"""B2-7/8/15a: real temp files and git; injected I/O failure only in tmp_path."""
import json
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
import pytest
from duet.core.config import Config, Role
from duet.core.gitops import Git
from duet.core.textutil import save_memory
from duet.tests.test_work_hardening import board, git, ProbeLock


@pytest.mark.parametrize('payload', [b'\xff', b'[]', b'null', b'42'])
@pytest.mark.parametrize('kind', ['direct', 'state', 'work'])
def test_invalid_encoding_or_nonobject_json_backed_up(tmp_path, payload, kind):
    from duet.core.storage import load_json
    b = board(tmp_path); b.cfg.save_roles()
    path = b.cfg.dir / ('state.json' if kind == 'state' else 'work.json')
    path.write_bytes(payload)
    warnings = []
    b.orch.bus.subscribe(lambda event: warnings.append(event) if event.kind == 'notice' else None)
    if kind == 'direct': assert load_json(path, warnings.append) is None
    elif kind == 'state':
        b.cfg.load()
        warnings.extend(b.cfg.load_warnings)
    else: b._load()
    backups = list(path.parent.glob(path.name+'.corrupt-*'))
    assert len(backups) == 1 and backups[0].read_bytes() == payload
    assert warnings


def test_hash_excludes_all_duet_and_ignore_is_idempotent():
    with tempfile.TemporaryDirectory(dir='/tmp') as root:
        p = Path(root)
        git(p, 'init', '-q')
        git(p, 'config', 'user.name', 'Test')
        git(p, 'config', 'user.email', 'test@example.com')
        (p/'.duet').mkdir()
        (p/'.duet/state.json').write_text('one')
        (p/'code').write_text('one')
        git(p, 'add', '-A'); git(p, 'commit', '-qm', 'one')
        g = Git(p)
        before = g.code_hash()
        (p/'.duet/state.json').write_text('two')
        git(p, 'add', '-A'); git(p, 'commit', '-qm', 'two')
        assert g.code_hash() == before
        (p/'code').write_text('two')
        git(p, 'add', '-A'); git(p, 'commit', '-qm', 'three')
        assert g.code_hash() != before
        (p/'.gitignore').write_text('# existing\n')
        g.ensure_gitignore('duet'); g.ensure_gitignore('duet')
        content = (p/'.gitignore').read_text()
        assert content.startswith('# existing\n')
        for name in ['memory', 'asks', 'work']:
            assert content.count(f'.duet/{name}/\n') == 1


@pytest.mark.parametrize('kind', ['state', 'work', 'memory'])
def test_atomic_replace_failure_preserves_old_file(tmp_path, monkeypatch, kind):
    b = board(tmp_path)
    cfg = b.cfg
    path = cfg.dir / (kind+'.json') if kind != 'memory' else cfg.dir/'memory/architect.md'
    path.parent.mkdir(exist_ok=True)
    path.write_text('previous')
    def fail(src, dst):
        assert Path(src).parent == path.parent
        raise OSError('injected replace failure')
    monkeypatch.setattr(os, 'replace', fail)
    try:
        if kind == 'state': cfg.save_state()
        elif kind == 'work': b.save()
        else: save_memory(tmp_path, 'architect', 'new')
    except OSError:
        pass
    assert path.read_text() == 'previous'
    assert not list(path.parent.glob('*.tmp'))


@pytest.mark.parametrize('kind', ['state', 'work'])
def test_corrupt_json_is_backed_up_and_warned(tmp_path, kind):
    b = board(tmp_path)
    cfg = b.cfg
    cfg.save_roles()
    path = cfg.dir / (kind+'.json')
    events = []
    b.orch.bus.subscribe(events.append)
    for _ in range(2):
        path.write_text('{broken')
        if kind == 'state': cfg.load()
        else: b._load()
    backups = list(cfg.dir.glob(kind+'.json.corrupt-*'))
    assert len(backups) == 2
    assert all(p.read_text() == '{broken' for p in backups)
    if kind == 'state': assert cfg.load_warnings
    else: assert any(e.kind == 'notice' for e in events)


def test_hard_reset_takes_shared_lock(tmp_path, monkeypatch):
    g = Git(tmp_path)
    lock = g.merge_lock = ProbeLock()
    def run(*args):
        assert lock.held
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(g, '_run', run)
    assert g.hard_reset('fake-sha')
    assert lock.entries == 1


def test_reset_waits_for_merge_critical_section(tmp_path, monkeypatch):
    g = Git(tmp_path)
    ran = threading.Event()
    monkeypatch.setattr(g, '_run', lambda *a: (ran.set() or SimpleNamespace(returncode=0)))
    with g.merge_lock:
        t = threading.Thread(target=g.hard_reset, args=('fake-sha',))
        t.start()
        try: assert not ran.wait(.05)
        finally: pass
    t.join(1)
    assert ran.is_set() and not t.is_alive()


@pytest.mark.parametrize('stage', ['write', 'flush'])
def test_atomic_write_failure_cleans_temporary(tmp_path, monkeypatch, stage):
    from duet.core import storage
    target = tmp_path/'state.json'
    target.write_text('old')
    original = storage.tempfile.NamedTemporaryFile
    class FailingStream:
        def __init__(self, **kw): self.stream = original(**kw)
        def __enter__(self): return self
        def __exit__(self, *args): self.stream.close()
        @property
        def name(self): return self.stream.name
        def write(self, text):
            self.stream.write(text[:1])
            if stage == 'write': raise OSError('write failed')
        def flush(self): raise OSError('flush failed')
    monkeypatch.setattr(storage.tempfile, 'NamedTemporaryFile', FailingStream)
    with pytest.raises(OSError): storage.atomic_write(target, 'new')
    assert target.read_text() == 'old'
    assert list(tmp_path.iterdir()) == [target]


def test_storage_roundtrip_and_permission_error_is_not_corruption(tmp_path, monkeypatch):
    from duet.core.storage import load_json
    from duet.core.work import WorkItem
    b = board(tmp_path)
    b.cfg.save_roles()
    b.cfg.state.last_n = 42
    b.cfg.save_state()
    cfg = Config(tmp_path); cfg.load()
    assert cfg.state.last_n == 42
    b.items['one'] = WorkItem('one', 'implementer', 'task', status='merged')
    b.save()
    assert board(tmp_path).items['one'].status == 'merged'
    assert 'body' in save_memory(tmp_path, 'architect', 'body').read_text()
    assert load_json(tmp_path/'missing', lambda _: pytest.fail('unexpected warning')) is None
    def denied(*a, **k): raise PermissionError('denied')
    monkeypatch.setattr(Path, 'read_text', denied)
    with pytest.raises(PermissionError): load_json(b.path, lambda _: pytest.fail('not corruption'))
    assert not list(b.cfg.dir.glob('*.corrupt-*'))


def test_config_corruption_notice_reaches_orchestrator(tmp_path):
    from duet.core.events import EventBus
    from duet.core.orchestrator import Orchestrator
    cfg = Config(tmp_path); cfg.dir.mkdir()
    cfg.roles = {'architect': Role('architect', 'claude')}
    cfg.save_roles(); cfg.state_file.write_text('{bad')
    cfg.load()
    bus = EventBus(); events = []; bus.subscribe(events.append)
    Orchestrator(cfg, bus, SimpleNamespace(), fake=True)
    assert any(e.kind == 'notice' and '.corrupt-' in e.data['text'] for e in events)


@pytest.mark.parametrize('kind', ['state', 'work'])
def test_startup_corruption_notice_reaches_late_ui_subscriber(tmp_path, kind):
    import asyncio
    from unittest.mock import AsyncMock
    from duet.core.events import EventBus
    from duet.core.orchestrator import Orchestrator
    cfg = Config(tmp_path); cfg.dir.mkdir()
    cfg.roles = {'architect': Role('architect', 'claude')}
    cfg.save_roles(); (cfg.dir/(kind+'.json')).write_text('{bad')
    cfg.load()
    bus = EventBus()
    o = Orchestrator(cfg, bus, SimpleNamespace(), fake=True)
    # TUI/console/web subscribe after Orchestrator construction.
    events = []; bus.subscribe(events.append)
    o._run_loop = AsyncMock()
    asyncio.run(o.run(None))
    assert any(e.kind == 'notice' and '.corrupt-' in e.data['text'] for e in events)
