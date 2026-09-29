"""D3: real repositories; only agent responses and sparse failure are simulated."""
import asyncio
from pathlib import Path
import subprocess

import pytest

from duet.core.gitops import Git
from duet.core.worktrees import Worktrees, GitError
from duet.core.fsutil import is_link
from duet.core import work as work_mod
from duet.core.config import Config, Role
from duet.core.events import EventBus
from duet.core.dialogue import Dialogue
from duet.core.orchestrator import Orchestrator
from duet.tests.test_work import NoUI, wait_report
from duet.adapters.fake import FakeAdapter


def git(path, *args):
    p = subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


pytestmark = pytest.mark.git245


@pytest.fixture(params=['app', 'space 한글/nested'])
def repo(tmp_path, request):
    git(tmp_path, 'init', '-q', '-b', 'main')
    git(tmp_path, 'config', 'user.name', 'Test')
    git(tmp_path, 'config', 'user.email', 'test@example.com')
    p = tmp_path / request.param
    p.mkdir(parents=True)
    (tmp_path/'sibling').mkdir()
    (tmp_path/'sibling/file').write_text('outside\n')
    (p/'code').write_text('old\n')
    (p/'.gitignore').write_text('.duet/\nnode_modules/\nignored\n')
    git(tmp_path, 'add', '-A'); git(tmp_path, 'commit', '-qm', 'initial')
    return tmp_path, p


def outside_dirty(root):
    (root/'sibling/file').write_text('staged\n')
    git(root, 'add', 'sibling/file')
    (root/'sibling/file').write_text('unstaged\n')
    (root/'sibling/new').write_text('untracked')
    return outside_state(root)


def outside_state(root):
    return (git(root, 'ls-files', '--stage', '--', 'sibling'),
            (root/'sibling/file').read_bytes(), (root/'sibling/new').read_bytes())


def assert_scoped(root, project, sha):
    prefix = project.relative_to(root).as_posix() + '/'
    names = git(root, '-c', 'core.quotePath=false', 'diff-tree', '--no-commit-id', '--name-only', '-r', sha).splitlines()
    assert names and all(n.startswith(prefix) for n in names), names


def test_snapshot_excludes_pre_staged_outside(repo):
    root, p = repo
    before = outside_dirty(root)
    (p/'code').write_text('new\n'); (p/'added').write_text('new')
    sha = Git(p).snapshot('duet #1 [implementer] change')
    assert sha
    assert_scoped(root, p, sha)
    assert outside_state(root) == before
    assert Git(p).snapshot('outside only') is None


def test_hash_and_turn_lookup_are_project_relative(repo):
    root, p = repo
    g = Git(p)
    (p/'code').write_text('one')
    git(root, 'add', '-A'); git(root, 'commit', '-qm', 'duet #1 [worker] first')
    first = git(root, 'rev-parse', 'HEAD'); code_hash = g.code_hash()
    (root/'sibling/file').write_text('other')
    git(root, 'add', '-A'); git(root, 'commit', '-qm', 'duet #1 [worker] sibling')
    assert g.find_turn_commit(1) == first
    assert g.code_hash() == code_hash
    (p/'DIALOGUE.md').write_text('dialogue')
    (p/'.duet').mkdir(); (p/'.duet/memory.md').write_text('memory')
    git(p, 'add', '-f', 'DIALOGUE.md', '.duet/memory.md'); git(p, 'commit', '-qm', 'docs')
    assert g.code_hash() == code_hash
    (p/'code').write_text('two')
    git(p, 'add', 'code'); git(p, 'commit', '-qm', 'duet #1 [worker] latest')
    assert g.find_turn_commit(1) == git(root, 'rev-parse', 'HEAD')
    assert g.code_hash() != code_hash


