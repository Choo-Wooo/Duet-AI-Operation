"""긴 텍스트·목록을 에이전트 입력에 넣기 좋게 줄이는 도구와 작업 기억 블록 처리."""
from __future__ import annotations

import re
import time
from pathlib import Path
from .storage import atomic_write

MEMORY_RE = re.compile(r"```duet-memory[ \t]*\n(?P<body>.*?)\n```[ \t]*", re.S)
MEMORY_MAX_CHARS = 12000


def summarize_paths(paths: list[str], limit: int = 20) -> str:
    """긴 파일 목록을 폴더별 개수 + 앞부분 예시로 줄인다."""
    if not paths:
        return "없음"
    if len(paths) <= limit:
        return ", ".join(paths)
    groups: dict[str, int] = {}
    for p in paths:
        parts = p.split("/")
        key = "/".join(parts[:2]) + "/" if len(parts) > 2 else (parts[0] + "/" if len(parts) > 1 else "(루트)")
        groups[key] = groups.get(key, 0) + 1
    top = sorted(groups.items(), key=lambda kv: -kv[1])[:10]
    return (f"{len(paths)}개 파일 — 폴더별: " + ", ".join(f"{k} {v}개" for k, v in top)
            + f" … 예: {', '.join(paths[:limit // 2])}")


def extract_memory(text: str | None) -> tuple[str | None, str | None]:
    """응답에서 마지막 ```duet-memory 블록을 꺼내고, 블록을 뺀 본문을 돌려준다."""
    if not text:
        return text, None
    found = []
    stack = []
    outer = None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^[ \t]*(`{3,}|~{3,})([^\r\n]*)', line)
        if marker:
            run, label = marker.groups()
            label = label.strip()
            if not stack:
                stack.append(run)
                outer = (offset, offset + len(line)) if label == 'duet-memory' else None
            elif run[0] == stack[-1][0] and len(run) >= len(stack[-1]) and not label:
                stack.pop()
                if not stack and outer is not None:
                    found.append((outer[0], offset + len(line), text[outer[1]:offset].strip()))
                    outer = None
            elif outer is not None and (label or run[0] != stack[-1][0]):
                stack.append(run)
        offset += len(line)
    if not found:
        return text, None
    body = found[-1][2]
    clean = text
    for start, end, _ in reversed(found):
        clean = clean[:start] + clean[end:]
    clean = clean.rstrip()
    return clean, body or None


def memory_path(project: Path, role: str) -> Path:
    return project / ".duet" / "memory" / f"{role}.md"


def save_memory(project: Path, role: str, body: str) -> Path:
    path = memory_path(project, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    if len(body) > MEMORY_MAX_CHARS:
        body = body[:MEMORY_MAX_CHARS] + "\n… (길이 제한으로 잘림 — 다음 갱신 때 더 짧게 정리할 것)"
    stamp = time.strftime("%Y-%m-%d %H:%M")
    atomic_write(path, f"<!-- {role} 작업 기억 · 갱신 {stamp} · duet 이 매 턴 응답에서 자동 저장 -->\n{body}\n")
    return path
