"""턴 단위 git 스냅샷과 정체/핑퐁 감지용 코드 해시."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

DIALOGUE_PREFIXES = ("DIALOGUE.md", "DIALOGUE-archive/")


class Git:
    def __init__(self, project: Path, enabled: bool = True):
        self.project = project
        self.enabled = enabled and shutil.which("git") is not None
        self._ident: list[str] = []

    def _run(self, *args: str, check: bool = False) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *self._ident, *args], cwd=self.project, capture_output=True, text=True,
                              check=check)

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
        return msg

    def ensure_gitignore(self, duet_dirname: str) -> None:
        gi = self.project / ".gitignore"
        lines = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
        want = [f"/{duet_dirname}/", ".duet/venv/", ".duet/logs/", ".duet/state.json", ".duet/saves/",
                ".duet/worktrees/", ".duet/work.json", ".duet/models.json", ".duet/web.json"]
        add = [w for w in want if w not in lines]
        if add:
            block = ("\n" if lines and lines[-1].strip() else "") + "# duet\n" + "\n".join(add) + "\n"
            with gi.open("a", encoding="utf-8") as f:
                f.write(block)

    def snapshot(self, message: str) -> str | None:
        """변경이 있으면 커밋하고 커밋 해시를 돌려준다."""
        if not self.enabled:
            return None
        self._run("add", "-A")
        if self._run("diff", "--cached", "--quiet").returncode == 0:
            return None
        r = self._run("commit", "-q", "--no-verify", "-m", message)
        if r.returncode != 0:
            return None
        return self._run("rev-parse", "HEAD").stdout.strip()

    def code_hash(self) -> str | None:
        """대화 문서/설계 문서를 뺀 작업 트리 내용 해시 (현재 HEAD 기준)."""
        if not self.enabled:
            return None
        r = self._run("ls-tree", "-r", "HEAD")
        if r.returncode != 0:
            return "empty"
        lines = [ln for ln in r.stdout.splitlines()
                 if not ln.split("\t", 1)[-1].startswith(DIALOGUE_PREFIXES)]
        return hashlib.sha1("\n".join(lines).encode()).hexdigest()

    def find_turn_commit(self, n: int) -> str | None:
        r = self._run("log", "--format=%H %s", "-n", "2000")
        for line in r.stdout.splitlines():
            sha, _, subj = line.partition(" ")
            if subj.startswith(f"duet #{n} "):
                return sha
        return None

    def hard_reset(self, sha: str) -> bool:
        return self._run("reset", "--hard", "-q", sha).returncode == 0
