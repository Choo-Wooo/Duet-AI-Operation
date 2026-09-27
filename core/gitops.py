"""턴 단위 git 스냅샷과 정체/핑퐁 감지용 코드 해시."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading
import os
import tempfile
import logging
from contextlib import contextmanager
from pathlib import Path

DIALOGUE_PREFIXES = ("DIALOGUE.md", "DIALOGUE-archive/")


@contextmanager
def temporary_index():
    """Never replace the user's index, even while composing a scoped commit."""
    with tempfile.TemporaryDirectory(prefix='duet-index-') as directory:
        yield {'GIT_INDEX_FILE': str(Path(directory) / 'index')}


class Git:
    def __init__(self, project: Path, enabled: bool = True):
        self.project = project
        self.enabled = enabled and shutil.which("git") is not None
        self._ident: list[str] = []
        self.merge_lock = threading.Lock()
        self.warning = None
        self.last_error = ''
        self.prefix = (self._run('rev-parse', '--show-prefix').stdout.rstrip('\n')
                       if self.enabled else '')

    def _run(self, *args: str, check: bool = False, env: dict | None = None,
             input: str | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *self._ident, *args], cwd=self.project, capture_output=True, text=True,
                              check=check, env={**os.environ, **(env or {})}, input=input)

    def commit_scoped(self, message: str, *, extra_parent: str | None = None,
                      allow_empty: bool = False) -> str | None:
        """Commit only this project's staged tree, leaving outside index entries intact."""
        head = self._run('rev-parse', '--verify', 'HEAD').stdout.strip()
        entries = self._run('ls-files', '--stage', '--full-name', '-z', '--', '.', check=True).stdout
        if any(row.split('\t', 1)[0].split()[-1] != '0' for row in entries.split('\0') if row):
            raise RuntimeError('프로젝트에 미해결 인덱스 항목이 있습니다')
        with temporary_index() as env:
            self._seed_index(head, env)
            old = self._run('ls-files', '--full-name', '-z', '--', '.', env=env, check=True).stdout
            zero = '0' * len(head)
            remove = ''.join(f'0 {zero}\t{name}\0' for name in old.split('\0') if name)
            root = self._run('rev-parse', '--show-toplevel', check=True).stdout.strip()
            self._run('-C', root, 'update-index', '-z', '--index-info', env=env, input=remove + entries, check=True)
            tree = self._run('write-tree', env=env, check=True).stdout.strip()
        return self._publish_tree(tree, head, message, extra_parent=extra_parent, allow_empty=allow_empty)

    def _seed_index(self, head: str, env: dict) -> None:
        if head:
            self._run('read-tree', head, env=env, check=True)
        else:
            self._run('read-tree', '--empty', env=env, check=True)

    def _publish_tree(self, tree: str, head: str, message: str, *, extra_parent: str | None = None,
                      allow_empty: bool = False) -> str | None:
        if head:
            changed = self._run('diff-tree', '--no-commit-id', '--name-only', '-r', '-z', head, tree,
                                check=True).stdout.split('\0')
            prefix = self.prefix
            if any(name and not name.startswith(prefix) for name in changed):
                raise RuntimeError('프로젝트 밖 경로가 커밋에 포함됩니다')
            if not any(changed) and not allow_empty and extra_parent is None:
                return None
        parents = (['-p', head] if head else []) + (['-p', extra_parent] if extra_parent else [])
        sha = self._run('commit-tree', tree, *parents, '-m', message, check=True).stdout.strip()
        self._run('update-ref', '-m', message.splitlines()[0], 'HEAD', sha,
                  head or ('0' * len(sha)), check=True)
        return sha

    def _warn(self, message: str) -> None:
        self.last_error = message
        logging.getLogger(__name__).warning(message)
        if self.warning:
            self.warning(message)

    def is_repo(self) -> bool:
        return self._run("rev-parse", "--is-inside-work-tree").returncode == 0

    def ensure_repo(self) -> str | None:
        """저장소가 아니면 git init. 안내 문구를 돌려준다."""
        if not self.enabled:
            return "git 이 설치되어 있지 않아 턴 스냅샷/롤백을 끕니다."
        msg = None
        if not self.is_repo():
            self._run("init", "-q")
            msg = "git 저장소가 아니어서 git init 을 했습니다 (턴마다 스냅샷 커밋, /rollback 지원)."
        if not self._run("config", "user.email").stdout.strip():
            self._ident = ["-c", "user.name=duet", "-c", "user.email=duet@localhost"]
        self.prefix = self._run('rev-parse', '--show-prefix', check=True).stdout.rstrip('\n')
        return msg

    def ensure_gitignore(self, duet_dirname: str) -> None:
        gi = self.project / ".gitignore"
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
        want = [f"/{duet_dirname}/", ".duet/venv/", ".duet/logs/", ".duet/state.json", ".duet/saves/",
                ".duet/worktrees/", ".duet/work.json", ".duet/models.json", ".duet/web.json",
                ".duet/memory/", ".duet/asks/", ".duet/work/"]
        add = [w for w in want if w not in lines]
        if add:
            block = ("\n" if lines and lines[-1].strip() else "") + "# duet\n" + "\n".join(add) + "\n"
            with gi.open("a", encoding="utf-8") as f:
                f.write(block)

    def snapshot(self, message: str) -> str | None:
        """변경이 있으면 커밋하고 커밋 해시를 돌려준다."""
        with self.merge_lock:
            return self._snapshot_locked(message)

    def _snapshot_locked(self, message: str) -> str | None:
        """Caller owns merge_lock; also used by the snapshot+squash transaction."""
        if not self.enabled:
            return None
        self.last_error = ''
        if self.prefix:
            try:
                if self._run('ls-files', '--unmerged', '-z', '--', '.', check=True).stdout:
                    raise RuntimeError('프로젝트에 미해결 충돌이 있습니다')
                head = self._run('rev-parse', '--verify', 'HEAD').stdout.strip()
                with temporary_index() as env:
                    self._seed_index(head, env)
                    self._run('add', '-A', '--', '.', env=env, check=True)
                    tree = self._run('write-tree', env=env, check=True).stdout.strip()
                sha = self._publish_tree(tree, head, message)
            except (subprocess.CalledProcessError, RuntimeError, OSError) as e:
                self._warn(f'프로젝트 스냅샷 실패; 작업 트리와 실제 인덱스는 변경하지 않았습니다: {e}')
                return None
            if sha:
                try:
                    self._run('restore', '--source=' + sha, '--staged', '--', '.', check=True)
                except (subprocess.CalledProcessError, OSError) as e:
                    self._warn(f'스냅샷 {sha} 커밋 완료, 프로젝트 인덱스 동기화 실패: {e}')
            return sha
        self._run("add", "-A", '--', '.')
        if self._run("diff", "--cached", "--quiet", '--', '.').returncode == 0:
            return None
        r = self._run("commit", "-q", "--no-verify", "-m", message)
        if r.returncode != 0:
            return None
        return self._run("rev-parse", "HEAD").stdout.strip()

    def code_hash(self) -> str | None:
        """대화 문서/설계 문서를 뺀 작업 트리 내용 해시 (현재 HEAD 기준)."""
        if not self.enabled:
            return None
        r = self._run("ls-tree", "-r", '-z', "HEAD", '--', '.')
        if r.returncode != 0:
            return "empty"
        lines = [ln for ln in r.stdout.split('\0') if ln
                 if not ln.split("\t", 1)[-1].startswith((*DIALOGUE_PREFIXES, '.duet/'))]
        return hashlib.sha1("\n".join(lines).encode()).hexdigest()

    def find_turn_commit(self, n: int) -> str | None:
        r = self._run("log", "--format=%H %s", "-n", "2000", '--', '.')
        for line in r.stdout.splitlines():
            sha, _, subj = line.partition(" ")
            if subj.startswith(f"duet #{n} "):
                return sha
        return None

    def hard_reset(self, sha: str) -> bool:
        with self.merge_lock:
            if self.prefix:
                self.last_error = ''
                published = None
                try:
                    # Refuse to overwrite untracked/ignored files restored by the target.
                    target = set(self._run('ls-tree', '-r', '--name-only', '-z', sha, '--', '.', check=True).stdout.split('\0'))
                    untracked = set(self._run('ls-files', '--others', '-z', '--', '.', check=True).stdout.split('\0'))
                    target.discard('')
                    untracked.discard('')
                    target_dirs = {str(parent) for name in target for parent in Path(name).parents}
                    untracked_dirs = {str(parent) for name in untracked for parent in Path(name).parents}
                    if target & untracked or target_dirs & untracked or untracked_dirs & target:
                        self._warn('롤백 거부: 미추적/무시 파일과 복원 경로가 충돌합니다. 작업 트리와 인덱스는 변경하지 않았습니다.')
                        return False
                    head = self._run('rev-parse', '--verify', 'HEAD', check=True).stdout.strip()
                    with temporary_index() as env:
                        self._seed_index(head, env)
                        self._run('restore', '--source=' + sha, '--staged', '--', '.', env=env, check=True)
                        tree = self._run('write-tree', env=env, check=True).stdout.strip()
                    published = self._publish_tree(tree, head, f'duet: rollback {sha}', allow_empty=True)
                    self._run('restore', '--source=' + published, '--staged', '--worktree', '--', '.', check=True)
                    return True
                except (subprocess.CalledProcessError, RuntimeError, OSError) as e:
                    state = (f'롤백 커밋 {published} 생성 후 파일/인덱스 복원 실패; 일부 경로가 복원되었을 수 있습니다'
                             if published else '롤백 실패; HEAD, 작업 트리와 실제 인덱스는 변경하지 않았습니다')
                    self._warn(f'{state}: {e}')
                    return False
            return self._run("reset", "--hard", "-q", sha).returncode == 0
