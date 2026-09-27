"""설치된 CLI 찾기: 여러 곳에 설치돼 있으면 가장 최신 버전을 고른다.

`claude update` 는 보통 ~/.local/bin/claude (네이티브 설치)를 갱신하는데, PATH 에서 먼저 잡히는 건
Homebrew/npm 으로 깔린 예전 claude 일 수 있다. 그래서 PATH 순서가 아니라 버전으로 고른다.
"""
from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
from pathlib import Path

from .procs import WINDOWS, hidden_kwargs

HOME = Path.home()
EXTRA_DIRS = [
    HOME / ".local" / "bin",
    HOME / ".claude" / "local",
    HOME / ".npm-global" / "bin",
    HOME / ".bun" / "bin",
    HOME / ".volta" / "bin",
    Path("/opt/homebrew/bin"),
    Path("/usr/local/bin"),
]
if WINDOWS:
    _local = Path(os.environ.get("LOCALAPPDATA") or HOME / "AppData" / "Local")
    _roaming = Path(os.environ.get("APPDATA") or HOME / "AppData" / "Roaming")
    EXTRA_DIRS = [
        HOME / ".local" / "bin",              # claude 네이티브 설치 (claude.exe)
        HOME / ".claude" / "local",
        _roaming / "npm",                     # npm i -g (claude.cmd, codex.cmd)
        HOME / ".bun" / "bin",
        HOME / ".volta" / "bin",
        _local / "Volta" / "bin",
        _local / "agy" / "bin",               # Antigravity CLI
        _local / "Programs" / "OpenAI" / "Codex" / "bin",
        Path(os.environ.get("ProgramFiles") or "C:/Program Files") / "nodejs",
    ]


def _exe_names(name: str) -> list[str]:
    """Windows 는 확장자(.exe/.cmd …)가 붙은 실행 파일을 찾는다. .exe 를 가장 먼저."""
    if not WINDOWS:
        return [name]
    exts = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    order = [".exe"] + [e for e in (".cmd", ".bat", ".com") if e in exts or e == ".cmd"]
    return [name + e for e in order]


def _version_of(path: str) -> tuple[tuple[int, ...], str]:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, timeout=20, stdin=subprocess.DEVNULL,
                             **hidden_kwargs())
        text = (out.stdout or out.stderr).decode("utf-8", "replace").strip()
    except Exception:
        return (), ""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return (tuple(int(x) for x in m.groups()) if m else ()), text.splitlines()[0] if text else ""


def candidates(name: str) -> list[str]:
    found: list[str] = []
    for d in os.environ.get("PATH", "").split(os.pathsep) + [str(p) for p in EXTRA_DIRS]:
        if not d:
            continue
        for exe in _exe_names(name):
            p = Path(d) / exe
            if p.is_file() and os.access(p, os.X_OK):
                rp = str(p)
                if rp not in found:
                    found.append(rp)
    # nvm 설치본
    nvm = HOME / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        for p in sorted(nvm.glob(f"*/bin/{name}")):
            if str(p) not in found:
                found.append(str(p))
    nvm_win = os.environ.get("NVM_HOME")  # nvm-windows
    if WINDOWS and nvm_win and Path(nvm_win).is_dir():
        for exe in _exe_names(name):
            for p in sorted(Path(nvm_win).glob(f"*/{exe}")):
                if str(p) not in found:
                    found.append(str(p))
    if name == "claude":
        try:  # claude-agent-sdk 에 들어 있는 CLI 도 후보 (최후 수단)
            import claude_agent_sdk
            for exe in _exe_names("claude"):
                b = Path(claude_agent_sdk.__file__).parent / "_bundled" / exe
                if b.is_file():
                    found.append(str(b))
                    break
        except Exception:
            pass
    return found


@functools.lru_cache(maxsize=None)
def resolve(name: str, override: str | None = None) -> tuple[str | None, str, list[tuple[str, str]]]:
    """(선택된 경로, 버전 문자열, [(후보 경로, 버전)]) 를 돌려준다."""
    if override:
        return override, _version_of(override)[1], [(override, _version_of(override)[1])]
    best, best_v, best_text = None, (), ""
    seen: list[tuple[str, str]] = []
    real_seen: set[str] = set()
    for c in candidates(name):
        try:
            real = os.path.realpath(c)
        except OSError:
            real = c
        if real in real_seen:
            continue
        real_seen.add(real)
        v, text = _version_of(c)
        seen.append((c, text or "?"))
        if v and v > best_v:
            best, best_v, best_text = c, v, text
    if best is None and seen:
        best, best_text = seen[0]
    return best, best_text, seen


def which(name: str) -> str | None:
    return resolve(name, os.environ.get(f"DUET_{name.upper()}_PATH") or None)[0] or shutil.which(name)
