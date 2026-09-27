"""합의 task 데이터, 계획 문서, git과 독립적인 파일 지문."""
from __future__ import annotations

import hashlib
import os
import stat
import re
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .dialogue import control_text, without_directives
from .fsutil import is_junction

PHASES = ("plan", "plan_review", "implement", "verify")
VERSION = re.compile(r"^## v(\d+)(?: \(deviation\))?\s*$", re.M)
EXCLUDED_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", "DIALOGUE-archive", ".idea", ".vscode"}
EXCLUDED_FILES = {".DS_Store"}
# (경로, 크기, 수정시각, inode) → sha256. 바뀌지 않은 큰 파일(월드·맵 등)을 매번 다시 읽지 않도록.
_HASH_CACHE: dict[tuple, str] = {}
DUET_RUNTIME = {"venv", "logs", "saves", "memory", "reports", "asks", "worktrees"}


def new_task(role: str, instruction: str, base_commit: str | None, baseline: dict) -> dict:
    task_id = "task-" + uuid4().hex[:12]
    return dict(id=task_id, role=role, instruction=instruction, agreement=True, phase="plan",
                plan_path=f"docs/plans/{task_id}.md", submitted_version=0, agreed_version=None,
                rounds=0, rounds_extra=0, negotiations=0, negotiation_limit=6, negotiation_ask=False,
                base_commit=base_commit, test_command="",
                agreed_text_sha256=None, agreed_files=[], delegate_fingerprint=baseline,
                agree_fingerprint={}, waiting=False, wait_reason="", review_n=0,
                human_approved_version=None, plan_changes=[])


def validate_task(task: dict | None) -> None:
    if task is None:
        return
    if not isinstance(task, dict):
        raise ValueError("task는 객체여야 합니다")
    for name, default in [('negotiations', 0), ('negotiation_limit', 6)]:
        task.setdefault(name, default)
        if type(task[name]) is not int or task[name] < 0:
            raise ValueError(f'잘못된 task.{name}')
    task.setdefault('negotiation_ask', False)
    if not {"agreed_version", "human_approved_version", "agreed_text_sha256", "base_commit"} <= task.keys():
        raise ValueError("task의 합의/기준 필드가 누락되었습니다")
    for name in ("id", "role", "instruction", "phase", "plan_path", "test_command", "wait_reason"):
        if not isinstance(task.get(name), str):
            raise ValueError(f"잘못된 task.{name}")
    if task["phase"] not in PHASES or task.get("agreement") is not True:
        raise ValueError("잘못된 task phase/agreement")
    if not re.fullmatch(r"task-[a-f0-9]{12}", task["id"]) or not task["role"]:
        raise ValueError("잘못된 task id/role")
    if task["plan_path"] != f"docs/plans/{task['id']}.md":
        raise ValueError("잘못된 task.plan_path")
    for name in ("submitted_version", "rounds", "rounds_extra", "review_n"):
        if type(task.get(name)) is not int or task[name] < 0:
            raise ValueError(f"잘못된 task.{name}")
    for name in ("agreed_version", "human_approved_version"):
        if task.get(name) is not None and (type(task[name]) is not int or not 1 <= task[name] <= task["submitted_version"]):
            raise ValueError(f"잘못된 task.{name}")
    if type(task.get("waiting")) is not bool:
        raise ValueError("잘못된 task.waiting")
    if task.get("base_commit") is not None and not isinstance(task["base_commit"], str):
        raise ValueError("잘못된 task.base_commit")
    sha = task.get("agreed_text_sha256")
    if sha is not None and (not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha)):
        raise ValueError("잘못된 합의 해시")
    for name in ("agreed_files", "plan_changes"):
        if not isinstance(task.get(name), list) or any(not isinstance(p, str) for p in task[name]):
            raise ValueError(f"잘못된 task.{name}")
    for name in ("delegate_fingerprint", "agree_fingerprint"):
        values = task.get(name)
        if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                             or not re.fullmatch(r"[a-f0-9]{64}", v)
                                             for k, v in values.items()):
            raise ValueError(f"잘못된 task.{name}")
    if task["phase"] in ("implement", "verify") and (
            task["agreed_version"] is None or not sha or not task["test_command"] or not task["agreed_files"]):
        raise ValueError("구현/검증 task에 합의 정보가 없습니다")


def canonical(text: str) -> str:
    return text.strip() + "\n"


def text_hash(text: str) -> str:
    return hashlib.sha256(canonical(text).encode("utf-8")).hexdigest()


def plan_versions(text: str) -> dict[int, str]:
    headers = list(VERSION.finditer(control_text(text)))
    versions = {}
    for i, match in enumerate(headers):
        version = int(match.group(1))
        if version in versions or version < 1:
            raise ValueError("계획 버전이 중복되거나 잘못되었습니다")
        end = headers[i + 1].start() if i + 1 < len(headers) else len(text)
        versions[version] = canonical(text[match.end():end])
    return versions


