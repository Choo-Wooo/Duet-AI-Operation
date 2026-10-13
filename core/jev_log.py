"""One append-only Markdown conversation for JEV questions and decisions."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock
from uuid import uuid4

from .storage import atomic_write

LOG_NAME = "DIALOGUE.md"
ENTRY_MARKER = "<!-- duet-jev-entry -->"
_HEADER = "# JEV 판단 기록\n\n질문·선택지·선택한 답변과 관제 후속 판단을 시간순으로 이어 기록합니다.\n"
_LOCK = RLock()
_LEGACY_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_LEGACY_NAME = re.compile(r"\d{6}-\d{6}-[0-9a-f]{8}\.md")


@dataclass(frozen=True)
class DecisionLogEntry:
    path: Path
    decision_id: str


def block(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    fence = "`" * max(3, max((len(run) for run in re.findall(r'`+', text)), default=0) + 1)
    return f"{fence}\n{text}\n{fence}\n"


def _check_path(path: Path) -> None:
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise OSError("JEV log path must not be a symlink")


def _folder(project: Path) -> Path:
    folder = Path(project) / '.duet' / 'logs' / 'jev'
    _check_path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _open_regular(path: Path, flags: int) -> int:
    _check_path(path)
    fd = os.open(path, flags | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0), 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("JEV log must be a regular file")
    return fd


@contextmanager
def _locked(folder: Path):
    # A constant lock file also protects migration's atomic replacement of the
    # Markdown inode. Never use the replaced log itself as the lock target.
    with _LOCK:
        fd = _open_regular(folder / '.lock', os.O_RDWR | os.O_CREAT)
        locked = False
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
            yield
        finally:
            if locked:
                if os.name == 'nt':
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _read(path: Path) -> bytes:
    with os.fdopen(_open_regular(path, os.O_RDONLY), 'rb') as stream:
        return stream.read()


def _append(path: Path, text: str) -> None:
    fd = _open_regular(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    with os.fdopen(fd, 'a', encoding='utf-8') as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(_HEADER)
        stream.write('\n\n' + ENTRY_MARKER + '\n' + text.rstrip() + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def _migrate_locked(folder: Path) -> None:
    originals = []
    for day in sorted(folder.iterdir()):
        if not _LEGACY_DAY.fullmatch(day.name):
            continue
        _check_path(day)
        if not day.is_dir():
            continue
        for path in sorted(day.iterdir()):
            if _LEGACY_NAME.fullmatch(path.name):
                originals.append((path, _read(path)))
    if not originals:
        return
    target = folder / LOG_NAME
    try:
        content = _read(target).decode('utf-8') if target.exists() else _HEADER
        additions = []
        for path, raw in originals:
            relative = path.relative_to(folder).as_posix()
            stamp = hashlib.sha256(relative.encode() + b'\0' + raw).hexdigest()
            marker = f'<!-- duet-jev-legacy:{stamp} -->'
            if marker not in content:
                additions.append('\n\n' + ENTRY_MARKER + '\n' + marker +
                                 '\n## 이전 판단 · ' + relative + '\n\n' + raw.decode('utf-8') + '\n')
    except UnicodeError as exc:
        raise OSError('JEV log must contain UTF-8 Markdown') from exc
    _check_path(target)
    if additions:
        atomic_write(target, content + ''.join(additions))
        # Persist the new directory entry before retiring original records.
        if os.name != 'nt':
            fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    for path, raw in originals:
        # Older running Duet versions do not share this lock. Leave any record
        # they changed meanwhile for a later migration, rather than losing it.
        if _read(path) == raw:
            path.unlink()
    for day in {path.parent for path, _ in originals}:
        try:
            day.rmdir()
        except OSError:
            pass  # Keep directories containing unrecognized or updated files.


def migrate_legacy(project: Path) -> Path | None:
    """Consolidate old per-request files, safely retrying interrupted cleanup."""
    folder = Path(project) / '.duet' / 'logs' / 'jev'
    _check_path(folder)
    if not folder.exists():
        return None
    with _locked(folder):
        _migrate_locked(folder)
    path = folder / LOG_NAME
    return path if path.exists() else None


def write_decision(project: Path, record: dict) -> DecisionLogEntry:
    now = datetime.now().astimezone()
    entry = DecisionLogEntry(_folder(project) / LOG_NAME, uuid4().hex)
    lines = [f"## 판단 · {now.isoformat(timespec='seconds')} · {record['purpose']}\n",
             f"- 판단 ID: {entry.decision_id}", f"- 종류: {record['purpose']}", f"- 모드: {record['mode']}",
             f"- 모델: {record['model'] or record['requested_model']}",
             f"- 확신 기준: {record['threshold']}", f"- 질문 버전: {record['rubric_version']}",
             f"- 입력 상태 해시: {record['state_hash']}",
             f"- 입력/출력 토큰: {record['input_tokens']} / {record['output_tokens']}",
             f"- 응답 시간: {record['latency_ms']} ms",
             f"- 응답 상태: {'상태 변경으로 적용 제외' if record['stale'] else record['error'] or '정상'}\n"]
    for name, question in record['questions'].items():
        lines += ["### 질문\n", block(name), block(question.get('instructions', '')),
                  "#### 선택지와 판단 기준\n"]
        for option, criterion in question.get('criteria', {}).items():
            lines += [block({'선택지': option, '판단 기준': criterion})]
        answer = record['answers'].get(name)
        lines += ["#### 선택한 답변\n", block(answer) if answer else "응답 없음\n"]
    with _locked(entry.path.parent):
        _migrate_locked(entry.path.parent)
        _append(entry.path, '\n'.join(lines))
    return entry


def write_control_outcome(project: Path, record: dict) -> DecisionLogEntry:
    """Record a local protocol result even when no model judgment was needed."""
    entry = DecisionLogEntry(_folder(project) / LOG_NAME, uuid4().hex)
    append_outcome(entry, record)
    return entry


def append_outcome(entry: DecisionLogEntry, record: dict) -> None:
    now = datetime.now().astimezone().isoformat(timespec='seconds')
    note = ('아래는 명령 처리 후 확인한 상태와 실행 결과입니다. 작업 등록·재개는 작업 완료를 뜻하지 않습니다.'
            if record.get('종류') == 'control_execution' else
            '아래 기록은 관제의 적용 판단이며 도구 실행·작업 완료를 보장하지 않습니다.')
    text = (f'## 관제 후속 판단 · {now}\n\n- 판단 ID: {entry.decision_id}\n\n'
            + note + '\n\n' + block(record))
    _check_path(entry.path)
    with _locked(entry.path.parent):
        _append(entry.path, text)
