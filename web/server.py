"""duet 웹 UI 서버 — `python3 duet --web`.

- 로컬(127.0.0.1)에서만 열고, 실행마다 새 토큰을 만들어 주소에 붙인다 (토큰 없는 요청은 거부).
- 브라우저와 WebSocket 으로 이벤트를 실시간 전송하고, 승인·선택·명령·역할/설정 편집을 받는다.
- 오케스트레이터에게는 HumanUI(ask_approval / ask_choice) 구현체로 붙는다. 여러 승인을 동시에 띄울 수 있다.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import webbrowser
from collections import deque
from dataclasses import replace
from pathlib import Path
from typing import Any

from aiohttp import WSMsgType, web

from .. import commands
from ..core.config import PERMISSION_PROFILES, ROLE_PRESETS, SUPPORTED_CLIS, Config, Role, preset_role, presets_info, validate_role_name
from ..core.conventions import load_conventions, save_conventions
from ..core.events import Event, EventBus
from ..core.fsutil import is_link
from ..core.jev_settings import clear_api_key, get_api_key, key_status, set_api_key, validate_settings as validate_jev_settings
from ..core.models import read_cache, refresh
from ..core.orchestrator import Orchestrator
from ..core.policy import ApprovalRequest, Decision, read_only_decision

STATIC = Path(__file__).parent / "static"
REPLAY = 3000

# 웹에서 바꿀 수 있는 설정: 키 → (형 변환, 검사, 설명)
SETTINGS: dict[str, tuple[Any, Any, str]] = {
    "coding_conventions_enabled": (bool, None, "새로 시작하는 작업에 저장된 코딩 컨벤션을 적용합니다. 진행 중 작업은 시작 시점의 문서로 구현·리뷰합니다."),
    "jev_mode": (str, lambda v: v in ("off", "shadow", "on"), "끔: 기존 동작 · 관찰: JEV 판단을 기록하고 기존 동작 · 적용: JEV 판단을 실행에 반영"),
    "jev_model": (str, None, "TypeSafe 결정 모델 ID (기본 jev-1.13.0)"),
    "jev_timeout_sec": (float, lambda v: 1 <= v <= 60, "JEV 응답 대기 한도(초). 오류·시간 초과는 기존 절차로 복귀"),
    "jev_confidence_threshold": (float, lambda v: 0 <= v <= 1, "모든 JEV 판단에 적용할 최소 확신값 (0~1, 기본 0.8). 미달하면 기존 절차로 복귀"),
    "jev_choices": (bool, None, "자동 모드의 선택지를 작업 맥락에 따라 판단"),
    "jev_approvals": (bool, None, "승인 정책으로 결정하지 못한 요청을 판단. 명시적 보호·금지 정책은 유지"),
    "jev_flow": (bool, None, "질문 형식·다음 턴 전달·정체·응답 대기·완료 보고 인계를 판단. 잘못된 질문은 작성한 모델에 재작성 요청"),
    "verification_level": (str, lambda v: v in ("functional", "balanced", "thorough"),
                           "핵심 기능(기본): 정상 동작만 테스트, 예외·위험은 미검증 보고만 · 균형: 관련 회귀·경계까지 · 전체: 모든 기준·전체 테스트. 다음 턴부터 적용하며 기존 합의 명령은 유지합니다."),
    "usage_limit_retry": (bool, None, "사용량 한도 자동 대기·재개 (기본 꺼짐, 앱 실행 중에만 동작)"),
    "usage_limit_wait_sec": (int, lambda v: 60 <= v <= 604800, "재설정 시각이 없을 때 대기 시간(초), 기본 18000 = 5시간"),
    "usage_limit_max_retries": (int, lambda v: 1 <= v <= 100, "한 턴의 사용량 한도 재시도 횟수 (기본 3)"),
    "design_questions": (bool, None, "설계 질문 모드: 처음에 질문을 모아 확인하고, 진행 중 중요 질문은 사이클 끝에 직접 답변을 받습니다"),
    "full_auto": (bool, None, "전권 자동 수락: 승인·선택을 사람에게 묻지 않고 끝까지 진행 (git push·sudo·시스템 삭제·배포만 막음)"),
    "max_parallel": (int, lambda v: 1 <= v <= 32, "병렬 작업 동시 세션 수 상한 (설계자 제외)"),
    "auto_merge": (bool, None, "ACCEPT + 통합 테스트 통과 시 자동 병합 (끄면 사람 승인)"),
    "integration_test": (str, None, "병합 전 통합 테스트 명령 (비우면 작업별 test_command)"),
    "work_test_timeout": (int, lambda v: 10 <= v <= 86400, "병렬 작업 테스트 시간 한도(초)"),
    "work_baseline_test": (bool, None, "합의 직후 구현 전 상태에서 합의 테스트를 돌려 원래부터 실패하는 테스트 구분"),
    "compact_timeout_sec": (int, lambda v: 30 <= v <= 3600, "대화 압축 시간 한도(초). 넘기면 실패로 보고 계속 진행"),
    "plan_rounds": (int, lambda v: v >= 1, "계획 합의 라운드 한도"),
    "plan_approval": (str, lambda v: v in ("architect", "human"), "계획 최종 승인: architect | human"),
    "checkpoint_every": (int, lambda v: v >= 0, "체크포인트 간격(턴, 0=끔)"),
    "context_limit_tokens": (int, lambda v: v >= 0, "역할 기본 컨텍스트 한도(토큰)"),
    "compact_floor_tokens": (int, lambda v: v >= 0, "작업 경계 압축 기준(토큰)"),
    "prompt_max_chars": (int, lambda v: v >= 0, "한 턴 프롬프트 최대 글자"),
    "dialogue_max_kb": (int, lambda v: v >= 0, "DIALOGUE.md 보관 기준(KB)"),
    "git_snapshots": (bool, None, "턴마다 git 스냅샷 커밋"),
}


def _to_bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "on", "yes", "y")


class WebUI:
    """HumanUI 구현 + WebSocket 허브."""

    def __init__(self, cfg: Config, bus: EventBus, msgs: list[str], fake: bool, token: str):
        self.cfg, self.bus, self.fake, self.token = cfg, bus, fake, token
        self.orch = Orchestrator(cfg, bus, self, fake=fake)
        self.clients: set[web.WebSocketResponse] = set()
        self.history: deque[dict] = deque(maxlen=REPLAY)
        self.pending: dict[str, dict] = {}  # id -> {kind, payload, future}
        self.asks: dict[str, Any] = {}  # 질문 패널 세션 (역할별)
        self.ask_lock = asyncio.Lock()
        self.jev_test_lock = asyncio.Lock()
        self._steer_tasks: set[asyncio.Task] = set()
        self.models: dict | None = read_cache(cfg.dir)
        from ..core.config import detect_clis
        self.installed = set(SUPPORTED_CLIS) if fake else set(detect_clis())
        self.startup = msgs
        bus.subscribe(self._on_event)

    # ---------------- 방송 ----------------
    def _on_event(self, ev: Event) -> None:
        msg = {"type": "event", "kind": ev.kind, "role": ev.role, "data": ev.data, "ts": ev.ts}
        if ev.kind != "status":
            self.history.append(msg)
        self._broadcast(msg)

    def _broadcast(self, msg: dict) -> None:
        data = json.dumps(msg, ensure_ascii=False, default=str)
        for ws in list(self.clients):
            if ws.closed:
                self.clients.discard(ws)
                continue
            asyncio.ensure_future(self._send_raw(ws, data))

    @staticmethod
    async def _send_raw(ws: web.WebSocketResponse, data: str) -> None:
        try:
            await ws.send_str(data)
        except Exception:
            pass

    # ---------------- HumanUI ----------------
    async def _ask(self, kind: str, payload: dict) -> Any:
        rid = secrets.token_hex(6)
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = {"kind": kind, "payload": payload, "future": fut, "ts": time.time()}
        self._broadcast({"type": "pending", "id": rid, "kind": kind, "payload": payload})
        self._notify_desktop(kind, payload)
        try:
            return await fut
        finally:
            self.pending.pop(rid, None)
            self._broadcast({"type": "resolved", "id": rid})

    def _notify_desktop(self, kind: str, payload: dict) -> None:
        """브라우저 탭이 없으면 데스크톱 알림(macOS 알림 센터 / Windows 풍선 알림)으로라도 알린다."""
        if self.clients or os.environ.get("DUET_NO_NOTIFY"):
            return
        try:
            import subprocess
            import sys
            if sys.platform == "darwin":
                title = "duet 승인 요청" if kind == "approval" else "duet 선택 요청"
                text = (payload.get("summary") or payload.get("title") or "")[:120].replace('"', "'")
                subprocess.Popen(["osascript", "-e", f'display notification "{text}" with title "{title}"'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            elif os.name == "nt":
                title = "duet 승인 요청" if kind == "approval" else "duet 선택 요청"
                text = (payload.get("summary") or payload.get("title") or "")[:120]
                ps_str = lambda s: "'" + s.replace("'", "''") + "'"
                script = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
                          "$n = New-Object System.Windows.Forms.NotifyIcon; $n.Icon = [System.Drawing.SystemIcons]::Information; "
                          f"$n.Visible = $true; $n.ShowBalloonTip(8000, {ps_str(title)}, {ps_str(text or title)}, 'Info'); "
                          "Start-Sleep -Seconds 9; $n.Dispose()")
                subprocess.Popen(["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            pass

    async def ask_approval(self, req: ApprovalRequest, reason: str, opinion: str | None) -> Decision:
        ans = await self._ask("approval", {"role": req.role, "summary": req.summary, "kind": req.kind,
                                           "reason": reason, "opinion": opinion, "paths": req.paths})
        choice, why = ans.get("answer", "n"), (ans.get("reason") or "").strip()
        if choice == "a":
            return Decision(True, "사람 허용(세션)", scope="session", by="human")
        if choice == "y":
            return Decision(True, "사람 허용", by="human")
        return Decision(False, why or "사람이 거부", by="human")

    async def ask_choice(self, title: str, body: str, options: list[tuple[str, str]]) -> str:
        ans = await self._ask("choice", {"title": title, "body": body,
                                         "options": [{"key": k, "label": l} for k, l in options]})
        key = ans.get("key")
        return key if any(k == key for k, _ in options) else options[-1][0]

    # ---------------- 상태 ----------------
    def state(self) -> dict:
        o = self.orch
        return {
            "project": str(self.cfg.project),
            "name": self.cfg.project.name,
            "status": o.status(),
            "roles": [{**r.to_yaml(), "name": r.name, "effort": r.effort, "context_limit": r.context_limit,
                       "max_sessions": r.max_sessions, "is_main": r.name == self.cfg.main}
                      for r in self.cfg.roles.values()],
            "main": self.cfg.main,
            "clis": list(SUPPORTED_CLIS),
            "installed": sorted(self.installed),
            "presets": presets_info(self.installed),
            "permissions": list(PERMISSION_PROFILES),
            "modes": {n: {"max_turns": m.max_turns, "style": m.style, "autonomy": m.autonomy}
                      for n, m in self.cfg.modes.items()},
            "settings": {k: self.cfg.settings.get(k) for k in SETTINGS},
            "settings_help": {k: v[2] for k, v in SETTINGS.items()},
            "jev": key_status(self.cfg),
            "conventions": self._conventions_metadata(),
            "saves": self._saves(),
            "models": self.models,
            "pending": [{"id": k, "kind": v["kind"], "payload": v["payload"]} for k, v in self.pending.items()],
            "startup": self.startup,
            "fake": self.fake,
        }

    def _saves(self) -> list[dict]:
        try:
            return self.orch.list_saves()
        except (ValueError, OSError):
            return []

    # ---------------- 요청 처리 ----------------
    async def handle(self, msg: dict) -> dict | None:
        t = msg.get("type")
        o = self.orch
        if t == "answer":
            p = self.pending.get(str(msg.get("id")))
            if p and not p["future"].done():
                p["future"].set_result(msg)
            return None
        if t == "input":
            text = str(msg.get("text") or "")
            to = msg.get("to")
            if msg.get("immediate") is True and text.strip() and not text.startswith("/"):
                if to and to not in self.cfg.roles:
                    if to not in o.work.items:
                        return {"type": "output", "text": f"대상 '{to}' 가 없습니다."}
                task = asyncio.create_task(self._steer_input(text, to))
                self._steer_tasks.add(task)
                task.add_done_callback(self._steer_tasks.discard)
                return {"type": "output", "text": "메시지를 현재 턴에 전달하는 중…"}
            if to and not text.startswith("/"):
                if to in self.cfg.roles:
                    o.submit(text, to=to)
                    return {"type": "output", "text": f"→ {to}"}
                if to in o.work.items:
                    return {"type": "output", "text": o.work.message(to, text)}
            out = await commands.handle(o, text)
            return {"type": "output", "text": out} if out else None
        if t == "state":
            return {"type": "state", "state": self.state()}
        if t == "history":
            return {"type": "history", "events": list(self.history)}
        if t == "control":
            action = msg.get("action")
            if action == "pause":
                o.pause()
            elif action == "resume":
                o.resume()
            elif action == "stop":
                await o.stop()
            return {"type": "state", "state": self.state()}
        if t == "role_set":
            return await self._role_set(msg)
        if t == "role_add":
            return self._role_add(msg)
        if t == "role_remove":
            out = await o.remove_role(str(msg.get("name")))
            return {"type": "output", "text": out, "state": self.state()}
        if t == "setting":
            return self._setting(msg)
        if t == "conventions_read":
            return {"type": "conventions", "operation": "read", "conventions": load_conventions(self.cfg)}
        if t == "convention_save":
            return self._convention_save(msg)
        if t == "jev_key":
            return self._jev_key(msg)
        if t == "jev_test":
            return await self._jev_test()
        if t == "mode":
            out = o.set_mode(str(msg.get("name")))
            return {"type": "output", "text": out, "state": self.state()}
        if t == "turns":
            v = msg.get("value")
            out = o.set_max_turns(None if v in (None, "", "inf", 0) else int(v))
            return {"type": "output", "text": out, "state": self.state()}
        if t == "models_refresh":
            asyncio.ensure_future(self._refresh_models())
            return {"type": "output", "text": "모델 목록을 조회합니다… (CLI 별로 수 초)"}
        if t == "save":
            try:
                meta = o.save(msg.get("name") or None, note=str(msg.get("note") or ""), force=bool(msg.get("force")))
                return {"type": "output", "text": f"저장: {meta.get('name')}", "state": self.state()}
            except (ValueError, OSError) as e:
                return {"type": "output", "text": f"저장 실패: {e}"}
        if t == "load":
            try:
                out = await o.load_save(str(msg.get("name")))
                return {"type": "output", "text": out, "state": self.state()}
            except (ValueError, OSError) as e:
                return {"type": "output", "text": f"불러오기 실패: {e}"}
        if t == "delete_save":
            try:
                o.delete_save(str(msg.get("name")))
                return {"type": "output", "text": "삭제했습니다.", "state": self.state()}
            except (ValueError, OSError) as e:
                return {"type": "output", "text": f"삭제 실패: {e}"}
        if t == "work":
            action, wid = msg.get("action"), str(msg.get("id") or "")
            if action == "cancel":
                out = o.work.cancel(wid, str(msg.get("reason") or "사람이 취소"))
            elif action == "resume":
                out = o.work.resume(wid)
            else:
                out = "알 수 없는 작업 명령"
            return {"type": "output", "text": out}
        if t == "read":
            return self._read_file(str(msg.get("path") or ""))
        if t == "ask":
            asyncio.ensure_future(self._ask_turn(str(msg.get("role") or self.cfg.main), str(msg.get("text") or "")))
            return None
        if t == "ask_reset":
            await self._ask_close(str(msg.get("role") or ""))
            return {"type": "output", "text": "질문 세션을 닫았습니다."}
        return {"type": "output", "text": f"알 수 없는 요청: {t}"}

    async def _steer_input(self, text: str, to: str | None) -> None:
        try:
            if to and to not in self.cfg.roles:
                result = await self.orch.work.steer(to, text)
            else:
                result = await self.orch.steer(text, to=to)
            if result:
                self.orch.notice(result)
        except Exception as e:
            self.orch.notice(f"메시지 전달 오류: {type(e).__name__}: {e}", "warn")

    async def _refresh_models(self) -> None:
        clis = sorted({r.cli for r in self.cfg.roles.values()} | {c for c in SUPPORTED_CLIS if _has(c)})
        if self.fake:
            self.models = {"updated": time.time(), "clis": {c: {"models": [
                {"id": f"{c}-model-a", "name": f"{c} 모델 A"}, {"id": f"{c}-model-b", "name": f"{c} 모델 B"}]}
                for c in SUPPORTED_CLIS}}
        else:
            self.models = await refresh(self.cfg.dir, clis)
        self._broadcast({"type": "models", "models": self.models})

    async def _role_set(self, msg: dict) -> dict:
        name, field, value = str(msg.get("name")), str(msg.get("field")), msg.get("value")
        if field == "main":
            if name not in self.cfg.roles:
                return {"type": "output", "text": f"'{name}' 역할이 없습니다."}
            self.cfg.main = name
            self.orch.policy.main_role = name
            self.cfg.save_roles()
            return {"type": "output", "text": f"메인 역할: {name} (다음 턴부터)", "state": self.state()}
        if field == "permissions" and value not in PERMISSION_PROFILES:
            return {"type": "output", "text": "permissions 는 " + " / ".join(PERMISSION_PROFILES)}
        out = await self.orch.edit_role(name, field, "" if value is None else str(value))
        return {"type": "output", "text": out, "state": self.state()}

    def _role_add(self, msg: dict) -> dict:
        name = str(msg.get("name") or "")
        cli = str(msg.get("cli") or "")
        try:
            validate_role_name(name)
        except ValueError as e:
            return {"type": "output", "status": 400, "text": str(e), "error": str(e)}
        if name in self.cfg.roles:
            return {"type": "output", "text": f"'{name}' 역할이 이미 있습니다."}
        if cli not in SUPPORTED_CLIS:
            return {"type": "output", "text": "cli 는 " + " / ".join(SUPPORTED_CLIS)}
        perm = str(msg.get("permissions") or "workspace_write")
        if perm not in PERMISSION_PROFILES:
            perm = "workspace_write"
        try:
            ms = max(1, int(msg.get("max_sessions") or 1))
        except ValueError:
            ms = 1
        role = Role(name, cli, msg.get("model") or None, str(msg.get("brief") or ""), perm,
                    msg.get("effort") or None, max_sessions=ms)
        preset = str(msg.get("preset") or "")
        if preset in ROLE_PRESETS:  # 프리셋: 설명·권한 외에 역할 전용 도구(MCP·자동 허용)와 한도도 채운다
            base = preset_role(preset, self.installed, name=name, cli=cli, model=role.model)
            if base:
                base.brief = role.brief or base.brief
                base.permissions, base.max_sessions = perm, ms
                if role.effort:
                    base.effort = role.effort
                role = base
        self.orch.add_role(role)
        return {"type": "output", "text": f"역할 추가: {name} ({role.cli}/{role.model or '기본'})", "state": self.state()}

    def _setting(self, msg: dict) -> dict:
        key, raw = str(msg.get("key")), msg.get("value")
        if key not in SETTINGS:
            return {"type": "output", "text": f"바꿀 수 없는 설정: {key}"}
        conv, check, _ = SETTINGS[key]
        if key == "coding_conventions_enabled" and type(raw) is not bool:
            return {"type": "output", "text": "코딩 컨벤션 적용 여부는 참 또는 거짓이어야 합니다.", "state": self.state()}
        if key == "jev_confidence_threshold" and type(raw) is bool:
            return {"type": "output", "text": f"{key}: 형식이 맞지 않습니다"}
        try:
            value = _to_bool(raw) if conv is bool else conv(raw)
        except (TypeError, ValueError):
            return {"type": "output", "text": f"{key}: 형식이 맞지 않습니다"}
        if check and not check(value):
            return {"type": "output", "text": f"{key}: 허용 범위를 벗어났습니다"}
        if key.startswith("jev_"):
            try:
                validate_jev_settings({**self.cfg.settings, key: value})
            except ValueError as e:
                return {"type": "output", "text": str(e)}
        if key == "coding_conventions_enabled" and value:
            snapshot = load_conventions(self.cfg)
            if snapshot.get("error") or not snapshot.get("content", "").strip():
                return {"type": "output", "text": "먼저 읽을 수 있는 코딩 컨벤션 Markdown을 저장하세요.", "state": self.state()}
        if key == "usage_limit_retry":
            return {"type": "output", "text": self.orch.set_usage_retry(bool(value)), "state": self.state()}
        if key == "verification_level":
            return {"type": "output", "text": self.orch.set_verification_level(value), "state": self.state()}
        if key == "design_questions":
            return {"type": "output", "text": self.orch.set_design_questions(bool(value)), "state": self.state()}
        if key == "full_auto":
            return {"type": "output", "text": self.orch.set_full_auto(bool(value)), "state": self.state()}
        self.cfg.settings[key] = value
        self.cfg.save_roles()
        if key == "git_snapshots":
            self.orch.git.enabled = bool(value)
        self.orch.emit_status()
        if key == "coding_conventions_enabled":
            self._broadcast({"type": "convention_status", "conventions": self._conventions_metadata()})
            return {"type": "output", "text": "코딩 컨벤션: 새로 시작하는 작업부터 " + ("적용합니다." if value else "적용하지 않습니다."), "state": self.state()}
        return {"type": "output", "text": f"{key} = {value}", "state": self.state()}

    def _conventions_metadata(self) -> dict:
        return {k: v for k, v in load_conventions(self.cfg).items() if k != "content"}

    def _convention_save(self, msg: dict) -> dict:
        current = load_conventions(self.cfg)
        error = None
        if "expected_sha256" in msg and msg["expected_sha256"] != current.get("sha256", ""):
            error = "다른 창이나 파일에서 컨벤션이 바뀌었습니다. 저장본 다시 읽기로 내용을 확인한 뒤 저장하세요."
        else:
            try:
                save_conventions(self.cfg, msg.get("content"))
            except ValueError as exc:
                error = str(exc)
            except OSError:
                error = "코딩 컨벤션을 저장할 수 없습니다. 문서 경로와 쓰기 권한을 확인하세요."
        snapshot = load_conventions(self.cfg)
        if not error:
            self._broadcast({"type": "convention_status", "conventions": self._conventions_metadata()})
        return {"type": "conventions", "operation": "save", "saved": not error,
                "error": error, "conventions": snapshot}

    def _jev_key(self, msg: dict) -> dict:
        try:
            if msg.get("action") == "save":
                set_api_key(self.cfg, msg.get("key"))
                text = "JEV API 키를 저장했습니다. 연결 확인으로 인증을 검사할 수 있습니다."
            elif msg.get("action") == "clear":
                clear_api_key(self.cfg)
                text = "저장된 JEV API 키를 삭제했습니다."
                if key_status(self.cfg)["source"] == "environment":
                    text += " TYPESAFE_API_KEY 환경변수의 키는 계속 사용됩니다."
            else:
                return {"type": "output", "text": "JEV API 키 작업은 save 또는 clear입니다."}
        except ValueError as e:
            return {"type": "output", "text": str(e)}
        except OSError:
            return {"type": "output", "text": "JEV API 키 파일을 변경할 수 없습니다. 저장 경로와 권한을 확인하세요."}
        self.orch.emit_status()
        return {"type": "output", "text": text, "state": self.state()}

    async def _jev_test(self) -> dict:
        if self.jev_test_lock.locked():
            return {"type": "output", "text": "JEV 연결 확인이 이미 진행 중입니다."}
        async with self.jev_test_lock:
            key = get_api_key(self.cfg)
            if not key:
                return {"type": "output", "text": "먼저 JEV API 키를 저장하거나 TYPESAFE_API_KEY를 설정하세요."}
            from ..core.jev import JevClient
            client = JevClient(key, model=self.cfg.settings["jev_model"],
                               timeout_sec=self.cfg.settings["jev_timeout_sec"])
            try:
                result = await client.test_connection()
            except Exception:
                # Third-party/network exception messages must never expose credentials.
                result = {"ok": False, "message": "연결 확인에 실패했습니다. 키·네트워크·모델 설정을 확인하세요."}
            return {"type": "output", "text": "JEV: " + result["message"],
                    "jev_test": {"ok": bool(result["ok"])}, "state": self.state()}

    def _read_file(self, rel: str) -> dict:
        """대화·작업 기록·계획·기억 파일만 읽기 전용으로 보여준다."""
        allowed = ("DIALOGUE.md", "docs/", ".duet/memory/", ".duet/asks/", ".duet/reports/", ".duet/logs/jev/")
        rel = rel.lstrip("/")
        if not rel.startswith(allowed) or ".." in Path(rel).parts:
            return {"type": "file", "path": rel, "error": "열 수 없는 경로입니다."}
        path = self.cfg.project / rel
        try:
            parent = self.cfg.project
            for part in Path(rel).parts:
                parent = parent / part
                if is_link(parent):
                    return {"type": "file", "path": rel, "error": "심볼릭 링크 파일은 열 수 없습니다."}
            if rel == JEV_DIALOGUE_PATH:
                return {"type": "file", "path": rel, "text": _read_doc(self.cfg.project, rel)["content"]}
            text = path.read_text(encoding="utf-8", errors="replace")
        except (OSError, ValueError) as e:
            return {"type": "file", "path": rel, "error": str(e)}
        if len(text) > 400_000:
            text = "… (앞부분 생략)\n" + text[-400_000:]
        return {"type": "file", "path": rel, "text": text}

    # ---------------- 질문 패널 (/ask 의 웹판) ----------------
    async def _ask_turn(self, role_name: str, text: str) -> None:
        from ..adapters import make_adapter
        from ..core.prompts import ASK_SYSTEM
        if role_name not in self.cfg.roles or not text.strip():
            return
        async with self.ask_lock:
            ad = self.asks.get(role_name)
            if ad is None:
                role = replace(self.cfg.roles[role_name], name=f"질문:{role_name}", permissions="read_only")
                bus = EventBus()
                bus.subscribe(lambda ev: self._broadcast({"type": "ask_event", "role": role_name, "kind": ev.kind,
                                                          "data": ev.data}))
                base = self.cfg.state.sessions.get(role_name)

                async def deny(req: ApprovalRequest) -> Decision:
                    return read_only_decision(req, "질문 패널")
                ad = make_adapter(role, self.cfg.project, bus, deny, base, ASK_SYSTEM.replace("{role}", role_name),
                                  fake=self.fake, fork_session=bool(base))
                ad.plan_read_only = True
                try:
                    await ad.start()
                except Exception as e:
                    self._broadcast({"type": "ask_event", "role": role_name, "kind": "error",
                                     "data": {"text": f"세션 시작 실패: {e}"}})
                    return
                self.asks[role_name] = ad
                if not base:
                    text = (f"먼저 `.duet/memory/{role_name}.md` 와 DIALOGUE.md 최근 턴을 필요한 만큼 읽어 맥락을 잡은 뒤 "
                            f"답하세요.\n\n질문: {text}")
            self._broadcast({"type": "ask_event", "role": role_name, "kind": "busy", "data": {"busy": True}})
            tr = await ad.run_turn(text)
            self._broadcast({"type": "ask_event", "role": role_name, "kind": "answer",
                             "data": {"text": tr.full_text or tr.text or tr.error or "(응답 없음)", "ok": tr.ok}})
            limit = int(self.cfg.settings.get("ask_compact_tokens") or 0)
            if limit and ad.context_tokens > limit:
                from ..core.prompts import COMPACT_ASK
                await ad.compact(COMPACT_ASK)

    async def _ask_close(self, role_name: str) -> None:
        for name in ([role_name] if role_name else list(self.asks)):
            ad = self.asks.pop(name, None)
            if ad:
                try:
                    await ad.close()
                except Exception:
                    pass

    async def close(self) -> None:
        tasks = list(self._steer_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._ask_close("")
        for p in list(self.pending.values()):
            if not p["future"].done():
                p["future"].set_result({"answer": "n", "key": None, "reason": "duet 종료"})


def _has(cli: str) -> bool:
    from ..core.clis import which
    return which(cli) is not None


# ---------------------------------------------------------------- aiohttp 앱
DOC_LIMIT = 1024 * 1024
JEV_DIALOGUE_PATH = '.duet/logs/jev/DIALOGUE.md'
JEV_ENTRY_MARKER = b'<!-- duet-jev-entry -->\n'


def _doc_visible(parts: tuple[str, ...]) -> bool:
    if not parts or any(p in ('..', '.', '') for p in parts):
        return False
    directories = parts[:-1]
    if parts[0] == '.duet':
        if len(parts) >= 4 and parts[1:3] == ('logs', 'jev'):
            directories = parts[3:-1]
        elif len(parts) >= 3 and parts[1] == 'memory':
            directories = parts[2:-1]
        else:
            return False
    return not any(p.startswith('.') or p in ('node_modules', 'duet') for p in directories)


def _doc_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    if not raw or path.is_absolute() or not _doc_visible(tuple(raw.split('/'))) or path.suffix != '.md':
        raise ValueError('허용되지 않는 문서 경로')
    parent = root
    for part in path.parts[:-1]:
        parent = parent / part
        if is_link(parent):
            raise ValueError('심볼릭 링크 디렉터리는 문서 목록에서 제외됩니다')
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError('프로젝트 밖의 문서')
    relative = target.relative_to(root.resolve())
    if not _doc_visible(relative.parts) or relative.suffix != '.md':
        raise ValueError('제외된 문서')
    if not target.is_file():
        raise FileNotFoundError(raw)
    cumulative_jev = raw == JEV_DIALOGUE_PATH and relative.as_posix() == JEV_DIALOGUE_PATH
    if target.stat().st_size > DOC_LIMIT and not cumulative_jev:
        raise ValueError('문서 크기는 1MiB 이하이어야 합니다')
    return target


def _docs(root: Path) -> list[dict]:
    import heapq
    rows = []
    for base, dirs, files in os.walk(root, followlinks=False):
        rel = Path(base).relative_to(root)
        dirs[:] = [d for d in dirs if not is_link(Path(base)/d) and
                   (_doc_visible((rel/d/'file.md').parts) or (rel == Path('.') and d == '.duet')
                    or (rel == Path('.duet') and d == 'logs'))]
        for name in files:
            raw = (rel/name).as_posix()
            try:
                path = _doc_path(root, raw)
                stat = path.stat()
            except (OSError, ValueError, RuntimeError):
                continue
            row = (stat.st_mtime, raw, stat.st_size)
            if len(rows) < 2000: heapq.heappush(rows, row)
            elif row > rows[0]: heapq.heapreplace(rows, row)
    return [dict(path=p, size=s, mtime=m) for m, p, s in sorted(rows, reverse=True)]


def _read_doc(root: Path, raw: str) -> dict:
    path = _doc_path(root, raw)
    with path.open('rb') as f:
        stat = os.fstat(f.fileno())
        if raw == JEV_DIALOGUE_PATH and stat.st_size > DOC_LIMIT:
            f.seek(stat.st_size - DOC_LIMIT)
            content = f.read(DOC_LIMIT)
            marker = content.find(JEV_ENTRY_MARKER)
            notice = '> 오래된 앞부분을 생략하고 최근 최대 1MiB만 표시합니다. 전체 기록은 프로젝트의 `.duet/logs/jev/DIALOGUE.md`에 보존됩니다.\n\n'
            if marker >= 0:
                text = content[marker:].decode('utf-8')
            else:
                # A single large entry can fill the entire tail. Show it as
                # literal text so a partial Markdown fence cannot leak out.
                while content and 0x80 <= content[0] <= 0xBF:
                    content = content[1:]
                from ..core.jev_log import block
                text = block(content.decode('utf-8'))
            return dict(path=raw, content=notice + text, mtime=stat.st_mtime)
        content = f.read(DOC_LIMIT + 1)
    if len(content) > DOC_LIMIT:
        raise ValueError('문서 크기 초과')
    return dict(path=raw, content=content.decode('utf-8'), mtime=stat.st_mtime)


def build_app(ui: WebUI) -> web.Application:
    @web.middleware
    async def auth(request: web.Request, handler):
        if request.path.startswith("/static/") or request.path == "/favicon.ico":
            return await handler(request)
        tok = request.query.get("t") or request.cookies.get("duet_token") or ""
        if not secrets.compare_digest(tok, ui.token):
            return web.Response(status=403, text="duet: 토큰이 필요합니다. 터미널에 표시된 주소로 여세요.")
        resp = await handler(request)
        if request.query.get("t") and isinstance(resp, web.Response) and request.path == "/":
            resp.set_cookie("duet_token", ui.token, httponly=True, samesite="Strict")
        return resp

    app = web.Application(middlewares=[auth])

    async def index(request: web.Request) -> web.StreamResponse:
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def api_state(request: web.Request) -> web.Response:
        return web.json_response(ui.state(), dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))

    async def api_docs(request: web.Request) -> web.Response:
        origin = request.headers.get('Origin')
        if origin and origin.split('://', 1)[-1] != request.host:
            return web.Response(status=403, text='origin')
        if request.path == '/api/docs':
            return web.json_response(await asyncio.to_thread(_docs, ui.cfg.project))
        raw = request.query.get('path', '')
        try:
            return web.json_response(await asyncio.to_thread(_read_doc, ui.cfg.project, raw))
        except FileNotFoundError:
            return web.json_response({'error': '문서가 없습니다'}, status=404)
        except (ValueError, OSError, RuntimeError) as e:
            return web.json_response({'error': str(e)}, status=400)

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        origin = request.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] != request.host:
            return web.Response(status=403, text="origin")  # type: ignore[return-value]
        ws = web.WebSocketResponse(heartbeat=25, max_msg_size=8 * 1024 * 1024)
        await ws.prepare(request)
        ui.clients.add(ws)
        await ws.send_str(json.dumps({"type": "hello", "state": ui.state(), "history": list(ui.history)},
                                     ensure_ascii=False, default=str))
        try:
            async for m in ws:
                if m.type != WSMsgType.TEXT:
                    continue
                try:
                    msg = json.loads(m.data)
                except json.JSONDecodeError:
                    continue
                try:
                    reply = await ui.handle(msg)
                except Exception as e:  # 한 요청의 오류가 연결을 끊지 않게
                    reply = {"type": "output", "text": f"오류: {type(e).__name__}: {e}"}
                if reply is not None:
                    if msg.get("rid"):
                        reply["rid"] = msg["rid"]
                    await ws.send_str(json.dumps(reply, ensure_ascii=False, default=str))
        finally:
            ui.clients.discard(ws)
        return ws

    app.router.add_get("/", index)
    app.router.add_get("/api/state", api_state)
    app.router.add_get('/api/docs', api_docs)
    app.router.add_get('/api/doc', api_docs)
    app.router.add_get("/ws", ws_handler)
    app.router.add_static("/static/", STATIC)
    return app


async def serve(cfg: Config, bus: EventBus, msgs: list[str], fake: bool, first: str | None,
                host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(18)
    ui = WebUI(cfg, bus, msgs, fake, token)
    app = build_app(ui)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = None
    for p in range(port, port + 20):
        try:
            site = web.TCPSite(runner, host, p)
            await site.start()
            port = p
            break
        except OSError:
            site = None
    if site is None:
        raise SystemExit(f"[duet] {port}~{port + 19} 포트를 열 수 없습니다.")
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{shown}:{port}/?t={token}"
    try:
        (cfg.dir / "web.json").write_text(json.dumps({"url": url, "pid": os.getpid()}), encoding="utf-8")
    except OSError:
        pass
    for m in msgs:
        print("· " + m)
    print(f"\n[duet] 웹 UI: {url}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        print("[duet] 경고: 로컬 밖에서도 접속할 수 있게 열었습니다. 주소(토큰)를 다른 사람과 공유하지 마세요.")
    print("[duet] 끝내려면 이 터미널에서 Ctrl+C\n", flush=True)
    if open_browser and not os.environ.get("DUET_NO_BROWSER"):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    server = asyncio.create_task(ui.orch.serve())
    if ui.models is None:
        asyncio.ensure_future(ui._refresh_models())
    if first:
        ui.orch.submit(first)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    import signal
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None), getattr(signal, "SIGBREAK", None)):
        if sig is None or (sig == getattr(signal, "SIGHUP", None) and signal.getsignal(sig) is signal.SIG_IGN):  # nohup 이면 그대로
            continue
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            # Windows: 이벤트 루프 신호 처리기가 없어 일반 신호 처리기로 종료를 알린다
            try:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
            except (ValueError, OSError, RuntimeError):
                pass
    try:
        await stop.wait()
    finally:
        print("\n[duet] 종료하는 중…", flush=True)
        await ui.close()
        await ui.orch.stop()
        server.cancel()
        await ui.orch.close()
        for ws in list(ui.clients):
            await ws.close()
        await runner.cleanup()
        try:
            (cfg.dir / "web.json").unlink()
        except OSError:
            pass
