"""질문 콘솔(/ask): 설계자 세션을 복제(fork)한 읽기 전용 분신에게 프로젝트에 대해 묻는 별도 창.

- 본 작업 세션은 건드리지 않는다 (복제본이라 여기서 나눈 대화는 본 세션 기억에 섞이지 않음).
- 파일 쓰기·명령 실행은 막고 읽기·검색만 허용한다.
- duet 본체가 새 터미널 창으로 띄우고, 창에서 /quit 하거나 duet 을 끄면 창이 닫힌다.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from .core.config import Config
from .core.events import Event, EventBus
from .core.policy import ApprovalRequest, Decision, read_only_decision
from .core.procs import WINDOWS, terminate_tree
from .core.prompts import ASK_SYSTEM, COMPACT_ASK

DIM, BOLD, CYAN, RED, RESET = "\033[2m", "\033[1m", "\033[36m", "\033[31m", "\033[0m"


def _asks_dir(project: Path) -> Path:
    d = project / ".duet" / "asks"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------- 창 띄우기 / 닫기
def _applescript_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def launch_window(duet_dir: Path, project: Path, role: str) -> str:
    """새 터미널 창에서 질문 콘솔을 연다. 사람에게 보여줄 결과 문구를 돌려준다."""
    cmd = (f"cd {shlex.quote(str(project))} && DUET_ASK_WINDOW=1 exec {shlex.quote(sys.executable)} "
           f"{shlex.quote(str(duet_dir))} --no-venv --ask {shlex.quote(role)}")
    try:
        if WINDOWS:
            # 새 콘솔 창에서 실행한다. 프로세스가 끝나면 창도 닫힌다
            env = {**os.environ, "DUET_ASK_WINDOW": "1", "PYTHONUTF8": "1"}
            subprocess.Popen([sys.executable, str(duet_dir), "--no-venv", "--ask", role], cwd=str(project), env=env,
                             creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP)
            return f"새 창에서 {role} 질문 콘솔을 열었습니다. 창에서 /quit 하면 닫힙니다."
        if sys.platform == "darwin":
            if os.environ.get("TERM_PROGRAM") == "iTerm.app":
                script = (f'tell application "iTerm" to create window with default profile command '
                          f'{_applescript_str("/bin/zsh -lc " + shlex.quote(cmd))}')
            else:
                script = (f'tell application "Terminal"\n do script {_applescript_str(cmd)}\n activate\nend tell')
            subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=15)
            return f"새 창에서 {role} 질문 콘솔을 열었습니다. 창에서 /quit 하면 닫힙니다."
        for term, args in (("gnome-terminal", ["--", "bash", "-lc", cmd]),
                           ("konsole", ["-e", "bash", "-lc", cmd]),
                           ("x-terminal-emulator", ["-e", "bash", "-lc", cmd])):
            if shutil.which(term):
                subprocess.Popen([term, *args], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f"새 창에서 {role} 질문 콘솔을 열었습니다."
    except Exception as e:
        return f"새 창을 열지 못했습니다 ({e}). 다른 터미널에서 직접 실행하세요: {cmd}"
    return f"새 창을 열 수 있는 터미널을 찾지 못했습니다. 다른 터미널에서 실행하세요: {cmd}"


def _close_own_window() -> None:
    """duet 이 띄운 창이면, 프로세스가 끝난 직후 이 창을 닫는다 (macOS Terminal)."""
    if os.environ.get("DUET_ASK_WINDOW") != "1" or sys.platform != "darwin":
        return
    if os.environ.get("TERM_PROGRAM") == "iTerm.app":
        return  # iTerm 은 명령이 끝나면 창이 닫힌다
    try:
        tty = os.ttyname(sys.stdin.fileno())
    except OSError:
        return
    script = f'''delay 0.6
tell application "Terminal"
  repeat with w in windows
    try
      if tty of selected tab of w is {_applescript_str(tty)} then close w saving no
    end try
  end repeat
end tell'''
    subprocess.Popen(["osascript", "-e", script], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def stop_all(project: Path) -> int:
    """duet 본체가 꺼질 때 열린 질문 콘솔을 모두 닫는다."""
    n = 0
    d = project / ".duet" / "asks"
    for pid_file in d.glob("*.pid") if d.exists() else []:
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            if WINDOWS:
                terminate_tree(pid)  # 창의 파이썬과 그 안의 CLI 까지
            else:
                os.kill(pid, signal.SIGTERM)
            n += 1
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            pass
        try:
            pid_file.unlink()
        except OSError:
            pass
    return n


# ---------------------------------------------------------------- 콘솔
def _print_event(ev: Event) -> None:
    d = ev.data
    if ev.kind == "text":
        print(f"\n{d['text']}\n", flush=True)
    elif ev.kind == "tool":
        print(f"{DIM}  ▸ {d['detail'][:160]}{RESET}", flush=True)
    elif ev.kind == "notice":
        print(f"{DIM}  · {d['text']}{RESET}", flush=True)
    elif ev.kind == "error":
        print(f"{RED}  ✗ {d['text']}{RESET}", flush=True)


def _ainput(loop: asyncio.AbstractEventLoop, prompt: str) -> asyncio.Future:
    """데몬 스레드로 input() — 창이 닫힐 때 프로세스 종료를 막지 않도록."""
    fut = loop.create_future()

    def deliver(fn, value):
        if not fut.done():
            fn(value)

    def worker():
        try:
            value = input(prompt)
            cb = (deliver, fut.set_result, value)
        except BaseException as e:  # EOFError, KeyboardInterrupt
            cb = (deliver, fut.set_exception, e)
        try:
            loop.call_soon_threadsafe(*cb)
        except RuntimeError:
            pass

    threading.Thread(target=worker, daemon=True).start()
    return fut


async def _read_only_approver(req: ApprovalRequest) -> Decision:
    return read_only_decision(req, "질문 콘솔")


async def run_ask(cfg: Config, role_name: str | None = None, fake: bool = False) -> int:
    from .adapters import make_adapter

    role_name = role_name or cfg.main
    if role_name not in cfg.roles:
        print(f"[duet] '{role_name}' 역할이 없습니다. 역할: {', '.join(cfg.roles)}")
        return 2
    project = cfg.project
    pid_file = _asks_dir(project) / f"{os.getpid()}.pid"
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    transcript = _asks_dir(project) / f"{time.strftime('%Y%m%d-%H%M%S')}-{role_name}.md"
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is None:  # Windows 에는 SIGHUP 이 없다
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass

    role = replace(cfg.roles[role_name], permissions="read_only")
    base_session = cfg.state.sessions.get(role_name)
    bus = EventBus()
    bus.subscribe(_print_event)
    ad = make_adapter(role, project, bus, _read_only_approver, base_session,
                      ASK_SYSTEM.replace("{role}", role_name), fake=fake, fork_session=bool(base_session))
    ad.plan_read_only = True  # Claude: 읽기·검색 도구와 읽기 명령만, Codex: 읽기 전용 샌드박스
    print(f"{BOLD}duet 질문 콘솔 — {role_name}{RESET}  ({role.cli}/{role.model or '기본'})")
    print(f"{DIM}{'현재 ' + role_name + ' 세션을 복제해 지금까지의 맥락을 이어받습니다.' if base_session else '아직 본 세션이 없어 작업 기억과 DIALOGUE.md 로 맥락을 잡습니다.'}"
          f" 본 작업에는 영향이 없습니다. 끝내려면 /quit{RESET}")
    try:
        await ad.start()
    except Exception as e:
        print(f"{RED}세션을 시작하지 못했습니다: {e}{RESET}")
        pid_file.unlink(missing_ok=True)
        if os.environ.get("DUET_ASK_WINDOW") == "1":
            _pause_before_close()
            _close_own_window()
        return 1

    limit = int(cfg.settings.get("ask_compact_tokens") or 0)
    first = True
    try:
        with transcript.open("a", encoding="utf-8") as log:
            log.write(f"# 질문 콘솔 — {role_name} · {time.strftime('%Y-%m-%d %H:%M')}\n\n")
            while not stop.is_set():
                read = _ainput(loop, f"{CYAN}질문> {RESET}")
                waiter = asyncio.ensure_future(stop.wait())
                done, _ = await asyncio.wait({read, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if waiter in done and read not in done:
                    break
                waiter.cancel()
                try:
                    q = read.result().strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not q:
                    continue
                if q in ("/quit", "/exit", "/q"):
                    break
                prompt = q
                if first and not base_session:
                    prompt = (f"먼저 `.duet/memory/{role_name}.md` 와 DIALOGUE.md 의 최근 턴을 필요한 만큼 읽어 "
                              f"프로젝트 맥락을 잡은 뒤 답하세요.\n\n질문: {q}")
                first = False
                tr = await ad.run_turn(prompt)
                if not tr.ok:
                    print(f"{RED}  ✗ {tr.error}{RESET}")
                log.write(f"## 질문\n{q}\n\n## 답\n{tr.full_text or tr.text or tr.error or ''}\n\n")
                log.flush()
                if limit and ad.context_tokens > limit:
                    print(f"{DIM}  · 대화가 {ad.context_tokens:,} 토큰이라 압축합니다…{RESET}", flush=True)
                    await ad.compact(COMPACT_ASK)
    finally:
        await ad.close()
        pid_file.unlink(missing_ok=True)
        print(f"{DIM}질문 기록: {transcript.relative_to(project)}{RESET}")
        _close_own_window()
    return 0


def _pause_before_close() -> bool:
    try:
        input("Enter 를 누르면 창을 닫습니다.")
    except (EOFError, KeyboardInterrupt):
        pass
    return True