def read_plan(project: Path, task: dict, version: int) -> str:
    versions = plan_versions((project / task["plan_path"]).read_text(encoding="utf-8"))
    if version not in versions:
        raise ValueError(f"계획 v{version}이 없습니다")
    return versions[version]


def record_plan(project: Path, task: dict, response: str, deviation: bool = False) -> int:
    body = without_directives(response)
    if not body:
        raise ValueError("계획 전문이 비었습니다")
    if VERSION.search(control_text(body)):
        raise ValueError("버전 헤더 없이 계획 전문을 제출하세요. 버전은 오케스트레이터가 부여합니다")
    path = project / task["plan_path"]
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    version = max([task["submitted_version"], *plan_versions(existing)], default=0) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(f"\n## v{version}" + (" (deviation)" if deviation else "") + "\n\n" + canonical(body))
    task["submitted_version"] = version
    return version


def parse_plan(text: str) -> tuple[list[str], str]:
    # canonical fenced `files` block; only one block and one top-level command.
    blocks = []
    fence, label, body = None, "", []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(">"):
            continue
        marker = re.match(r"(`{3,}|~{3,})(.*)$", stripped)
        if fence is None:
            if marker:
                fence, label = marker.group(1), marker.group(2).strip()
                body = []
        elif marker and marker.group(1)[0] == fence[0] and len(marker.group(1)) >= len(fence) and not marker.group(2).strip():
            if label in ("files", "files:"):
                blocks.append("\n".join(body))
            fence = None
        else:
            body.append(line)
    if len(blocks) != 1:
        raise ValueError("files 코드블록이 없거나 여러 개입니다")
    files = [p.strip() for p in blocks[0].splitlines() if p.strip()]
    if not files or any(PurePosixPath(p).is_absolute() or ".." in PurePosixPath(p).parts
                        or p in (".", "") or any(c in p for c in "*?[]\\") for p in files):
        raise ValueError("files에는 프로젝트 상대 파일 경로를 적으세요")
    commands = re.findall(r"^test_command:[ \t]*(.*)$", control_text(text), re.M)
    if len(commands) != 1 or not commands[0].strip():
        raise ValueError("test_command 한 줄이 없거나 여러 개입니다")
    return list(dict.fromkeys(files)), commands[0].strip()


def fingerprint(project: Path, exclude: list[str] | None = None) -> tuple[dict[str, str], list[str]]:
    extra = set(exclude or ())
    excluded_dirs, excluded_files = EXCLUDED_DIRS | extra, EXCLUDED_FILES | extra
    values, errors = {}, []
    def onerror(err):
        errors.append(str(err))
    for root, dirs, files in os.walk(project, followlinks=False, onerror=onerror):
        directory = Path(root)
        rel_dir = directory.relative_to(project).as_posix()
        dirs[:] = sorted(d for d in dirs if d not in excluded_dirs
                         and not (rel_dir == ".duet" and d in DUET_RUNTIME)
                         and not is_junction(directory / d))  # Windows 정션은 따라가지 않는다
        for name in sorted(files):
            if name in excluded_files:
                continue
            path = directory / name
            rel = path.relative_to(project).as_posix()
            if rel == ".duet/state.json":
                continue
            try:
                st = os.lstat(path)
                if stat.S_ISLNK(st.st_mode):
                    raw = os.readlink(path).encode()
                    values[rel] = hashlib.sha256(raw).hexdigest()
                elif not stat.S_ISREG(st.st_mode):
                    # FIFO·소켓·장치 파일은 열면 멈출 수 있으므로(named pipe 는 쓰는 쪽이 없으면 영원히 대기) 건너뛴다
                    continue
                else:
                    key = (str(path), st.st_size, st.st_mtime_ns, st.st_ino)
                    cached = _HASH_CACHE.get(key)
                    if cached is None:
                        with path.open("rb") as f:
                            h = hashlib.sha256()
                            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                                h.update(chunk)
                            cached = h.hexdigest()
                        _HASH_CACHE[key] = cached
                    values[rel] = cached
            except OSError as e:
                errors.append(f"{rel}: {e}")
    return values, errors


def changed_files(before: dict, after: dict) -> list[str]:
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p)
                  and p != "DIALOGUE.md" and not p.startswith("docs/plans/"))


def task_status(task: dict | None) -> str:
    if not task:
        return "진행 중 합의 task 없음"
    agreed = f"v{task['agreed_version']}" if task["agreed_version"] else "미합의"
    return (f"{task['id']} · {task['phase']} · 제출 v{task['submitted_version']} / 합의 {agreed} · "
            f"라운드 {task['rounds']}" + (" · 대기" if task["waiting"] else ""))
