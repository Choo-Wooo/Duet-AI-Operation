"""duet 실행: 인자 처리 → 프로젝트 자동 설정 → TUI(또는 콘솔) 시작."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .core.config import Config, default_roles, detect_clis
from .core.dialogue import Dialogue
from .core.events import EventBus
from .core.gitops import Git
from .core.saves import format_saves, list_saves


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="duet", description="Claude Code × Codex 오케스트레이터")
    p.add_argument("--mode", help="대화 모드 (sprint, review, deliberate, …)")
    p.add_argument("--max-turns", help="요청당 턴 한도 (숫자 또는 inf)")
    p.add_argument("--budget-usd", type=float, help="비용 한도(선택, 기본 없음)")
    p.add_argument("--max-hours", type=float, help="시간 한도(선택, 기본 없음)")
    p.add_argument("--fake", action="store_true", help="CLI 없이 가짜 에이전트로 흐름 시험")
    p.add_argument("--no-tui", action="store_true", help="분할 화면 대신 단순 콘솔 모드")
    p.add_argument("--no-venv", action="store_true", help="가상환경 자동 준비를 건너뜀")
    session = p.add_mutually_exclusive_group()
    session.add_argument("--new-session", action="store_true", help="저장된 에이전트 세션을 버리고 새로 시작")
    session.add_argument("--load", metavar="이름", help="저장된 대화 세션을 불러온 뒤 시작")
    p.add_argument("--list-saves", action="store_true", help="저장된 세션 목록 출력 후 종료")
    p.add_argument("-m", "--message", help="시작하자마자 설계자에게 보낼 메시지")
    return p.parse_args(argv)


def setup_project(duet_dir: Path, project: Path, fake: bool) -> tuple[Config, list[str]]:
    """처음 실행이면 .duet/ 설정을 자동으로 만든다."""
    msgs: list[str] = []
    cfg = Config(project)
    cfg.dir.mkdir(exist_ok=True)
    clis = detect_clis()
    if not cfg.roles_file.exists():
        if fake:
            clis = {"claude": "fake", "codex": "fake"}
        if not clis:
            print("[duet] claude 또는 codex CLI 를 찾지 못했습니다. 하나 이상 설치하고 로그인한 뒤 다시 실행하세요.")
            print("  Claude Code: npm i -g @anthropic-ai/claude-code   (또는 https://claude.com/claude-code)")
            print("  Codex CLI  : npm i -g @openai/codex")
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
    git = Git(project, bool(cfg.settings.get("git_snapshots", True)))
    m = git.ensure_repo()
    if m:
        msgs.append(m)
    if git.enabled:
        git.ensure_gitignore(duet_dir.name)
    if Dialogue(project).ensure():
        msgs.append("공유 대화 문서 DIALOGUE.md 를 만들었습니다.")
    return cfg, msgs


def main(duet_dir: Path, project: Path, argv: list[str]) -> int:
    args = parse_args(argv)
    if args.list_saves:
        try:
            print(format_saves(list_saves(Config(project))))
        except (ValueError, OSError) as e:
            print(f"[duet] {e}")
            return 2
        return 0
    cfg, msgs = setup_project(duet_dir, project, args.fake)
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
        if args.no_tui:
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
