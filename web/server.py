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
from ..core.events import Event, EventBus
from ..core.models import read_cache, refresh
from ..core.orchestrator import Orchestrator
from ..core.policy import ApprovalRequest, Decision, read_only_decision

STATIC = Path(__file__).parent / "static"
REPLAY = 3000

# 웹에서 바꿀 수 있는 설정: 키 → (형 변환, 검사, 설명)
SETTINGS: dict[str, tuple[Any, Any, str]] = {
    "full_auto": (bool, None, "전권 자동 수락: 승인·선택을 사람에게 묻지 않고 끝까지 진행 (git push·sudo·시스템 삭제·배포만 막음)"),
    "max_parallel": (int, lambda v: 1 <= v <= 32, "병렬 작업 동시 세션 수 상한 (설계자 제외)"),
    "auto_merge": (bool, None, "ACCEPT + 통합 테스트 통과 시 자동 병합 (끄면 사람 승인)"),
    "integration_test": (str, None, "병합 전 통합 테스트 명령 (비우면 작업별 test_command)"),
    "work_test_timeout": (int, lambda v: 10 <= v <= 86400, "병렬 작업 테스트 시간 한도(초)"),
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
        """브라우저 탭이 없으면 macOS 알림으로라도 알린다."""
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
        try:
            value = _to_bool(raw) if conv is bool else conv(raw)
        except (TypeError, ValueError):
            return {"type": "output", "text": f"{key}: 형식이 맞지 않습니다"}
        if check and not check(value):
            return {"type": "output", "text": f"{key}: 허용 범위를 벗어났습니다"}
        if key == "full_auto":
            return {"type": "output", "text": self.orch.set_full_auto(bool(value)), "state": self.state()}
        self.cfg.settings[key] = value
        self.cfg.save_roles()
        if key == "git_snapshots":
            self.orch.git.enabled = bool(value)
        self.orch.emit_status()
        return {"type": "output", "text": f"{key} = {value}", "state": self.state()}

    def _read_file(self, rel: str) -> dict:
        """대화·작업 기록·계획·기억 파일만 읽기 전용으로 보여준다."""
        allowed = ("DIALOGUE.md", "docs/", ".duet/memory/", ".duet/asks/", ".duet/reports/")
        rel = rel.lstrip("/")
        if not rel.startswith(allowed) or ".." in Path(rel).parts:
            return {"type": "file", "path": rel, "error": "열 수 없는 경로입니다."}
        path = self.cfg.project / rel
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
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
        await self._ask_close("")
        for p in list(self.pending.values()):
            if not p["future"].done():
                p["future"].set_result({"answer": "n", "key": None, "reason": "duet 종료"})


def _has(cli: str) -> bool:
    from ..core.clis import which
    return which(cli) is not None


# ---------------------------------------------------------------- aiohttp 앱
DOC_LIMIT = 1024 * 1024


def _doc_visible(parts: tuple[str, ...]) -> bool:
    if not parts or any(p in ('..', '.', '') for p in parts):
        return False
    directories = parts[:-1]
    if parts[0] == '.duet':
        if len(parts) < 3 or parts[1] != 'memory':
            return False
        directories = parts[2:-1]
    return not any(p.startswith('.') or p in ('node_modules', 'duet') for p in directories)


def _doc_path(root: Path, raw: str) -> Path:
    path = Path(raw)
    if not raw or path.is_absolute() or not _doc_visible(tuple(raw.split('/'))) or path.suffix != '.md':
        raise ValueError('허용되지 않는 문서 경로')
    parent = root
    for part in path.parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise ValueError('심볼릭 링크 디렉터리는 문서 목록에서 제외됩니다')
    target = (root / path).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError('프로젝트 밖의 문서')
    relative = target.relative_to(root.resolve())
    if not _doc_visible(relative.parts) or relative.suffix != '.md':
        raise ValueError('제외된 문서')
    if not target.is_file():
        raise FileNotFoundError(raw)
    if target.stat().st_size > DOC_LIMIT:
        raise ValueError('문서 크기는 1MiB 이하이어야 합니다')
    return target


def _docs(root: Path) -> list[dict]:
    import heapq
    rows = []
    for base, dirs, files in os.walk(root, followlinks=False):
        rel = Path(base).relative_to(root)
        dirs[:] = [d for d in dirs if not (Path(base)/d).is_symlink() and
                   (_doc_visible((rel/d/'file.md').parts) or (rel == Path('.') and d == '.duet'))]
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
        def read():
            path = _doc_path(ui.cfg.project, raw)
            with path.open('rb') as f:
                content = f.read(DOC_LIMIT + 1)
                mtime = os.fstat(f.fileno()).st_mtime
            if len(content) > DOC_LIMIT:
                raise ValueError('문서 크기 초과')
            return dict(path=raw, content=content.decode('utf-8'), mtime=mtime)
        try:
            return web.json_response(await asyncio.to_thread(read))
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
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
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
