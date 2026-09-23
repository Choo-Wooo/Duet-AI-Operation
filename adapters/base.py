"""CLI 어댑터 공통 인터페이스."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from ..core.config import Role
from ..core.events import EventBus
from ..core.policy import ApprovalRequest, Decision

Approver = Callable[[ApprovalRequest], Awaitable[Decision]]


@dataclass
class TurnResult:
    text: str
    ok: bool = True
    error: str | None = None
    cost_usd: float | None = None
    tokens: int | None = None
    interrupted: bool = False
    full_text: str | None = None


class AgentAdapter(ABC):
    """하나의 역할 = 하나의 CLI 세션."""

    def __init__(self, role: Role, project: Path, bus: EventBus, approver: Approver | None,
                 session_id: str | None, system_append: str, reviewer: bool = False,
                 fork_session: bool = False):
        self.role = role
        self.project = project
        self.bus = bus
        self.approver = approver
        self.session_id = session_id
        self.system_append = system_append
        self.reviewer = reviewer  # 심사 전용(읽기 전용) 보조 세션
        self.busy = False
        self.fork_session = fork_session
        self.restore_failed = False
        self.plan_read_only = False
        self.agreement_phase: str | None = None
        self.verification_command: str | None = None
        self.turn_kind: str | None = None

    def recovery_prompt(self, prompt: str) -> str:
        if self.restore_failed:
            self.restore_failed = False
            return "세션 복원 실패: DIALOGUE.md 전체를 읽고 맥락을 복구하라.\n\n" + prompt
        return prompt

    @property
    def label(self) -> str:
        return self.role.name + (" (심사)" if self.reviewer else "")

    def emit(self, kind: str, /, **data) -> None:
        if self.reviewer and kind in ("tool", "tool_output"):
            return  # 심사 세션의 도구 호출은 화면에 띄우지 않는다
        self.bus.emit(kind, self.label, **data)

    async def approve(self, req: ApprovalRequest) -> Decision:
        if self.reviewer:
            return Decision(False, "심사 세션은 읽기 전용입니다.", by="policy")
        if self.approver is None:
            return Decision(False, "승인 처리기가 없습니다.", by="policy")
        return await self.approver(req)

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def run_turn(self, prompt: str) -> TurnResult: ...

    @abstractmethod
    async def interrupt(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


def clip(text: str, n: int = 1200) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    return text[: n // 2] + f"\n… ({len(text) - n}자 생략) …\n" + text[-n // 2:]
