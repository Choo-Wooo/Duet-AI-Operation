"""DIALOGUE.md — 에이전트 간 공유 대화 문서."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

HEADER_RE = re.compile(r"^## \[(?P<role>[^\]\n]+)\] #(?P<n>\d+)(?P<rest>[^\n]*)$", re.M)
DIRECTIVE_RE = re.compile(r"<!--\s*duet:\s*(?P<name>[A-Z_]+)\b(?P<arg>.*?)-->", re.S)


def _control_matches(text: str):
    visible = control_text(text)
    # Skip comments as a unit: inline ```files in a TASK is argument text.
    spans = []
    tokens = re.compile(r'<!--.*?-->|`+', re.S)
    end = 0
    for token in tokens.finditer(visible):
        if token.start() < end or token.group().startswith('<!--'):
            continue
        rest = visible[token.end():]
        paragraph_end = re.search(r'\r?\n[ \t\r]*\n', rest)
        if paragraph_end:
            rest = rest[:paragraph_end.start()]
        close = re.search(r'(?<!`)' + re.escape(token.group()) + r'(?!`)', rest)
        if close:
            end = token.end() + close.end()
            spans.append((token.start(), end))
    for match in DIRECTIVE_RE.finditer(visible):
        prefix = visible[visible.rfind('\n', 0, match.start()) + 1:match.start()]
        if prefix.strip() or any(start <= match.start() < end for start, end in spans):
            continue
        yield match


def control_text(text: str, *, recover_turns: bool = False) -> str:
    """코드 fence/인용을 같은 길이의 공백으로 가려 예제가 실행되지 않게 한다."""
    lines = []
    fence = None
    last_number = None
    for line in text.splitlines(keepends=True):
        if recover_turns:
            header = HEADER_RE.match(line)
            if header and (fence is None or (last_number is not None
                                             and int(header['n']) == last_number + 1)):
                # A sequential turn header is a recovery boundary even when
                # the previous response was truncated inside a code fence.
                last_number = int(header['n'])
                fence = None
                lines.append(line)
                continue
        stripped = line.lstrip()
        marker = re.match(r"(`{3,}|~{3,})(.*)", stripped)
        hidden = fence is not None or stripped.startswith(">")
        if not stripped.startswith(">") and marker:
            run, rest = marker.groups()
            if fence is None:
                fence = run
                hidden = True
            elif run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
                fence = None
        lines.append("".join("\n" if c == "\n" else " " for c in line) if hidden else line)
    return "".join(lines)


def extract_directives(text: str) -> list[tuple[str, str]]:
    return [(m.group("name"), text[m.start('arg'):m.end('arg')].strip()) for m in _control_matches(text)]


def without_directives(text: str) -> str:
    for m in reversed(list(_control_matches(text))):
        text = text[:m.start()] + text[m.end():]
    return text.strip()


@dataclass
class Turn:
    role: str
    n: int
    rest: str
    start: int
    end: int
    body: str

    @property
    def directives(self) -> list[tuple[str, str]]:
        return extract_directives(self.body)

    def directive(self, name: str) -> str | None:
        for k, v in self.directives:
            if k == name:
                return v
        return None

    def summary(self, max_lines: int = 3, max_chars: int = 240) -> str:
        text = DIRECTIVE_RE.sub("", self.body).strip()
        lines = [ln for ln in text.splitlines() if ln.strip()][:max_lines]
        s = "\n".join(lines)
        return s if len(s) <= max_chars else s[: max_chars - 1] + "…"


class Dialogue:
    def __init__(self, project: Path, filename: str = "DIALOGUE.md"):
        self.project = project
        self.path = project / filename
        self.archive_dir = project / "DIALOGUE-archive"

    def ensure(self) -> bool:
        if self.path.exists():
            return False
        self.path.write_text(
            f"# DIALOGUE — {self.project.name}\n\n"
            "> duet 공유 대화 문서입니다. 각 에이전트는 이 문서 끝에 `## [역할] #번호` 헤더로 자기 턴을 추가합니다.\n"
            "> 흐름 제어 지시문은 `<!-- duet: ... -->` HTML 주석으로 씁니다.\n\n",
            encoding="utf-8",
        )
        return True

    def read(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def turns(self) -> list[Turn]:
        text = self.read()
        ms = list(HEADER_RE.finditer(control_text(text, recover_turns=True)))
        out = []
        for i, m in enumerate(ms):
            end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
            out.append(Turn(m.group("role").strip(), int(m.group("n")), m.group("rest"),
                            m.start(), end, text[m.end():end].strip("\n")))
        return out

    def max_number(self) -> int:
        ts = self.turns()
        return max((t.n for t in ts), default=0)

    def find(self, role: str, min_n: int) -> Turn | None:
        """role 이 쓴 턴 중 번호가 min_n 이상인 마지막 턴."""
        found = None
        for t in self.turns():
            if t.role == role and t.n >= min_n:
                found = t
        return found

    def get(self, n: int) -> Turn | None:
        for t in self.turns():
            if t.n == n:
                return t
        return None

    def _append(self, block: str) -> None:
        text = self.read()
        if text and not text.endswith("\n"):
            block = "\n" + block
        if text and not text.endswith("\n\n"):
            block = "\n" + block
        with self.path.open("a", encoding="utf-8") as f:
            f.write(block)

    def append_turn(self, role: str, n: int, body: str, note: str = "") -> None:
        stamp = time.strftime("%Y-%m-%d %H:%M")
        rest = f" · {stamp}" + (f" · {note}" if note else "")
        self._append(f"## [{role}] #{n}{rest}\n{body.rstrip()}\n")

    def append_note(self, line: str) -> None:
        """턴과 턴 사이의 한 줄 기록(승인 판정 등)."""
        self._append(f"> {line}\n")

    def archive_before(self, n: int) -> Path | None:
        """#n 이전의 턴을 DIALOGUE-archive/ 로 옮긴다. 제목 부분은 남긴다."""
        text = self.read()
        ts = self.turns()
        if not ts:
            return None
        keep_from = next((t.start for t in ts if t.n >= n), None)
        if keep_from is None or keep_from <= ts[0].start:
            return None
        head = text[: ts[0].start]
        old = text[ts[0].start: keep_from]
        self.archive_dir.mkdir(exist_ok=True)
        dest = self.archive_dir / f"DIALOGUE-{time.strftime('%Y%m%d-%H%M%S')}.md"
        dest.write_text(head + old, encoding="utf-8")
        rel = dest.relative_to(self.project)
        self.path.write_text(
            head + f"> 이전 대화(#{ts[0].n}~#{n - 1})는 `{rel}` 에 보관되어 있습니다.\n\n" + text[keep_from:],
            encoding="utf-8",
        )
        return dest
