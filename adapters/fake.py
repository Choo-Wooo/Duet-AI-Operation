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
            if (self.turn_kind or "").startswith("work_"):
                body = await self._work_turn(prompt)
                self.emit("text", text=body.split("<!--")[0].strip())
                return TurnResult(body, full_text=body, tokens=321)
            m = re.search(r"턴 #(\d+)", prompt)
            n = int(m.group(1)) if m else self.dialogue.max_number() + 1
            if self.role.name.startswith("질문:"):  # 웹 질문 패널
                await self._step("tool", name="Read", detail="Read docs/design")
                q = prompt.split("질문:")[-1].strip()
                body = (f"(가짜 에이전트 답변) '{q[:80]}' 에 대한 답입니다. 실제 실행에서는 역할 세션을 복제한 읽기 전용 "
                        "분신이 설계 문서와 코드를 읽고 근거와 함께 답합니다.")
                return TurnResult(body, full_text=body)
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
        if "사람의 새 메시지" in prompt and "병렬" in self._last_human():
            items = ["- id: api\n  role: implementer\n  task: api.txt 를 만든다\n",
                     "- id: ui\n  role: implementer\n  task: ui.txt 를 만든다\n  depends_on: [api]\n"]
            if "- designer" in prompt:  # 데모: 팀에 디자이너·리서처가 있으면 함께 나눈다
                items[1] = "- id: ui\n  role: designer\n  task: ui.txt 에 화면 시안을 만든다\n  depends_on: [api]\n"
                items.append("- id: db\n  role: implementer\n  task: db.txt 를 만든다\n")
            if "- researcher" in prompt:
                items.append("- id: survey\n  role: researcher\n  task: survey.txt 에 조사 결과를 쓴다\n")
            return (f"작업을 {len(items)}개로 나눠 동시에 맡깁니다.\n```duet-work\n" + "".join(items) + "```")
        if "병렬 작업 보고" in prompt:
            return "병렬 작업 결과를 확인했습니다.\n<!-- duet: STATUS done -->"
        if "사람의 새 메시지" in prompt:
            await self._step("tool", name="Write", detail="Write docs/design.md")
            return ("요청을 설계했습니다(docs/design.md). 구현을 맡깁니다.\n"
                    "<!-- duet: DELEGATE implementer -->\n"
                    "<!-- duet: TASK src/app.py 에 hello() 함수를 만들고 테스트를 추가하라 -->")
        return "확인했습니다."

    def _last_human(self) -> str:
        turns = [t for t in self.dialogue.turns() if t.role == "human"] if hasattr(self.dialogue, "turns") else []
        if turns:
            return turns[-1].body
        text = self.dialogue.read() if hasattr(self.dialogue, "read") else ""
        return text[-400:]

    async def _work_turn(self, prompt: str) -> str:
        """병렬 작업 흐름용 응답 (계획·검토·구현·검증·협의)."""
        await asyncio.sleep(float(os.environ.get("DUET_FAKE_WORK_DELAY", DELAY / 4)))
        kind = self.turn_kind
        wid = re.search(r"병렬 작업 ([a-z0-9-]+)", prompt)
        wid = wid.group(1) if wid else "work"
        # 읽기 전용 역할(리서처)은 docs/ 아래에만 쓴다
        rel = f"docs/research/{wid}.md" if self.role.permissions == "read_only" else f"{wid}.txt"
        if kind == "work_plan":
            return (f"### 이해한 요구\n{rel} 를 만든다\n```files\n{rel}\n```\n### AC\nAC1 파일 존재\n"
                    f"test_command: test -f {rel}\n<!-- duet: PLAN ready -->")
        if kind == "work_review":
            return "계획이 적절합니다.\n<!-- duet: AGREE -->"
        if kind == "work_implement":
            if "CONSULT 답변" not in prompt and "consult" in wid:
                return "인터페이스를 확인하겠습니다.\n<!-- duet: CONSULT architect 파일 이름 규칙은? -->"
            path = self.project / rel
            req = ApprovalRequest(self.role.name, "file", f"Write {rel}", paths=[str(path)])
            d = await self.approve(req)
            if d.allow:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{wid}\n")
            return f"{rel} 를 만들었습니다.\n<!-- duet: REPORT done -->"
        if kind == "work_verify":
            return "검토 완료.\n<!-- duet: ACCEPT -->"
        return "파일 이름은 작업 id 를 씁니다."

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
