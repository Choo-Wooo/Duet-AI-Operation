"""입력창 명령 처리 (TUI/콘솔 공용)."""
from __future__ import annotations

import shlex

from .core.config import PERMISSION_PROFILES, SUPPORTED_CLIS, Role
from .core.orchestrator import Orchestrator
from .core.saves import format_saves

HELP = """명령어
  (일반 텍스트)              메인 역할(설계자)에게 전달
  /to <역할> <메시지>        특정 역할에게 직접 전달
  /mode [이름]               대화 모드 보기/변경 (sprint, review, deliberate, …)
  /turns <N|inf>            이번 요청의 턴 한도 (inf = 무제한)
  /auto on|off              off 면 위임 전마다 확인
  /role list                역할 목록
  /role add <이름> <claude|codex> <모델> <설명…>
  /role edit <이름> <항목>=<값>   (cli, model, brief, permissions, effort, context_limit)
  /ask [역할]               새 창에 질문 콘솔 열기 (기본 설계자, 본 작업과 분리된 읽기 전용 분신)
  /memory [역할]            역할의 작업 기억 파일 보기
  /compact [역할]           다음 턴 전에 그 역할 대화 압축 예약
  /role remove <이름>
  /pause  /resume  /stop    자동 진행 멈춤 / 재개 / 현재 턴 중단
  /rollback <턴번호>         그 턴의 git 스냅샷으로 되돌리기
  /save [이름] [--force] [-- 메모]   대화 세션 저장 (진행 중이면 /pause 후 턴 종료 대기)
  /saves                    저장된 세션 목록 (최신순)
  /load <이름>              세션 불러오기 (현재 상태 자동 저장, 진행 중에는 /stop 필요)
  /save-delete <이름>        저장본 삭제
  /status                   상태 보기
  /plan                     현재 합의 계획서 경로·버전·단계 보기
  /quit                     종료"""


def _fmt_roles(orch: Orchestrator) -> str:
    lines = []
    for r in orch.cfg.roles.values():
        mark = " ★메인" if r.name == orch.cfg.main else ""
        lines.append(f"  {r.name}{mark}: {r.cli}/{r.model or '기본'} [{r.permissions}] {r.brief}")
    return "역할\n" + "\n".join(lines)


def _fmt_status(orch: Orchestrator) -> str:
    s = orch.status()
    mt = "∞" if s["max_turns"] is None else s["max_turns"]
    return (f"모드 {s['mode']} · 턴 {s['run_turns']}/{mt} · 자동위임 {'on' if s['auto'] else 'off'} · "
            f"{'일시정지 · ' if s['paused'] else ''}실행 중 {s['running'] or '-'} · "
            f"Claude API 환산 ${s['cost_usd']:.2f} (구독이면 실제 청구 아님) · 토큰 {s['tokens']:,}" +
            (f"\n{s['task_summary']}" if s.get("task") else ""))