def test_rollback_preserves_outside_index_untracked_and_lineage(repo):
    root, p = repo
    target = git(root, 'rev-parse', 'HEAD')
    (p/'code').unlink(); (p/'later').write_text('delete on restore')
    (root/'sibling/file').write_text('new sibling commit\n')
    git(root, 'add', '-A'); git(root, 'commit', '-qm', 'later')
    before_head = git(root, 'rev-parse', 'HEAD')
    before = outside_dirty(root)
    (p/'untracked').write_text('keep'); (p/'ignored').write_text('keep ignored')
    assert Git(p).hard_reset(target)
    assert (p/'code').read_text() == 'old\n' and not (p/'later').exists()
    assert (p/'untracked').read_text() == 'keep' and (p/'ignored').read_text() == 'keep ignored'
    assert not git(p, 'ls-files', '--', 'untracked', 'ignored')
    assert outside_state(root) == before
    assert git(root, 'rev-parse', 'HEAD^') == before_head
    assert 'rollback' in git(root, 'log', '-1', '--format=%s')
    assert_scoped(root, p, 'HEAD')


def test_sparse_create_reuse_deps_and_scoped_diff(repo):
    root, p = repo
    (p/'node_modules').mkdir(); (p/'node_modules/keep').write_text('dep')
    wt = Worktrees(p)
    path = wt.create('one', 'main')
    expected = p/'.duet/worktrees/one'/p.relative_to(root)
    assert path == expected
    assert wt.path_for('one') == path
    assert not (p/'.duet/worktrees/one/sibling').exists()
    assert is_link(path/'node_modules')  # Windows 는 정션
    assert wt.create('one', 'main') == path
    (path/'code').write_text('work'); (path/'new').write_text('new')
    assert set(wt.changed_files(path, 'main')) == {'code','new'}
    assert 'sibling' not in wt.diff_stat(path, 'main')
    git(path, 'add', '-f', 'node_modules')
    wt.commit_all(path, 'work')
    assert not git(path, 'ls-files', '--', 'node_modules')
    assert_scoped(root, p, wt.branch_for('one'))
    wt.remove('one')
    assert not (p/'.duet/worktrees/one').exists()


@pytest.mark.parametrize('fail', [False, True])
def test_sparse_probe_cleanup_and_rejection(repo, monkeypatch, fail):
    root, p = repo
    wt = Worktrees(p)
    original = wt.git
    calls = []
    def wrapped(*args, **kwargs):
        if args and args[0] == 'sparse-checkout':
            calls.append(args)
            if fail: raise GitError('injected sparse-checkout failure')
        return original(*args, **kwargs)
    monkeypatch.setattr(wt, 'git', wrapped)
    refs = git(root, 'show-ref')
    trees = git(root, 'worktree', 'list', '--porcelain')
    reason = wt.check_ready()
    assert calls
    assert bool(reason) == fail
    if fail: assert 'sparse' in reason
    assert git(root, 'show-ref') == refs
    assert git(root, 'worktree', 'list', '--porcelain') == trees


def test_merge_and_squash_preserve_outside(repo):
    root, p = repo
    wt = Worktrees(p); path = wt.create('one', 'main')
    (path/'worker').write_text('work'); wt.commit_all(path, 'worker')
    (p/'base').write_text('base'); (root/'sibling/file').write_text('new base outside')
    git(root,'add','-A'); git(root,'commit','-qm','base with sibling')
    before = outside_dirty(root)
    assert wt.merge_base_into(path, 'main') == []
    assert (path/'base').read_text() == 'base'
    sha = wt.squash_into_base('one', 'main', 'squash')
    assert sha and (p/'worker').read_text() == 'work'
    assert_scoped(root,p,sha)
    assert outside_state(root) == before


def test_subdir_conflict_resolved_and_scoped(repo):
    root,p = repo; wt=Worktrees(p); path=wt.create('conflict','main')
    (path/'code').write_text('worker\n'); wt.commit_all(path,'worker')
    (p/'code').write_text('base\n'); Git(p).snapshot('base')
    before=outside_dirty(root)
    assert wt.merge_base_into(path,'main') == ['code']
    assert wt.in_merge(path)
    assert '<<<<<<<' in (path/'code').read_text()
    (path/'code').write_text('resolved\n'); wt.finish_merge(path)
    assert not wt.in_merge(path)
    sha=wt.squash_into_base('conflict','main','resolved')
    assert_scoped(root,p,sha)
    assert outside_state(root)==before


