"""Textual 분할 TUI: 대화 흐름 | 역할별 실시간 출력 | 상태 줄 | 입력창."""
from __future__ import annotations

import asyncio
import re

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Input, RichLog, Static, TabbedContent, TabPane

from .. import commands
from ..core.config import Config
from ..core.events import Event, EventBus
from ..core.orchestrator import Orchestrator
from ..core.policy import ApprovalRequest, Decision
from .screens import ApprovalScreen, ChoiceScreen

PALETTE = ["#7aa2f7", "#9ece6a", "#e0af68", "#bb9af7", "#7dcfff", "#f7768e", "#73daca"]


def _safe_id(name: str) -> str:
    return "r-" + re.sub(r"[^a-zA-Z0-9_-]", "_", name)


class DuetApp(App):
    TITLE = "duet"
    CSS = """
    Screen { layout: vertical; }
    #main { height: 1fr; }
    #dialogue { width: 45%; border: round $primary; border-title-color: $primary; padding: 0 1; }
    #tabs { width: 55%; }
    TabPane { padding: 0; }
    TabPane RichLog { border: round $secondary; padding: 0 1; }
    #status { height: 1; background: $boost; color: $text; padding: 0 1; }
    #input { dock: bottom; }
    """
    BINDINGS = [
        Binding("ctrl+x", "stop_turn", "현재 턴 중단"),
        Binding("ctrl+q", "quit_app", "종료", priority=True),
        Binding("escape", "focus_input", "입력창", show=False),
    ]

    def __init__(self, cfg: Config, bus: EventBus, msgs: list[str], fake: bool, first: str | None):
        super().__init__()
        self.cfg, self.bus, self.msgs, self.fake, self.first = cfg, bus, msgs, fake, first
        self.orch = Orchestrator(cfg, bus, self, fake=fake)
        self.role_colors: dict[str, str] = {}
        self.status_data: dict = {}
        self._mounted = False

    # ---------- 화면 ----------
    def compose(self) -> ComposeResult:
        with Horizontal(id="main"):
            d = RichLog(id="dialogue", wrap=True, markup=False, highlight=False, auto_scroll=True)
            d.border_title = "대화 흐름 · DIALOGUE.md"
            yield d
            with TabbedContent(id="tabs"):
                for name in self.cfg.roles:
                    yield self._role_pane(name)
        yield Static(id="status")
        yield Input(placeholder="설계자에게 메시지, 또는 /help", id="input")

    def _role_pane(self, name: str) -> TabPane:
        log = RichLog(wrap=True, markup=False, highlight=False, auto_scroll=True, id=_safe_id(name) + "-log")
        r = self.cfg.roles[name]
        log.border_title = f"{name} · {r.cli}/{r.model or '기본'}"
        return TabPane(name, log, id=_safe_id(name))

    def color(self, role: str | None) -> str:
        if not role:
            return "white"
        base = role.split(" ")[0].split("#")[0]
        if base not in self.role_colors:
            self.role_colors[base] = PALETTE[len(self.role_colors) % len(PALETTE)]
        return self.role_colors[base]

    def on_mount(self) -> None:
        self._mounted = True
        for name in self.cfg.roles:
            self.color(name)
        self.bus.subscribe(self.on_bus_event)
        dlg = self.query_one("#dialogue", RichLog)
        dlg.write(Text("duet — 설계자와 대화하면 역할들이 DIALOGUE.md 로 협업합니다. /help", style="bold"))
        for m in self.msgs:
            dlg.write(Text("· " + m, style="dim"))
        mt = self.cfg.max_turns
        dlg.write(Text(f"· 모드 {self.cfg.state.mode}, 턴 한도 {'∞' if mt is None else mt}"
                       f"{' (가짜 에이전트)' if self.fake else ''}", style="dim"))
        self.query_one("#input", Input).focus()
        self.run_worker(self.orch.serve(), group="orch", exclusive=False)
        self.set_interval(1.0, self._tick)
        if self.first:
            self.orch.submit(self.first)
        self.orch.emit_status()

    # ---------- 이벤트 → 화면 ----------
    def role_log(self, role: str | None) -> RichLog | None:
        if not role:
            return None
        base = role.split(" ")[0].split("#")[0]
        try:
            return self.query_one("#" + _safe_id(base) + "-log", RichLog)
        except Exception:
            return None

    def on_bus_event(self, ev: Event) -> None:
        if not self._mounted:
            return
        try:
            self._render(ev)
        except Exception:
            pass

    def _render(self, ev: Event) -> None:
        dlg = self.query_one("#dialogue", RichLog)
        d, role, k = ev.data, ev.role, ev.kind
        c = self.color(role)
        rl = self.role_log(role)
        tag = f"[{role.split('#', 1)[1]}] " if role and "#" in role else ""
        if tag and k in ("text", "tool", "tool_output") and rl:  # 병렬 작업 세션: 역할 탭에 작업 id 를 붙여 표시
            body = d.get("text") if k != "tool" else "▸ " + d["detail"]
            if k == "tool_output":
                body = ("  ✓ " if d.get("ok") else "  ✗ ") + (d.get("text") or "").strip()[:600]
            rl.write(Text(tag + (body or ""), style=c if k == "text" else "dim"))
            return
        if tag and k in ("turn_start", "turn_end"):
            if rl and k == "turn_start":
                rl.write(Text(f"\n━━ {tag}{d['kind']} ━━", style=f"bold {c}"))
            return
        if k == "work":
            style = "yellow" if d["status"].startswith("waiting") or d["status"] == "failed" else "cyan"
            dlg.write(Text(f"· 작업 {d['id']} → {d['status']}" + (f" ({d['reason']})" if d.get("reason") else ""),
                           style=style))
            return
        if k == "human":
            t = Text()
            t.append(f"\n[사람 #{d['n']}] ", style="bold white on #3b4261")
            if d.get("to"):
                t.append(f"@{d['to']} ", style="bold")
            t.append(d["text"])
            dlg.write(t)
        elif k == "turn_start":
            info = d.get("info") or ""
            label = {"human": "사람 메시지", "delegate": "위임", "report": "보고 검토", "checkpoint": "체크포인트",
                     "system": "안내"}.get(d["kind"], d["kind"])
            t = Text(f"▶ {role} #{d['n']} · {label}", style=f"bold {c}")
            if d["kind"] == "delegate" and info:
                t.append("\n   " + info[:200], style=c)
            dlg.write(t)
            if rl:
                rl.write(Text(f"\n━━ #{d['n']} {label} ━━", style=f"bold {c}"))
            self._select_tab(role)
        elif k == "turn_end":
            t = Text(f"■ {role} #{d['n']}", style=f"bold {c}")
            if d.get("interrupted"):
                t.append(" (중단됨)", style="yellow")
            summary = d.get("summary") or ""
            if summary:
                t.append("\n" + "\n".join("   " + ln for ln in summary.splitlines()))
            for name, arg in d.get("directives") or []:
                t.append(f"\n   ⤷ {name} {arg[:120]}", style="yellow")
            dlg.write(t)
        elif k == "text":
            if rl:
                rl.write(Text(d["text"]))
        elif k == "tool":
            if rl:
                rl.write(Text("▸ " + d["detail"], style="cyan"))
        elif k == "tool_output":
            if rl:
                mark = "✓" if d.get("ok") else "✗"
                rl.write(Text(f"  {mark} " + (d.get("text") or "").strip()[:2000],
                              style="dim" if d.get("ok") else "red"))
        elif k == "approval":
            verdict = "허용" if d["allow"] else "거부"
            line = f"⚖ {d['summary'][:140]} → {verdict} ({d['by']}: {d['reason']})"
            if rl:
                rl.write(Text(line, style="dim green" if d["allow"] else "dim red"))
            if d["tier"] != "auto":
                dlg.write(Text(f"   ⚖ {role}: {d['summary'][:100]} → {verdict} ({d['by']})",
                               style="green" if d["allow"] else "red"))
        elif k == "ask":
            dlg.write(Text(f"❓ {role}: {d['text']}", style="bold red"))
            self.bell()
        elif k == "memory":
            if rl:
                rl.write(Text(f"· 작업 기억 갱신 ({d.get('chars', 0):,}자) → .duet/memory/{role}.md", style="dim"))
        elif k == "notice":
            style = "yellow" if d.get("level") == "warn" else "dim"
            (rl if (rl and role) else dlg).write(Text("· " + d["text"], style=style))
        elif k == "error":
            dlg.write(Text("✗ " + (f"{role}: " if role else "") + d["text"], style="bold red"))
        elif k == "idle":
            dlg.write(Text("— 사람 차례입니다 —", style="bold green"))
            self.bell()
        elif k == "roles":
            self._sync_tabs(d.get("roles") or [])
        elif k == "status":
            self.status_data = d
            self._draw_status()
        elif k == "phase":
            dlg.write(Text(f"· {d['task_id']}: {d['from'] or '위임'} → {d['to']}", style="cyan"))

    def _select_tab(self, role: str) -> None:
        try:
            self.query_one("#tabs", TabbedContent).active = _safe_id(role.split(" ")[0])
        except Exception:
            pass

    def _sync_tabs(self, roles: list[str]) -> None:
        tabs = self.query_one("#tabs", TabbedContent)
        existing = {p.id for p in tabs.query(TabPane)}
        for name in roles:
            if _safe_id(name) not in existing and name in self.cfg.roles:
                self.color(name)
                tabs.add_pane(self._role_pane(name))
        for pid in existing:
            if pid not in {_safe_id(n) for n in roles}:
                tabs.remove_pane(pid)

    def _tick(self) -> None:
        if self.status_data:
            self.status_data["elapsed"] = self.status_data.get("elapsed", 0) + 1
            self._draw_status()

    def _draw_status(self) -> None:
        s = self.status_data
        mt = "∞" if s.get("max_turns") is None else s.get("max_turns")
        el = int(s.get("elapsed", 0))
        running = s.get("running")
        t = Text()
        if s.get("paused"):
            t.append("⏸ 일시정지  ", style="bold yellow")
        if running:
            t.append(f"● {running} 실행 중", style=f"bold {self.color(running)}")
        elif s.get("busy"):
            t.append("● 진행 중", style="bold")
        else:
            t.append("○ 대기", style="dim")
        t.append(f" · 턴 {s.get('run_turns', 0)}/{mt} · 모드 {s.get('mode')}"
                 f" · 자동위임 {'on' if s.get('auto') else 'off'}")
        for name, (size, limit) in (s.get("contexts") or {}).items():
            if size:
                hot = bool(limit) and size > limit * 0.8
                t.append(f" · {name} {size // 1000}k" + (f"/{limit // 1000}k" if limit else ""),
                         style="bold yellow" if hot else "dim")
        pa = s.get("pending_approvals", 0)
        t.append(f" · 승인 대기 {pa}", style="bold red" if pa else "")
        t.append(f" · API환산 ${s.get('cost_usd', 0):.2f} · {s.get('tokens', 0):,} tok · {el // 60}m{el % 60:02d}s")
        t.append("   ^X 중단 ^Q 종료", style="dim")
        if s.get("task"):
            t.append(" · " + s["task_summary"], style="cyan")
        self.query_one("#status", Static).update(t)

    # ---------- 입력 ----------
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "input":
            return
        line = event.value
        event.input.value = ""
        if line.strip() in ("/quit", "/exit"):
            await self.action_quit_app()
            return
        self.run_worker(self._handle(line), group="cmd", exclusive=False)

    async def _handle(self, line: str) -> None:
        try:
            out = await commands.handle(self.orch, line)
        except Exception as e:
            out = f"명령 처리 오류: {e}"
        if out:
            self.query_one("#dialogue", RichLog).write(Text(out, style="italic #a9b1d6"))

    def action_focus_input(self) -> None:
        self.query_one("#input", Input).focus()

    async def action_stop_turn(self) -> None:
        await self.orch.stop()

    async def action_quit_app(self) -> None:
        self.query_one("#dialogue", RichLog).write(Text("종료 중…", style="dim"))
        try:
            await asyncio.wait_for(self.orch.stop(), 5)
            await asyncio.wait_for(self.orch.close(), 10)
        except Exception:
            pass
        self.exit()

    # ---------- HumanUI 구현 ----------
    async def ask_approval(self, req: ApprovalRequest, reason: str, opinion: str | None) -> Decision:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.bell()
        self.push_screen(ApprovalScreen(req, reason, opinion),
                         lambda r: fut.done() or fut.set_result(r or Decision(False, "사람이 거부", by="human")))
        return await fut

    async def ask_choice(self, title: str, body: str, options: list[tuple[str, str]]) -> str:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.bell()
        self.push_screen(ChoiceScreen(title, body, options),
                         lambda r: fut.done() or fut.set_result(r or options[-1][0]))
        return await fut
