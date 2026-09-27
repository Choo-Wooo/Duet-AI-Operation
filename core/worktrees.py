"""병렬 작업용 git 워크트리·로컬 작업 브랜치 관리.

- 작업 브랜치(duet/work/<id>)는 duet 시작 시 체크아웃돼 있던 기준 브랜치에서 로컬로만 만든다.
- 끝나면 기준 브랜치에 squash 병합하고 워크트리와 작업 브랜치를 지운다. 원격에는 절대 올리지 않는다
  (pre-push 훅으로 duet/work/* push 를 막는다). push 는 사람이 기준 브랜치만 한다.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import json
import hashlib
from pathlib import Path

from .gitops import Git, temporary_index

BRANCH_PREFIX = "duet/work/"
WORKTREE_DIR = Path(".duet") / "worktrees"
# 워크트리에서도 쓸 수 있게 원본의 의존성 폴더를 링크 (git 에서 무시되는 것만)
SHARED_DEP_DIRS = ("node_modules", ".venv", "venv", ".tox", "vendor/bundle")

PRE_PUSH = """#!/bin/sh
# duet: 로컬 전용 작업 브랜치(duet/work/*)는 원격에 올리지 않는다.
while read local_ref local_sha remote_ref remote_sha; do
  case "$local_ref$remote_ref" in
    *refs/heads/duet/work/*)
      echo "duet: 로컬 작업 브랜치($local_ref)는 push 하지 않습니다. 기준 브랜치만 올리세요." >&2
      exit 1;;
  esac
done
exit 0
"""
PRE_PUSH_MARK = "duet: 로컬 전용 작업 브랜치"


class GitError(RuntimeError):
    pass


def valid_id(s: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", s or ""))


class Worktrees:
    def __init__(self, project: Path):
        self.project = project
        self._ident: list[str] = []
        if not self.git("config", "user.email").stdout.strip():
            self._ident = ["-c", "user.name=duet", "-c", "user.email=duet@localhost"]

    @property
    def prefix(self) -> str:
        return self.git('rev-parse', '--show-prefix', check=True).stdout.rstrip('\n')

    def git(self, *args: str, cwd: Path | None = None, check: bool = False,
            env: dict | None = None, input: str | None = None) -> subprocess.CompletedProcess:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true", **(env or {})}
        r = subprocess.run(["git", *self._ident, *args], cwd=cwd or self.project, capture_output=True, text=True,
                           env=env, input=input)
        if check and r.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()}")
        return r

    # ---------- 기준 브랜치 ----------
    def current_branch(self) -> str | None:
        r = self.git("symbolic-ref", "--quiet", "--short", "HEAD")
        return r.stdout.strip() or None if r.returncode == 0 else None

    def has_commit(self) -> bool:
        return self.git("rev-parse", "--verify", "-q", "HEAD").returncode == 0

    def check_ready(self) -> str | None:
        """병렬 작업을 시작할 수 없는 이유 (없으면 None)."""
        if self.git("rev-parse", "--is-inside-work-tree").returncode != 0:
            return "git 저장소가 아닙니다."
        if not self.has_commit():
            return "커밋이 하나도 없습니다. 먼저 커밋을 하나 만드세요."
        if not self.current_branch():
            return "HEAD 가 브랜치가 아닙니다(detached). 작업할 브랜치를 체크아웃하세요."
        if self.prefix:
            version = self.git('--version')
            match = re.search(r'git version (\d+)\.(\d+)', version.stdout)
            if not match or tuple(map(int, match.groups())) < (2, 38):
                return '하위 프로젝트 병렬 작업에는 Git 2.38 이상의 merge-tree --write-tree가 필요합니다.'
            probe_merge = self.git('merge-tree', '--write-tree', '--merge-base=HEAD', 'HEAD', 'HEAD')
            if probe_merge.returncode:
                return 'Git merge-tree 범위 병합 사전 점검 실패: ' + (probe_merge.stderr or probe_merge.stdout).strip()
            # Detached probe leaves no branch and never checks out the whole repository.
            with tempfile.TemporaryDirectory(prefix='duet-sparse-') as directory:
                probe = Path(directory) / 'probe'
                try:
                    self.git('worktree', 'add', '--detach', '--no-checkout', str(probe), 'HEAD', check=True)
                    self._sparse(probe)
                except GitError as e:
                    return f'sparse-checkout 사전 점검 실패: {e}'
                finally:
                    self.git('worktree', 'remove', '--force', str(probe))
                    self.git('worktree', 'prune')
        return None

    def install_pre_push(self) -> str | None:
        hooks = Path(self.git("rev-parse", "--git-path", "hooks").stdout.strip() or ".git/hooks")
        if not hooks.is_absolute():
            hooks = self.project / hooks
        path = hooks / "pre-push"
        try:
            if path.exists():
                if PRE_PUSH_MARK in path.read_text(errors="replace"):
                    return None
                return ("기존 pre-push 훅이 있어 duet 보호 훅을 넣지 않았습니다. duet/work/* 브랜치는 병합 후 자동 삭제되지만, "
                        "push 전에 git branch 로 확인하세요.")
            hooks.mkdir(parents=True, exist_ok=True)
            path.write_text(PRE_PUSH)
            path.chmod(0o755)
        except OSError as e:
            return f"pre-push 훅을 넣지 못했습니다: {e}"
        return None

    # ---------- 워크트리 ----------
    def path_for(self, wid: str) -> Path:
        return self.root_for(wid) / self.prefix

    def root_for(self, wid: str) -> Path:
        return self.project / WORKTREE_DIR / wid

    def branch_for(self, wid: str) -> str:
        return BRANCH_PREFIX + wid

    def create(self, wid: str, base: str) -> Path:
        root, path, branch = self.root_for(wid), self.path_for(wid), self.branch_for(wid)
        if root.exists():
            if self.prefix:
                self._sparse(root, checkout=False)
            self._protect_deps(path)
            return path  # 재시작 후 이어서
        root.parent.mkdir(parents=True, exist_ok=True)
        existed = self.git("rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0
        options = ['--no-checkout'] if self.prefix else []
        try:
            if existed:
                self.git("worktree", "add", *options, str(root), branch, check=True)
            else:
                self.git("worktree", "add", *options, "-b", branch, str(root), base, check=True)
            if self.prefix:
                self._sparse(root)
        except GitError:
            self.git('worktree', 'remove', '--force', str(root))
            self.git('worktree', 'prune')
            if not existed:
                self.git('branch', '-D', branch)
            raise
        self._link_deps(path)
        return path

    def _sparse(self, root: Path, *, checkout: bool = True) -> None:
        self.git('sparse-checkout', 'set', '--cone', '--', self.prefix.rstrip('/'), cwd=root, check=True)
        if checkout:
            self.git('checkout', cwd=root, check=True)
        (root / self.prefix).mkdir(parents=True, exist_ok=True)

    def _link_deps(self, path: Path) -> None:
        for name in SHARED_DEP_DIRS:
            src = self.project / name
            dst = path / name
            if src.is_dir() and not dst.exists() and self.git("check-ignore", "-q", name).returncode == 0:
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    dst.symlink_to(src, target_is_directory=True)
                except OSError:
                    pass
        self._protect_deps(path)

    def _protect_deps(self, path: Path) -> None:
        names = [name for name in SHARED_DEP_DIRS
                 if (path / name).is_symlink() and (self.project / name).is_dir()
                 and (path / name).resolve() == (self.project / name).resolve()]
        if not names:
            return
        exclude = Path(self.git("rev-parse", "--git-path", "info/exclude", cwd=path, check=True).stdout.strip())
        if not exclude.is_absolute():
            exclude = path / exclude
        previous = exclude.read_text() if exclude.exists() else ""
        additions = ['/' + self.prefix + name for name in names
                     if '/' + self.prefix + name not in previous.splitlines()]
        if additions:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with exclude.open("a") as stream:
                stream.write(("\n" if previous and not previous.endswith("\n") else "")
                             + "\n".join(additions) + "\n")
        for name in names:
            self.git("rm", "--cached", "-f", "--ignore-unmatch", "--", name, cwd=path, check=True)

    def remove(self, wid: str) -> None:
        path, branch = self.root_for(wid), self.branch_for(wid)
        if path.exists():
            self.git("worktree", "remove", "--force", str(path))
        self.git("worktree", "prune")
        self.git("branch", "-D", branch)

    def local_work_branches(self) -> list[str]:
        r = self.git("for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCH_PREFIX}")
        return [b for b in r.stdout.split() if b]

    # ---------- 작업 트리 상태 ----------
    def commit_all(self, path: Path, message: str) -> bool:
        self._protect_deps(path)
        self.git("add", "-A", '--', '.', cwd=path, check=True)
        if self.git("diff", "--cached", "--quiet", '--', '.', cwd=path).returncode == 0:
            return False
        if self.prefix:
            return self._commit_scoped(path, message) is not None
        self.git("commit", "-q", "--no-verify", "-m", message, cwd=path, check=True)
        return True

    def diff_stat(self, path: Path, base: str) -> str:
        base = self.git("merge-base", base, "HEAD", cwd=path, check=True).stdout.strip()
        self._protect_deps(path)
        self.git("add", "-A", "-N", '--', '.', cwd=path)  # 새 파일도 diff 에 보이게 (intent-to-add)
        r = self.git("diff", '--relative', "--stat", base, '--', '.', cwd=path)
        return r.stdout.strip() or "(변경 없음)"

    def changed_files(self, path: Path, base: str) -> list[str]:
        base = self.git("merge-base", base, "HEAD", cwd=path, check=True).stdout.strip()
        self._protect_deps(path)
        self.git("add", "-A", "-N", '--', '.', cwd=path)
        r = self.git("diff", '--relative', "--name-only", '-z', base, '--', '.', cwd=path)
        return [ln for ln in r.stdout.split('\0') if ln]

    def merge_base_into(self, path: Path, base: str) -> list[str]:
        """기준 브랜치의 최신 내용을 작업 브랜치에 합친다. 충돌 파일 목록을 돌려준다 (없으면 [])."""
        if self.prefix:
            if self.git('merge-base', '--is-ancestor', base, 'HEAD', cwd=path).returncode == 0:
                return []
            target = self.git('rev-parse', base, cwd=path, check=True).stdout.strip()
            conflicts = self._merge_scoped(path, target, keep_conflicts=True)
            if not conflicts:
                self._commit_scoped(path, f'duet: merge {base}', extra_parent=target)
            return conflicts
        r = self.git("merge", "--no-edit", "--no-verify", base, cwd=path)
        if r.returncode == 0:
            return []
        conflicts = self.git("diff", "--name-only", "--diff-filter=U", cwd=path).stdout.split()
        if not conflicts:
            raise GitError((r.stderr or r.stdout).strip())
        return conflicts

    def unresolved(self, path: Path) -> list[str]:
        return [p for p in self.git("diff", '--relative', "--name-only", '-z', "--diff-filter=U",
                                   '--', '.', cwd=path).stdout.split('\0') if p]

    def in_merge(self, path: Path) -> bool:
        if self.prefix:
            return self._merge_marker(path).exists()
        gd = self.git("rev-parse", "--git-dir", cwd=path).stdout.strip()
        p = Path(gd) if Path(gd).is_absolute() else path / gd
        return (p / "MERGE_HEAD").exists()

    def finish_merge(self, path: Path) -> None:
        metadata = None
        if self.prefix:
            metadata = json.loads(self._merge_marker(path).read_text())
            # An unchanged synthesized conflict file is not a resolution. Users
            # may explicitly git add it to choose that side; edits remain auto-staged.
            for name in self.unresolved(path):
                if name not in metadata['conflicts'] or self._file_signature(path / name) == metadata['conflicts'][name]:
                    raise GitError('미해결 인덱스 충돌: ' + name + ' — 내용을 해결하거나 명시적으로 stage하세요')
        self._protect_deps(path)
        self.git("add", "-A", '--', '.', cwd=path)
        left = self.git("diff", "--cached", "--check", '--', '.', cwd=path)
        if left.returncode != 0 and "conflict" in left.stdout.lower():
            raise GitError("충돌 표시(<<<<<<<)가 남아 있습니다:\n" + left.stdout[:1000])
        if self.prefix:
            marker = self._merge_marker(path)
            if self.unresolved(path):
                raise GitError('프로젝트 인덱스에 미해결 충돌이 남아 있습니다')
            self._commit_scoped(path, 'duet: resolve project merge', extra_parent=metadata['target'])
            marker.unlink()
            return
        self.git("commit", "-q", "--no-verify", "--no-edit", cwd=path, check=True)

    def squash_into_base(self, wid: str, base: str, message: str) -> str | None:
        """메인 작업 트리(기준 브랜치)에 squash 병합 후 커밋. 커밋 해시(변경 없으면 None)."""
        if self.current_branch() != base:
            raise GitError(f"메인 작업 트리가 기준 브랜치 {base} 가 아닙니다 (현재 {self.current_branch()}).")
        if self.prefix:
            before = self.git('rev-parse', 'HEAD', check=True).stdout.strip()
            # Refusal is not rollback: never restore over pre-existing dirty work.
            if self.git('status', '--porcelain', '--untracked-files=no', '--', '.', check=True).stdout:
                raise GitError('병합 전에 프로젝트 내부 변경을 스냅샷으로 저장해야 합니다')
            try:
                conflicts = self._merge_scoped(self.project, self.branch_for(wid))
                if conflicts:
                    raise GitError('squash 병합 충돌: ' + ', '.join(conflicts))
                return self._commit_scoped(self.project, message)
            except (GitError, OSError):
                self.git('restore', '--source=' + before, '--staged', '--worktree', '--', '.', check=True)
                raise
        r = self.git("merge", "--squash", self.branch_for(wid))
        if r.returncode != 0:
            self.git("reset", "--merge")
            raise GitError("squash 병합 실패: " + (r.stderr or r.stdout).strip())
        if self.git("diff", "--cached", "--quiet").returncode == 0:
            return None
        c = self.git("commit", "-q", "--no-verify", "-m", message)
        if c.returncode != 0:
            self.git("reset", "--merge")
            raise GitError("병합 커밋 실패: " + (c.stderr or c.stdout).strip())
        return self.git("rev-parse", "HEAD").stdout.strip()

    def _commit_scoped(self, path: Path, message: str, **kwargs) -> str | None:
        scope = Git(path)
        scope._ident = self._ident
        try:
            return scope.commit_scoped(message, **kwargs)
        except (subprocess.CalledProcessError, RuntimeError) as e:
            raise GitError(f'프로젝트 범위 커밋 실패: {e}') from e

    def _merge_marker(self, path: Path) -> Path:
        name = self.git('rev-parse', '--git-path', 'duet-project-merge', cwd=path, check=True).stdout.strip()
        return Path(name) if Path(name).is_absolute() else path / name

    def _project_tree(self, path: Path, source: str, onto: str) -> str:
        with temporary_index() as env:
            self.git('read-tree', onto, cwd=path, env=env, check=True)
            self.git('restore', '--source=' + source, '--staged', '--', '.', cwd=path, env=env, check=True)
            return self.git('write-tree', cwd=path, env=env, check=True).stdout.strip()

    @staticmethod
    def _file_signature(path: Path):
        if path.is_symlink():
            return 'link:' + os.readlink(path)
        if path.is_file():
            return 'file:' + hashlib.sha256(path.read_bytes()).hexdigest()
        return None

    def _merge_scoped(self, path: Path, incoming: str, *, keep_conflicts: bool = False) -> list[str]:
        # All three trees have identical outside entries. Only the project may
        # be checked out or content-merged; the real outside index is untouched.
        if self.git('status', '--porcelain', '--untracked-files=no', '--', '.', cwd=path, check=True).stdout:
            raise GitError('병합 전에 프로젝트 내부 변경을 스냅샷으로 저장해야 합니다')
        head = self.git('rev-parse', 'HEAD', cwd=path, check=True).stdout.strip()
        base = self.git('merge-base', head, incoming, cwd=path, check=True).stdout.strip()
        ancestor = self._project_tree(path, base, head)
        theirs = self._project_tree(path, incoming, head)
        root = Path(self.git('rev-parse', '--show-toplevel', cwd=path, check=True).stdout.strip())
        # Native ort merge preserves add/add, modify/delete and rename semantics.
        merged = self.git('-c', 'merge.renames=true', 'merge-tree', '--write-tree', '--no-messages', '-z',
                          '--merge-base=' + ancestor, head, theirs, cwd=root)
        if merged.returncode not in (0, 1):
            raise GitError('프로젝트 merge-tree 실패: ' + (merged.stderr or merged.stdout).strip())
        records = merged.stdout.split('\0')
        tree, stages = records[0], [row for row in records[1:] if row]
        if not re.fullmatch(r'[a-f0-9]{40,64}', tree):
            raise GitError('merge-tree가 유효한 결과 트리를 반환하지 않았습니다')
        prefix = self.prefix
        changed = self.git('diff-tree', '--no-relative', '--no-commit-id', '--name-only', '-r', '-z',
                           head, tree, cwd=root, check=True).stdout.split('\0')
        if any(name and not name.startswith(prefix) for name in changed):
            raise GitError('병합 결과가 프로젝트 밖 경로를 변경합니다')
        conflict_names = set()
        for row in stages:
            info, sep, name = row.partition('\t')
            if not sep or not name.startswith(prefix) or info.split()[-1] not in ('1', '2', '3'):
                raise GitError('merge-tree 충돌 인덱스가 프로젝트 범위를 벗어났습니다')
            conflict_names.add(name)
        if merged.returncode and not conflict_names:
            raise GitError('merge-tree 충돌이 발생했지만 해결 가능한 인덱스 항목이 없습니다')
        with temporary_index() as env:
            self.git('read-tree', head, cwd=path, env=env, check=True)
            # read-tree creates entries without stat information. Refresh before
            # the checked -u transition; outside dirty/missing entries are irrelevant.
            self.git('update-index', '--refresh', '--ignore-missing', cwd=path, env=env)
            self.git('-c', 'core.sparseCheckout=false', 'read-tree', '-m', '-u', head, tree,
                     cwd=path, env=env, check=True)
            entries = self.git('ls-files', '--stage', '--full-name', '-z', '--', '.', cwd=path, env=env, check=True).stdout
            existing = self.git('ls-files', '--full-name', '-z', '--', '.', cwd=path, check=True).stdout
            zero = '0' * len(head)
            remove = ''.join(f'0 {zero}\t{name}\0' for name in existing.split('\0') if name)
            unmerge = ''.join(f'0 {zero}\t{name}\0' for name in sorted(conflict_names))
            unmerge += ''.join(row + '\0' for row in stages)
            self.git('update-index', '-z', '--index-info', cwd=root, input=remove + entries + unmerge, check=True)
        conflicts = self.unresolved(path)
        if conflicts and keep_conflicts:
            target = self.git('rev-parse', incoming, cwd=path, check=True).stdout.strip()
            self._merge_marker(path).write_text(json.dumps(dict(target=target,
                conflicts={name: self._file_signature(path / name) for name in conflicts}), ensure_ascii=False))
        return conflicts