def test_fake_parallel_flow_cwd_and_final_commit(repo, monkeypatch):
    root,p=repo
    cfg=Config(p); cfg.dir.mkdir(); cfg.roles={'architect':Role('architect','claude',permissions='read_only'),
        'implementer':Role('implementer','codex',max_sessions=2)}
    cfg.save_roles(); cfg.save_state(); Dialogue(p).ensure()
    o=Orchestrator(cfg,EventBus(),NoUI(),fake=True)
    original=FakeAdapter._work_turn; seen=[]
    async def work(self,prompt):
        if self.role.name.startswith('implementer#'):
            expected=p/'.duet/worktrees/one'/p.relative_to(root)
            seen.append((self.turn_kind,self.project))
            assert self.project==expected
        return await original(self,prompt)
    monkeypatch.setattr(FakeAdapter,'_work_turn',work)
    shell = work_mod.create_shell
    test_cwds = []
    async def checked_shell(command, **kwargs):
        # Observe the real subprocess call without replacing its execution.
        test_cwds.append(Path(kwargs['cwd']))
        assert test_cwds[-1] == p/'.duet/worktrees/one'/p.relative_to(root)
        return await shell(command, **kwargs)
    monkeypatch.setattr(work_mod, 'create_shell', checked_shell)
    async def run():
        before=outside_dirty(root)
        assert o.work.submit([{'id':'one','role':'implementer','task':'one.txt'}],1)==[]
        await wait_report(o,timeout=30)
        assert o.work.items['one'].status=='merged',o.work.summary()
        assert outside_state(root)==before
        assert_scoped(root,p,o.work.items['one'].merged_commit)
        await o.close()
    asyncio.run(run())
    assert {kind for kind,_ in seen} >= {'work_plan','work_implement'}
    assert len(test_cwds) >= 2  # verification and integration both execute.
    assert (p/'one.txt').exists() and not (p/'.duet/worktrees/one').exists()


def test_squash_refuses_dirty_project_without_erasing_it(repo):
    root,p=repo; wt=Worktrees(p); path=wt.create('dirty','main')
    (path/'worker').write_text('work'); wt.commit_all(path,'work')
    before=outside_dirty(root)
    (p/'code').write_text('unsaved local work')
    with pytest.raises(GitError): wt.squash_into_base('dirty','main','must refuse')
    assert (p/'code').read_text() == 'unsaved local work'
    assert outside_state(root) == before


def test_squash_conflict_restores_only_project(repo):
    root,p=repo; wt=Worktrees(p); path=wt.create('conflict','main')
    (path/'code').write_text('worker\n'); wt.commit_all(path,'work')
    (p/'code').write_text('base\n'); Git(p).snapshot('base')
    head=git(root,'rev-parse','HEAD'); before=outside_dirty(root)
    with pytest.raises(GitError): wt.squash_into_base('conflict','main','must refuse')
    assert git(root,'rev-parse','HEAD') == head
    assert (p/'code').read_text() == 'base\n'
    assert outside_state(root) == before
    assert not git(p,'diff','--cached','--','.')


@pytest.mark.parametrize('shape',['file_to_dir','dir_to_file'])
def test_rollback_refuses_untracked_path_collisions(repo, shape):
    root,p=repo
    if shape=='file_to_dir': (p/'collision').write_text('tracked')
    else:
        (p/'collision').mkdir(); (p/'collision/tracked').write_text('tracked')
    Git(p).snapshot('target'); target=git(root,'rev-parse','HEAD')
    git(p,'rm','-rf','collision'); Git(p).snapshot('remove')
    if shape=='file_to_dir':
        (p/'collision').mkdir(); keep=p/'collision/untracked'
    else: keep=p/'collision'
    keep.write_text('do not erase')
    head=git(root,'rev-parse','HEAD')
    assert not Git(p).hard_reset(target)
    assert keep.read_text()=='do not erase' and git(root,'rev-parse','HEAD')==head