async def handle(orch: Orchestrator, line: str) -> str | None:
    """명령을 처리하고 사람에게 보여줄 문구를 돌려준다. 일반 텍스트는 메인 역할에게 보낸다."""
    line = line.strip()
    if not line:
        return None
    if not line.startswith("/"):
        orch.submit(line)
        return None
    cmd, _, rest = line[1:].partition(" ")
    rest = rest.strip()
    cmd = cmd.lower()

    if orch.loading and cmd not in ("help", "h", "?", "status", "saves"):
        return "세션을 불러오는 중입니다. 완료 후 다시 시도하세요."
    if cmd in ("help", "h", "?"):
        return HELP
    if cmd in ("save", "saves", "load", "save-delete"):
        try:
            if cmd == "saves":
                if rest:
                    return "사용법: /saves"
                return format_saves(orch.list_saves())
            parts = shlex.split(rest)
            if cmd == "save":
                note = ""
                if "--" in parts:
                    index = parts.index("--")
                    note = " ".join(parts[index + 1:])
                    parts = parts[:index]
                force = "--force" in parts
                names = [p for p in parts if p != "--force"]
                if len(names) > 1 or any(p.startswith("--") for p in names):
                    return "사용법: /save [이름] [--force] [-- 메모]"
                meta = orch.save(names[0] if names else None, note=note, force=force)
                return f"세션 {meta['name']} 저장됨 (턴 #{meta['last_n']})"
            if len(parts) != 1:
                return f"사용법: /{cmd} <이름>"
            if cmd == "load":
                return await orch.load_save(parts[0])
            orch.delete_save(parts[0])
            return f"저장본 {parts[0]} 삭제됨"
        except (ValueError, OSError) as e:
            return str(e)
    if cmd == "to":
        role, _, msg = rest.partition(" ")
        if role not in orch.cfg.roles or not msg.strip():
            return "사용법: /to <역할> <메시지>   역할: " + ", ".join(orch.cfg.roles)
        orch.submit(msg.strip(), to=role)
        return None
    if cmd == "mode":
        if not rest:
            cur = orch.cfg.state.mode
            return "모드\n" + "\n".join(
                f"  {'▶' if n == cur else ' '} {n}: 한도 {'∞' if m.max_turns is None else m.max_turns} — {m.style}"
                for n, m in orch.cfg.modes.items())
        return orch.set_mode(rest)
    if cmd == "turns":
        if rest.lower() in ("inf", "∞", "unlimited", "무제한", "0"):
            return orch.set_max_turns(None)
        if rest.isdigit():
            return orch.set_max_turns(int(rest))
        return "사용법: /turns <숫자|inf>"
    if cmd == "auto":
        if rest in ("on", "off"):
            return orch.set_auto(rest == "on")
        return "사용법: /auto on|off"
    if cmd == "role":
        sub, _, args = rest.partition(" ")
        if sub in ("", "list", "ls"):
            return _fmt_roles(orch)
        if sub == "add":
            parts = args.split(None, 3)
            if len(parts) < 2 or parts[1] not in SUPPORTED_CLIS:
                return "사용법: /role add <이름> <claude|codex> <모델> <설명…>"
            if parts[0] in orch.cfg.roles:
                return f"'{parts[0]}' 역할이 이미 있습니다."
            orch.add_role(Role(parts[0], parts[1], parts[2] if len(parts) > 2 else None,
                               parts[3] if len(parts) > 3 else "", "workspace_write"))
            return None
        if sub == "edit":
            name, _, kv = args.partition(" ")
            field, _, value = kv.partition("=")
            field, value = field.strip(), value.strip()
            if field == "permissions" and value not in PERMISSION_PROFILES:
                return "permissions: " + " | ".join(PERMISSION_PROFILES)
            return await orch.edit_role(name, field, value)
        if sub in ("remove", "rm", "del"):
            return await orch.remove_role(args.strip())
        return "사용법: /role list|add|edit|remove"
    if cmd == "pause":
        orch.pause()
        return "자동 진행을 멈춥니다 (현재 턴은 끝까지 실행). /resume 으로 재개"
    if cmd == "resume":
        orch.resume()
        return "재개합니다."
    if cmd == "stop":
        await orch.stop()
        return "현재 턴을 중단합니다."
    if cmd == "rollback":
        if not rest.lstrip("#").isdigit():
            return "사용법: /rollback <턴번호>"
        return await orch.rollback(int(rest.lstrip("#")))
    if cmd == "ask":
        from pathlib import Path
        from .ask import launch_window
        role = rest or orch.cfg.main
        if role not in orch.cfg.roles:
            return "역할: " + ", ".join(orch.cfg.roles)
        return launch_window(Path(__file__).resolve().parent, orch.project, role)
    if cmd == "memory":
        from .core.textutil import memory_path
        role = rest or orch.cfg.main
        path = memory_path(orch.project, role)
        if not path.exists():
            return f"{role} 작업 기억이 아직 없습니다 (다음 턴부터 자동으로 생깁니다)."
        return f"{path.relative_to(orch.project)}\n" + path.read_text(encoding="utf-8")[:4000]
    if cmd == "compact":
        role = rest or orch.cfg.main
        if role not in orch.cfg.roles:
            return "역할: " + ", ".join(orch.cfg.roles)
        orch._compact_forced.add(role)
        return f"{role}: 다음 턴 전에 대화를 압축합니다."
    if cmd == "status":
        return _fmt_status(orch)
    if cmd == "plan":
        s = orch.status()
        return s["task_summary"] + (f"\n{s['task']['plan_path']}" if s["task"] else "")
    return f"알 수 없는 명령: /{cmd}  (/help)"
