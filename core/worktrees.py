"""병렬 작업용 git 워크트리·로컬 작업 브랜치 관리.

- 작업 브랜치(duet/work/<id>)는 duet 시작 시 체크아웃돼 있던 기준 브랜치에서 로컬로만 만든다.
- 끝나면 기준 브랜치에 squash 병합하고 워크트리와 작업 브랜치를 지운다. 원격에는 절대 올리지 않는다
  (pre-push 훅으로 duet/work/* push 를 막는다). push 는 사람이 기준 브랜치만 한다.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

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

    def git(self, *args: str, cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_EDITOR": "true"}
        r = subprocess.run(["git", *self._ident, *args], cwd=cwd or self.project, capture_output=True, text=True,
                           env=env)
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
        return self.project / WORKTREE_DIR / wid

    def branch_for(self, wid: str) -> str:
        return BRANCH_PREFIX + wid

    def create(self, wid: str, base: str) -> Path:
        path, branch = self.path_for(wid), self.branch_for(wid)
        if path.exists():
            return path  # 재시작 후 이어서
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.git("rev-parse", "--verify", "-q", f"refs/heads/{branch}").returncode == 0:
            self.git("worktree", "add", str(path), branch, check=True)
        else:
            self.git("worktree", "add", "-b", branch, str(path), base, check=True)
        self._link_deps(path)
        return path

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

    def remove(self, wid: str) -> None:
        path, branch = self.path_for(wid), self.branch_for(wid)
        if path.exists():
            self.git("worktree", "remove", "--force", str(path))
        self.git("worktree", "prune")
        self.git("branch", "-D", branch)

    def local_work_branches(self) -> list[str]:
        r = self.git("for-each-ref", "--format=%(refname:short)", f"refs/heads/{BRANCH_PREFIX}")
        return [b for b in r.stdout.split() if b]

    # ---------- 작업 트리 상태 ----------
    def commit_all(self, path: Path, message: str) -> bool:
        self.git("add", "-A", cwd=path)
        if self.git("diff", "--cached", "--quiet", cwd=path).returncode == 0:
            return False
        self.git("commit", "-q", "--no-verify", "-m", message, cwd=path, check=True)
        return True

    def diff_stat(self, path: Path, base: str) -> str:
        self.git("add", "-A", "-N", cwd=path)  # 새 파일도 diff 에 보이게 (intent-to-add)
        r = self.git("diff", "--stat", base, cwd=path)
        return r.stdout.strip() or "(변경 없음)"

    def changed_files(self, path: Path, base: str) -> list[str]:
        self.git("add", "-A", "-N", cwd=path)
        r = self.git("diff", "--name-only", base, cwd=path)
        return [ln for ln in r.stdout.splitlines() if ln]

    def merge_base_into(self, path: Path, base: str) -> list[str]:
        """기준 브랜치의 최신 내용을 작업 브랜치에 합친다. 충돌 파일 목록을 돌려준다 (없으면 [])."""
        r = self.git("merge", "--no-edit", "--no-verify", base, cwd=path)
        if r.returncode == 0:
            return []
        conflicts = self.git("diff", "--name-only", "--diff-filter=U", cwd=path).stdout.split()
        if not conflicts:
            raise GitError((r.stderr or r.stdout).strip())
        return conflicts

    def unresolved(self, path: Path) -> list[str]:
        return self.git("diff", "--name-only", "--diff-filter=U", cwd=path).stdout.split()

    def in_merge(self, path: Path) -> bool:
        gd = self.git("rev-parse", "--git-dir", cwd=path).stdout.strip()
        p = Path(gd) if Path(gd).is_absolute() else path / gd
        return (p / "MERGE_HEAD").exists()

    def finish_merge(self, path: Path) -> None:
        self.git("add", "-A", cwd=path)
        left = self.git("diff", "--cached", "--check", cwd=path)
        if left.returncode != 0 and "conflict" in left.stdout.lower():
            raise GitError("충돌 표시(<<<<<<<)가 남아 있습니다:\n" + left.stdout[:1000])
        self.git("commit", "-q", "--no-verify", "--no-edit", cwd=path, check=True)

    def squash_into_base(self, wid: str, base: str, message: str) -> str | None:
        """메인 작업 트리(기준 브랜치)에 squash 병합 후 커밋. 커밋 해시(변경 없으면 None)."""
        if self.current_branch() != base:
            raise GitError(f"메인 작업 트리가 기준 브랜치 {base} 가 아닙니다 (현재 {self.current_branch()}).")
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