def test_sparse_failure_rejects_submit_and_failed_creation_cleans_up(repo, monkeypatch):
    root,p=repo
    cfg=Config(p); cfg.dir.mkdir(); cfg.roles={'architect':Role('architect','claude'), 'implementer':Role('implementer','codex')}
    o=Orchestrator(cfg,EventBus(),NoUI(),fake=True); wt=o.work.wt
    original=wt.git
    def fail(*args,**kwargs):
        if args and args[0]=='sparse-checkout': raise GitError('sparse-checkout injected failure')
        return original(*args,**kwargs)
    monkeypatch.setattr(wt,'git',fail)
    refs=git(root,'show-ref'); trees=git(root,'worktree','list','--porcelain')
    errors=o.work.submit([{'id':'fail','role':'implementer','task':'task'}],1)
    assert errors and 'sparse-checkout' in errors[0]
    assert not o.work.items and not o.work.tasks
    with pytest.raises(GitError): wt.create('fail','main')
    assert git(root,'show-ref')==refs and git(root,'worktree','list','--porcelain')==trees
    assert not (p/'.duet/worktrees/fail').exists()


@pytest.mark.parametrize('kind',['add','delete','rename'])
def test_snapshot_preserves_other_staged_entries(repo, kind):
    root,p=repo
    if kind=='add':
        (root/'sibling/added').write_text('staged'); git(root,'add','sibling/added')
    elif kind=='delete': git(root,'rm','sibling/file')
    else: git(root,'mv','sibling/file','sibling/renamed')
    before=git(root,'ls-files','--stage','-z','--','sibling')
    (p/'code').unlink(); (p/'replacement').write_text('new')
    sha=Git(p).snapshot('duet #2 [worker] replace')
    assert_scoped(root,p,sha)
    assert git(root,'ls-files','--stage','-z','--','sibling')==before
    assert not (p/'code').exists()


def test_external_worker_commit_never_enters_squash(repo):
    root,p=repo; wt=Worktrees(p); path=wt.create('one','main')
    tree_root=Path(git(path,'rev-parse','--show-toplevel'))
    # Cone mode includes root files. Deliberately commit one outside prefix.
    (tree_root/'outside.txt').write_text('must not merge')
    git(tree_root,'add','outside.txt'); git(tree_root,'commit','-qm','outside worker commit')
    (path/'worker').write_text('inside'); wt.commit_all(path,'inside')
    before=outside_dirty(root)
    sha=wt.squash_into_base('one','main','squash')
    assert_scoped(root,p,sha)
    assert not (root/'outside.txt').exists()
    assert outside_state(root)==before


def test_worker_commit_and_merge_preserve_external_staging(repo):
    root,p=repo; wt=Worktrees(p); path=wt.create('one','main')
    tree_root=Path(git(path,'rev-parse','--show-toplevel'))
    (tree_root/'outside.txt').write_text('staged')
    git(tree_root,'add','outside.txt')
    staged=git(tree_root,'ls-files','--stage','--','outside.txt')
    (path/'worker').write_text('work'); wt.commit_all(path,'worker')
    assert not git(tree_root,'ls-tree','HEAD','--','outside.txt')
    (p/'base').write_text('base'); Git(p).snapshot('base')
    assert wt.merge_base_into(path,'main')==[]
    assert git(tree_root,'ls-files','--stage','--','outside.txt')==staged
    assert (tree_root/'outside.txt').read_text()=='staged'
    assert not git(tree_root,'ls-tree','HEAD','--','outside.txt')


def test_merge_nonoverlapping_edits_in_same_file(repo):
    root,p=repo
    (p/'code').write_text('one\n'+'middle\n'*8+'last\n'); Git(p).snapshot('seed')
    wt=Worktrees(p); path=wt.create('one','main')
    (path/'code').write_text((path/'code').read_text().replace('one','worker'))
    wt.commit_all(path,'worker')
    (p/'code').write_text((p/'code').read_text().replace('last','base'))
    Git(p).snapshot('base')
    assert wt.merge_base_into(path,'main')==[]
    assert (path/'code').read_text().startswith('worker') and (path/'code').read_text().endswith('base\n')


def test_diff_uses_project_paths_for_delete_and_rename(repo):
    root,p=repo; wt=Worktrees(p); path=wt.create('one','main')
    (path/'code').rename(path/'renamed code')
    (path/'.gitignore').unlink()
    names=wt.changed_files(path,'main')
    assert '.gitignore' in names and 'renamed code' in names
    assert all(not name.startswith(p.relative_to(root).as_posix()+'/') for name in names)
    assert 'sibling' not in wt.diff_stat(path,'main')


