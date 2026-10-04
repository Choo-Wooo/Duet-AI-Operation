"""duet 실행: 인자 처리 → 프로젝트 자동 설정 → TUI(또는 콘솔) 시작."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from .core.config import Config, default_roles, detect_clis, ensure_preset_roles
from .core.dialogue import Dialogue
from .core.events import EventBus
from .core.gitops import Git
from .core.saves import format_saves, list_saves


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="duet", description="Claude Code × Codex × Antigravity 오케스트레이터")
    p.add_argument("--mode", help="대화 모드 (sprint, review, deliberate, …)")
    p.add_argument("--max-turns", help="요청당 턴 한도 (숫자 또는 inf)")
    p.add_argument("--budget-usd", type=float, help="비용 한도(선택, 기본 없음)")
    p.add_argument("--max-hours", type=float, help="시간 한도(선택, 기본 없음)")
    p.add_argument("--fake", action="store_true", help="CLI 없이 가짜 에이전트로 흐름 시험")
    p.add_argument("--no-tui", action="store_true", help="분할 화면 대신 단순 콘솔 모드")
    p.add_argument("--web", action="store_true", help="브라우저 웹 UI 로 실행 (http://127.0.0.1:8765)")
    p.add_argument("--port", type=int, default=8765, help="웹 UI 포트 (사용 중이면 다음 번호)")
    p.add_argument("--host", default="127.0.0.1", help="웹 UI 주소 (기본 127.0.0.1 = 이 컴퓨터에서만)")
    p.add_argument("--no-browser", action="store_true", help="웹 UI 를 열 때 브라우저를 자동으로 띄우지 않음")
    p.add_argument("--no-venv", action="store_true", help="가상환경 자동 준비를 건너뜀")
    p.add_argument("--doctor", action="store_true", help="환경 점검 (--fix 로 고칠 수 있는 것 고치기, --yes 로 묻지 않기)")
    p.add_argument("--fix", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--yes", "-y", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--no-login", action="store_true", help=argparse.SUPPRESS)
    session = p.add_mutually_exclusive_group()
    session.add_argument("--new-session", action="store_true", help="저장된 에이전트 세션을 버리고 새로 시작")
    session.add_argument("--load", metavar="이름", help="저장된 대화 세션을 불러온 뒤 시작")
    p.add_argument("--list-saves", action="store_true", help="저장된 세션 목록 출력 후 종료")
    p.add_argument("-m", "--message", help="시작하자마자 설계자에게 보낼 메시지")
    bench = p.add_argument_group("무인 벤치마크 (--bench)")
    bench.add_argument("--bench", action="store_true",
                       help="과제 하나를 사람 없이 끝까지 진행하고 결과 커밋·지표를 남긴 뒤 종료 (-m 또는 --bench-task)")
    bench.add_argument("--bench-task", metavar="파일", help="과제 설명 파일 (instruction.md 등)")
    bench.add_argument("--bench-out", metavar="파일", help="지표 JSON 저장 위치 (기본 .duet/bench-result.json)")
    bench.add_argument("--bench-parallel", type=int, metavar="N", help="병렬 작업 동시 실행 수 (1 이면 병렬 없음)")
    bench.add_argument("--bench-timeout", type=float, default=10200.0, metavar="초", help="전체 시간 제한 (기본 10200초)")
    bench.add_argument("--bench-nudges", type=int, default=3, metavar="N",
                       help="사람 차례로 멈췄을 때 스스로 마무리하라고 안내할 횟수 (기본 3)")
    bench.add_argument("--role-model", action="append", default=[], metavar="역할=cli/모델",
                       help="역할의 CLI·모델 지정 (여러 번 가능). 예: architect=claude/claude-opus-5-5")
    p.add_argument("--ask", nargs="?", const="", metavar="역할",
                   help="질문 콘솔만 실행 (기본: 메인 역할). 보통은 duet 안에서 /ask 로 새 창을 엽니다")
    return p.parse_args(argv)


def setup_project(duet_dir: Path, project: Path, fake: bool) -> tuple[Config, list[str]]:
    """처음 실행이면 .duet/ 설정을 자동으로 만든다."""
    msgs: list[str] = []
    cfg = Config(project)
    cfg.dir.mkdir(exist_ok=True)
    clis = detect_clis()
    if not cfg.roles_file.exists():
        if fake:
            clis = {"claude": "fake", "codex": "fake", "agy": "fake"}
        if not clis:
            print("[duet] claude / codex / agy CLI 를 찾지 못했습니다. 하나 이상 설치하고 로그인한 뒤 다시 실행하세요.")
            print("  Claude Code: npm i -g @anthropic-ai/claude-code   (또는 https://claude.com/claude-code)")
            print("  Codex CLI  : npm i -g @openai/codex")
            print("  Antigravity: https://antigravity.google (agy)")
            sys.exit(2)
        cfg.main, cfg.roles = default_roles(clis)
        cfg.save_roles()
        desc = ", ".join(f"{r.name}={r.cli}/{r.model}" for r in cfg.roles.values())
        msgs.append(f"역할 설정을 만들었습니다: {desc}  (.duet/roles.yaml, /role 로 변경)")
    if not cfg.modes_file.exists():
        cfg.save_modes()
    if not cfg.policy_file.exists():
        cfg.save_policy()
    cfg.load()
    # 기존 프로젝트에 없는 기본 역할(디자이너·리서처)을 한 번 채워 넣는다
    msgs.extend(ensure_preset_roles(cfg, {"claude": 1, "codex": 1, "agy": 1} if fake else clis))
    if not fake:
        used = sorted({r.cli for r in cfg.roles.values()})
        missing = [c for c in used if c not in clis]
        if missing:
            msgs.append(f"경고: {', '.join(missing)} CLI 를 찾지 못했습니다. 해당 역할은 시작할 때 오류가 납니다.")
        from .core.clis import resolve
        for c in used:
            if c in clis:
                path, ver, seen = resolve(c)
                msgs.append(f"{c}: {ver} ({path}) — 설정·로그인은 설치된 {c} 그대로 사용")
                if len(seen) > 1:
                    others = ", ".join(f"{p} [{v}]" for p, v in seen if p != path)
                    msgs.append(f"  {c} 가 여러 곳에 있어 가장 최신 버전을 씁니다. 다른 설치본: {others}")
        from .core.accounts import check_accounts
        print("[duet] CLI 로그인 계정을 확인하는 중…", flush=True)
        msgs.extend(check_accounts([c for c in used if c in clis]))
    git = Git(project, bool(cfg.settings.get("git_snapshots", True)))
    m = git.ensure_repo()
    if m:
        msgs.append(m)
    if git.enabled:
        git.ensure_gitignore(duet_dir.name)
    if Dialogue(project).ensure():
        msgs.append("공유 대화 문서 DIALOGUE.md 를 만들었습니다.")
    return cfg, msgs


def _run_bench(args: argparse.Namespace, cfg: Config, msgs: list[str], duet_dir: Path) -> int:
    from .bench import run_bench
    task = args.message or ""
    if args.bench_task:
        task = Path(args.bench_task).read_text(encoding="utf-8")
    if not task.strip():
        print("[duet] --bench 에는 과제가 필요합니다: -m \"과제\" 또는 --bench-task 파일")
        return 2
    if args.budget_usd is not None:
        cfg.runtime["budget_usd"] = args.budget_usd
    out = Path(args.bench_out) if args.bench_out else cfg.dir / "bench-result.json"
    bus = EventBus(cfg.logs_dir)
    bus.subscribe(lambda ev: (lambda s: s and print(s, flush=True))(_bench_line(ev)))
    try:
        result = asyncio.run(run_bench(cfg, bus, msgs, args.fake, task, out=out, parallel=args.bench_parallel,
                                       timeout=args.bench_timeout, nudges=args.bench_nudges,
                                       duet_dirname=duet_dir.name))
    finally:
        bus.close()
    return 0 if result["finished"] in ("done", "stalled", "timeout") else 1


def _bench_line(ev) -> str | None:
    from .console import fmt_event
    if ev.kind in ("text", "tool_output"):
        return None  # 로그는 .duet/logs 에 남는다. 화면에는 흐름만
    return fmt_event(ev)


def _install_exit_cleanup() -> None:
    """어떤 경로로 끝나든 duet 이 띄운 하위 프로세스 그룹(codex app-server, 테스트, MCP 등)을 남기지 않는다.

    하위 프로세스는 새 세션으로 띄우므로 터미널·SSH 가 끊겨도(SIGHUP) 함께 죽지 않는다. 정리하지 않으면
    고아로 남아 포트와 연결을 계속 쥔다. SIGHUP 은 Ctrl+C 처럼 정상 종료 경로로 돌린다."""
    import atexit
    import signal
    from .core.procs import kill_all_groups
    atexit.register(kill_all_groups)
    hup = getattr(signal, "SIGHUP", None)
    if hup is not None and signal.getsignal(hup) is signal.SIG_DFL:  # nohup 으로 띄웠으면(SIG_IGN) 그대로 둔다
        def on_hup(*_):
            raise KeyboardInterrupt
        try:
            signal.signal(hup, on_hup)
        except (ValueError, OSError):
            pass


def main(duet_dir: Path, project: Path, argv: list[str]) -> int:
    # Claude Code 안에서 실행된 경우 그 세션 정보가 하위 claude 로 새지 않게 한다
    for key in ("CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION"):
        os.environ.pop(key, None)
    args = parse_args(argv)
    _install_exit_cleanup()
    if args.list_saves:
        try:
            print(format_saves(list_saves(Config(project))))
        except (ValueError, OSError) as e:
            print(f"[duet] {e}")
            return 2
        return 0
    if args.ask is not None:
        from .ask import run_ask
        cfg = Config(project)
        if not cfg.roles_file.exists():
            cfg, _ = setup_project(duet_dir, project, args.fake)
        else:
            cfg.load()
        try:
            return asyncio.run(run_ask(cfg, args.ask or None, args.fake))
        except KeyboardInterrupt:
            return 0
    cfg, msgs = setup_project(duet_dir, project, args.fake)
    if args.role_model:
        from .bench import apply_role_models
        try:
            msgs.extend("역할 지정: " + x for x in apply_role_models(cfg, args.role_model))
        except ValueError as e:
            print(f"[duet] {e}")
            return 2
    if args.bench:
        return _run_bench(args, cfg, msgs, duet_dir)
    if args.load:
        from .console import ConsoleUI
        from .core.orchestrator import Orchestrator
        load_bus = EventBus(cfg.logs_dir)
        load_bus.subscribe(lambda ev: msgs.append(ev.data["text"]) if ev.kind == "notice" else None)
        try:
            orch = Orchestrator(cfg, load_bus, ConsoleUI(), fake=args.fake)
            asyncio.run(orch.load_save(args.load))
        except (ValueError, OSError) as e:
            print(f"[duet] {e}")
            return 2
        finally:
            load_bus.close()
    if args.new_session:
        cfg.state.sessions.clear()
        cfg.state.reviewer_sessions.clear()
        cfg.state.fork_on_resume.clear()
        cfg.state.reviewer_fork_on_resume.clear()
    if args.mode:
        if args.mode not in cfg.modes:
            print(f"[duet] 모드는 {', '.join(cfg.modes)} 중 하나입니다.")
            return 2
        cfg.state.mode = args.mode
        cfg.state.max_turns = -1
    if args.max_turns:
        v = args.max_turns.lower()
        cfg.state.max_turns = None if v in ("inf", "0", "unlimited") else int(v)
    if args.budget_usd is not None:
        cfg.runtime["budget_usd"] = args.budget_usd
    if args.max_hours is not None:
        cfg.runtime["max_hours"] = args.max_hours
    cfg.save_state()

    bus = EventBus(cfg.logs_dir)
    try:
        if args.web:
            from .web.server import serve
            asyncio.run(serve(cfg, bus, msgs, args.fake, args.message, host=args.host, port=args.port,
                              open_browser=not args.no_browser))
        elif args.no_tui:
            from .console import run_console
            asyncio.run(run_console(cfg, bus, msgs, args.fake, args.message))
        else:
            from .tui.app import DuetApp
            DuetApp(cfg, bus, msgs, args.fake, args.message).run()
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()
    return 0
