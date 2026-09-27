"""단순 콘솔 모드 (--no-tui): 이벤트를 한 줄씩 출력하고 표준 입력으로 대화/승인."""
from __future__ import annotations

import asyncio
import sys

from . import commands
from .core.config import Config
from .core.events import Event, EventBus
from .core.orchestrator import Orchestrator
from .core.policy import ApprovalRequest, Decision

COLORS = {"human": "\033[97m", "error": "\033[91m", "notice": "\033[90m", "approval": "\033[93m"}
RESET = "\033[0m"


def fmt_event(ev: Event) -> str | None:
    d, r = ev.data, ev.role or "duet"
    k = ev.kind
    if k == "phase":
        return f"  · {d['task_id']}: {d['from'] or '위임'} → {d['to']}"
    if k == "human":
        return f"[사람 #{d['n']}]{' @' + d['to'] if d.get('to') else ''} {d['text']}"
    if k == "turn_start":
        return f"▶ [{r}] #{d['n']} 시작 ({d['kind']})"
    if k == "turn_end":
        dirs = " ".join(f"{a}{' ' + b[:40] if b else ''}" for a, b in d.get("directives") or [])
        return f"■ [{r}] #{d['n']} 끝 {('· ' + dirs) if dirs else ''}"
    if k == "text":
        return f"  [{r}] {d['text']}"
    if k == "tool":
        return f"  [{r}] ▸ {d['detail']}"
    if k == "tool_output":
        t = (d.get("text") or "").strip().splitlines()
        return f"  [{r}]   {'✓' if d.get('ok') else '✗'} {t[-1] if t else ''}"
    if k == "approval":
        if d["tier"] == "auto":
            return None
        return f"  [승인] {r}: {d['summary']} → {'허용' if d['allow'] else '거부'} ({d['by']}: {d['reason']})"
    if k in ("notice", "idle"):
        return f"  · {d['text']}"
    if k == "ask":
        return f"❓ [{r}] 질문: {d['text']}"
    if k == "error":
        return f"✗ [{r}] {d['text']}"
    return None


class ConsoleUI:
    def __init__(self):
        self.waiting: asyncio.Future | None = None
        self.lock = asyncio.Lock()

    async def _ask(self, prompt: str) -> str:
        async with self.lock:
            print(prompt, flush=True)
            self.waiting = asyncio.get_running_loop().create_future()
            try:
                return (await self.waiting).strip()
            finally:
                self.waiting = None

    async def ask_approval(self, req: ApprovalRequest, reason: str, opinion: str | None) -> Decision:
        text = (f"\n[승인 요청] {req.role}: {req.summary}\n  사유: {reason}"
                + (f"\n  설계자 의견: {opinion}" if opinion else "")
                + "\n  y/yes/allow=허용  a/s/session=세션 동안 허용  n=거부(이유 입력 가능: n 이유)")
        ans = (await self._ask(text)).strip()
        if ans.lower() in {"a", "s", "session"}:
            return Decision(True, "사람 허용(세션)", scope="session", by="human")
        if ans.lower() in {"y", "yes", "allow"}:
            return Decision(True, "사람 허용", by="human")
        reason = ans[1:].strip() if ans.lower() == "n" or ans.lower().startswith("n ") else ans
        return Decision(False, reason or "사람이 거부", by="human")

    async def ask_choice(self, title: str, body: str, options: list[tuple[str, str]]) -> str:
        opts = "  ".join(f"{i + 1}={label}" for i, (_, label) in enumerate(options))
        ans = await self._ask(f"\n[{title}]\n{body}\n  {opts}")
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1][0]
        for key, label in options:
            if ans in (key, label):
                return key
        return options[-1][0]


async def run_console(cfg: Config, bus: EventBus, msgs: list[str], fake: bool, first: str | None) -> None:
    ui = ConsoleUI()
    orch = Orchestrator(cfg, bus, ui, fake=fake)
    bus.subscribe(lambda ev: (lambda s: s and print(s, flush=True))(fmt_event(ev)))
    for m in msgs:
        print("· " + m)
    print("duet 콘솔 모드 — /help 로 명령어 보기")
    server = asyncio.create_task(orch.serve())
    if first:
        orch.submit(first)
    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                if ui.waiting is None and not orch.running and orch.inbox.empty():
                    break
                await asyncio.sleep(0.2)
                continue
            line = line.rstrip("\n")
            if ui.waiting is not None and not ui.waiting.done():
                ui.waiting.set_result(line)
                continue
            if line.strip() in ("/quit", "/exit"):
                break
            out = await commands.handle(orch, line)
            if out:
                print(out, flush=True)
    finally:
        await orch.stop()
        server.cancel()
        await orch.close()
