"""하위 프로세스 다루기 (macOS·Linux·Windows 공통).

- POSIX: 새 세션(프로세스 그룹)으로 띄우고 killpg 로 자손까지 멈춘다.
- Windows: 새 프로세스 그룹(CREATE_NEW_PROCESS_GROUP)으로 띄우고, 중단은 CTRL_BREAK,
  강제 종료는 `taskkill /T /F` 로 자손까지 멈춘다. 콘솔 창은 띄우지 않는다.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys

WINDOWS = os.name == "nt"


def group_kwargs() -> dict:
    """자손까지 한꺼번에 멈출 수 있게 새 프로세스 그룹으로 띄우는 인자."""
    if WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    return {"start_new_session": True}


def hidden_kwargs() -> dict:
    """Windows 에서 콘솔 창이 잠깐 뜨지 않게 하는 인자 (POSIX 는 없음)."""
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if WINDOWS else {}


def _taskkill(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=15, **hidden_kwargs())
    except (OSError, subprocess.SubprocessError):
        pass


def kill_tree(pid: int) -> None:
    """프로세스와 자손을 강제 종료한다. 이미 끝났으면 조용히 넘어간다."""
    if WINDOWS:
        _taskkill(pid)
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def terminate_tree(pid: int) -> None:
    """정상 종료를 요청한다 (Windows 는 정상 종료 신호가 없어 강제 종료)."""
    if WINDOWS:
        _taskkill(pid)
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass


def interrupt_tree(pid: int) -> None:
    """Ctrl+C 에 해당하는 중단 요청. Windows 는 CTRL_BREAK (안 되면 강제 종료)."""
    if WINDOWS:
        try:
            os.kill(pid, signal.CTRL_BREAK_EVENT)
        except OSError:
            _taskkill(pid)
        return
    try:
        os.killpg(pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        pass


def interrupted_code(rc: int | None) -> bool:
    """사용자 중단(Ctrl+C/Ctrl+Break/SIGTERM)으로 끝났을 때의 종료 코드인지."""
    if rc is None:
        return False
    if WINDOWS:
        return rc in (130, 0xC000013A, -1073741510)
    return rc in (-signal.SIGINT, -signal.SIGTERM, 130)


def close_transport(proc: asyncio.subprocess.Process) -> None:
    """끝난 프로세스의 파이프를 닫는다.

    Windows(Proactor)에서는 닫지 않은 채 이벤트 루프가 끝나면 GC 때
    'unclosed transport … I/O operation on closed pipe' 경고가 쏟아진다.
    """
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        try:
            transport.close()
        except Exception:
            pass


async def reap(proc: asyncio.subprocess.Process, timeout: float = 5) -> None:
    """자손까지 강제 종료하고 기다린 뒤 파이프를 닫는다."""
    if proc.returncode is None:
        kill_tree(proc.pid)
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass
    close_transport(proc)


def run_text(args: list[str], *, input: str | None = None, **kw) -> subprocess.CompletedProcess:
    """UTF-8 로 입출력하는 subprocess.run (Windows 기본 코드 페이지(cp949 등)를 쓰지 않는다).

    입력은 바이트로 넘겨 Windows 텍스트 모드의 \\n → \\r\\n 변환도 피한다.
    """
    kw.pop("text", None)
    check = kw.pop("check", False)
    kw.setdefault("capture_output", True)
    data = input.encode("utf-8") if input is not None else None
    r = subprocess.run(args, input=data, **hidden_kwargs(), **kw)
    out = r.stdout.decode("utf-8", "replace").replace("\r\n", "\n") if isinstance(r.stdout, bytes) else r.stdout
    err = r.stderr.decode("utf-8", "replace").replace("\r\n", "\n") if isinstance(r.stderr, bytes) else r.stderr
    if check and r.returncode:
        raise subprocess.CalledProcessError(r.returncode, r.args, out, err)
    return subprocess.CompletedProcess(r.args, r.returncode, out, err)


def install_asyncio_policy() -> None:
    """Windows 에서 하위 프로세스를 쓸 수 있는 Proactor 루프를 쓴다 (3.8+ 기본값이지만 명시)."""
    if WINDOWS and sys.version_info >= (3, 8):
        policy = getattr(asyncio, "WindowsProactorEventLoopPolicy", None)
        if policy and not isinstance(asyncio.get_event_loop_policy(), policy):
            asyncio.set_event_loop_policy(policy())


def git_bash() -> str | None:
    """Windows 의 Git Bash 경로 (WSL 의 bash.exe 는 쓰지 않는다). POSIX 는 None."""
    if not WINDOWS:
        return None
    import shutil
    from pathlib import Path
    env = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if env and Path(env).is_file():
        return env
    roots = []
    git = shutil.which("git")
    if git:
        p = Path(git).resolve()
        roots += [p.parent.parent, p.parent.parent.parent]  # <Git>\cmd\git.exe, <Git>\mingw64\bin\git.exe
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                 os.environ.get("LOCALAPPDATA") and os.path.join(os.environ["LOCALAPPDATA"], "Programs")):
        if base:
            roots.append(Path(base) / "Git")
    for root in roots:
        bash = root / "bin" / "bash.exe"
        if bash.is_file():
            return str(bash)
    return None


async def create_shell(command: str, **kw) -> asyncio.subprocess.Process:
    """셸 명령 실행. Windows 는 에이전트가 쓰는 문법(bash)과 맞추려고 Git Bash 가 있으면 그걸로, 없으면 cmd."""
    kw = {**group_kwargs(), **kw}
    bash = git_bash()
    if bash:
        return await asyncio.create_subprocess_exec(bash, "-c", command, **kw)
    return await asyncio.create_subprocess_shell(command, **kw)


def pid_alive(pid: int) -> bool:
    """프로세스가 살아 있는지 (Windows 에서 os.kill(pid, 0) 은 CTRL_C 를 보내므로 쓰지 않는다)."""
    if WINDOWS:
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            return bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