@pytest.mark.parametrize('scenario', ['add_add', 'delete_modify', 'rename_edit'])
def test_r1_native_content_merge(repo, scenario):
    root,p=repo; wt=Worktrees(p); path=wt.create('r1','main')
    if scenario=='add_add':
        (path/'added').write_text('worker addition\n'); name='added'
    elif scenario=='delete_modify':
        (path/'code').unlink(); name='code'
    else:
        (path/'code').rename(path/'renamed'); name='renamed'
    wt.commit_all(path,'worker')
    (p/('added' if scenario=='add_add' else 'code')).write_text('base edit\n')
    Git(p).snapshot('base')
    before=outside_dirty(root)
    conflicts=wt.merge_base_into(path,'main')
    if scenario=='rename_edit':
        assert conflicts==[]
        assert (path/'renamed').read_text()=='base edit\n'
        assert not (path/'code').exists()
    else:
        assert name in conflicts and wt.unresolved(path)
        assert (path/name).exists()
        assert 'base edit' in (path/name).read_text()
        if scenario=='add_add': assert '<<<<<<<' in (path/name).read_text()
        head=git(path,'rev-parse','HEAD')
        with pytest.raises(GitError): wt.finish_merge(path)
        assert git(path,'rev-parse','HEAD')==head and wt.unresolved(path)
        (path/name).write_text('resolved base edit\n')
        wt.finish_merge(path)
        assert not wt.unresolved(path)
    sha=wt.squash_into_base('r1','main','resolved')
    if sha: assert_scoped(root,p,sha)
    assert 'base edit' in (p/name).read_text()
    assert outside_state(root)==before


def test_r2_external_unmerged_snapshot_is_not_silent_or_staging(repo, caplog):
    root,p=repo
    git(root,'checkout','-qb','side')
    (root/'sibling/file').write_text('side\n'); git(root,'commit','-qam','side')
    git(root,'checkout','-q','main')
    (root/'sibling/file').write_text('main\n'); git(root,'commit','-qam','main')
    merged=subprocess.run(['git','merge','side'],cwd=root,capture_output=True)
    assert merged.returncode==1
    before=git(root,'ls-files','--stage','-z')
    (p/'code').write_text('pending project work\n')
    g=Git(p); notices=[]; g.warning=notices.append
    sha=g.snapshot('duet snapshot')
    if sha:
        assert_scoped(root,p,sha)
        assert git(root,'ls-files','--unmerged','-z')
    else:
        assert git(root,'ls-files','--stage','-z')==before
        assert notices and caplog.records
    assert (p/'code').read_text()=='pending project work\n'


@pytest.mark.parametrize('operation', ['snapshot', 'rollback'])
def test_r2_r3_update_ref_failure_preserves_index_and_worktree(repo, monkeypatch, caplog, operation):
    root,p=repo; target=git(root,'rev-parse','HEAD')
    (p/'code').write_text('committed later'); Git(p).snapshot('later')
    (p/'code').write_text('staged'); git(p,'add','code')
    (p/'code').write_text('unstaged'); outside_dirty(root)
    before=git(root,'ls-files','--stage','-z'); head=git(root,'rev-parse','HEAD')
    g=Git(p); notices=[]; g.warning=notices.append
    original=g._run
    def fail(*args,**kwargs):
        if args[0]=='update-ref':
            raise subprocess.CalledProcessError(1,['git',*args],stderr='injected guard failure')
        return original(*args,**kwargs)
    monkeypatch.setattr(g,'_run',fail)
    assert not (g.snapshot('fail') if operation=='snapshot' else g.hard_reset(target))
    assert (p/'code').read_text()=='unstaged'
    assert git(root,'ls-files','--stage','-z')==before
    assert git(root,'rev-parse','HEAD')==head
    assert notices and caplog.records


def test_r1_unsupported_git_rejects_parallel_before_probe(repo, monkeypatch):
    root,p=repo; wt=Worktrees(p); original=wt.git
    def version(*args,**kwargs):
        if args==('--version',): return subprocess.CompletedProcess(args,0,'git version 2.37.9\n','')
        return original(*args,**kwargs)
    monkeypatch.setattr(wt,'git',version)
    before=git(root,'worktree','list','--porcelain')
    assert '2.45' in (wt.check_ready() or '')
    assert git(root,'worktree','list','--porcelain')==before
