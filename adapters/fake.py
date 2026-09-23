"""가짜 어댑터 — CLI 없이 흐름·TUI·정책을 시험할 때 사용 (python3 duet --fake)."""
from __future__ import annotations

import asyncio
import os
import re
from uuid import uuid4

from ..core.dialogue import Dialogue
from ..core.policy import ApprovalRequest
from .base import AgentAdapter, TurnResult

DELAY = float(os.environ.get("DUET_FAKE_DELAY", "0.4"))


class FakeAdapter(AgentAdapter):
    async def start(self) -> None:
        if self.session_id and self.fork_session:
            self.session_id = f"fake-{self.role.name}-{uuid4().hex}"
        self.fork_session = False
        self.session_id = self.session_id or f"fake-{self.role.name}{'-rev' if self.reviewer else ''}"
        self._stop = False
        self.dialogue = Dialogue(self.project)

    async def _step(self, kind: str, **data) -> None:
        await asyncio.sleep(DELAY)
        self.emit(kind, **data)

    async def run_turn(self, prompt: str) -> TurnResult:
        self.busy = True
        self._stop = False
        try:
            if self.reviewer:
                await asyncio.sleep(DELAY)
                if "rm -rf" in prompt:
                    return TurnResult('{"decision": "deny", "reason": "삭제 대신 build 폴더를 무시하세요."}')
                return TurnResult('{"decision": "allow", "reason": "테스트용 허용", "scope": "once"}')
            m = re.search(r"턴 #(\d+)", prompt)
            n = int(m.group(1)) if m else self.dialogue.max_number() + 1
            if self.plan_read_only:
                await self._step("tool", name="Read", detail="Read DIALOGUE.md")
                body = ("### 이해한 요구\nhello 함수를 추가합니다.\n### 설계와 다른 점\n없음\n"
                        "```files\nsrc/app.py\n```\n### 수용 기준\nAC1: hello가 hi를 반환한다.\n"
                        "### 테스트 계획\nAC1의 반환값은 실제 실행하고 에이전트 응답만 모의한다.\n"
                        "test_command: python -m pytest\n### 열린 질문\n없음\n<!-- duet: PLAN ready -->")
                self.emit("text", text=body)
                return TurnResult(body, full_text=body, tokens=1234)
            if self.turn_kind == "plan_review":
                version = re.search(r"계획 v(\d+)", prompt)
                body = f"계획과 테스트 방법을 확인했습니다.\n<!-- duet: AGREE v{version.group(1) if version else '1'} -->"
            elif self.turn_kind == "verify":
                body = "가짜 흐름 검토 완료(실제 테스트 실행 아님).\n<!-- duet: ACCEPT -->\n<!-- duet: STATUS done -->"
            elif "체크포인트" in prompt:
                body = "체크포인트 요약: 결정 사항 없음(가짜 어댑터)."
            elif self.role.permissions == "read_only":
                body = await self._main_turn(prompt)
            else:
                body = await self._worker_turn()
            if self._stop:
                return TurnResult("", interrupted=True)
            self.dialogue.append_turn(self.role.name, n, body)
            self.emit("text", text=body.split("<!--")[0].strip())
            return TurnResult(body, full_text=body, tokens=1234)
        finally:
            self.busy = False

    async def _main_turn(self, prompt: str) -> str:
        await self._step("tool", name="Read", detail="Read DIALOGUE.md")
        if "보고(" in prompt:
            return "구현 보고를 확인했습니다. 요청이 완료되었습니다.\n<!-- duet: STATUS done -->"
        if "사람의 새 메시지" in prompt:
            await self._step("tool", name="Write", detail="Write docs/design.md")
            return ("요청을 설계했습니다(docs/design.md). 구현을 맡깁니다.\n"
                    "<!-- duet: DELEGATE implementer -->\n"
                    "<!-- duet: TASK src/app.py 에 hello() 함수를 만들고 테스트를 추가하라 -->")
        return "확인했습니다."

    async def _worker_turn(self) -> str:
        await self._step("tool", name="Read", detail="Read docs/design.md")
        for req in (
            ApprovalRequest(self.role.name, "command", "$ pip install requests", command="pip install requests"),
            ApprovalRequest(self.role.name, "command", "$ rm -rf build", command="rm -rf build"),
        ):
            if self._stop:
                return ""
            self.emit("tool", name="shell", detail=req.summary)
            d = await self.approve(req)
            self.emit("tool_output", text=("실행됨" if d.allow else f"거부됨: {d.reason}"), ok=d.allow)
        path = self.project / "src" / "app.py"
        req = ApprovalRequest(self.role.name, "file", "Write src/app.py", paths=[str(path)])
        d = await self.approve(req)
        if d.allow:
            path.parent.mkdir(exist_ok=True)
            n = len(path.read_text().splitlines()) if path.exists() else 0
            path.write_text((path.read_text() if path.exists() else "") + f"def hello_{n}():\n    return 'hi'\n")
            await self._step("tool_output", text="src/app.py 수정", ok=True)
        await self._step("tool", name="shell", detail="$ pytest -q")
        await self._step("tool_output", text="1 passed", ok=True)
        return "src/app.py 에 hello() 를 추가했고 테스트가 통과했습니다.\n<!-- duet: REPORT done -->"

    async def interrupt(self) -> None:
        self._stop = True

    async def close(self) -> None:
        pass
