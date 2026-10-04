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
    context_tokens: int | None = None  # 이번 턴 마지막 요청의 입력(컨텍스트) 크기
    context_overflow: bool = False  # 모델 입력 한도 초과로 실패했는지
    usage_limited: bool = False
    usage_reset_at: float | None = None


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
        self.context_tokens = 0  # 마지막으로 관측한 세션 컨텍스트 크기(토큰)
        self.handoff: str | None = None  # 세션 교체 직후 첫 턴에 붙일 안내
        self.context_limit = 0  # 0 이 아니면 CLI 자체 자동 압축 기준으로도 전달 (Codex)
        self.compact_timeout = 300.0  # 압축이 이 시간(초) 안에 끝나지 않으면 실패로 보고 진행을 풀어 준다

    def recovery_prompt(self, prompt: str) -> str:
        if self.handoff:
            note, self.handoff = self.handoff, None
            self.restore_failed = False
            return note + "\n\n" + prompt
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

    async def compact(self, instructions: str = "") -> bool:
        """CLI 고유의 대화 압축을 실행한다. 지원하지 않거나 실패하면 False."""
        return False

    @abstractmethod
    async def interrupt(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...


def clip(text: str, n: int = 1200) -> str:
    text = text or ""
    if len(text) <= n:
        return text
    return text[: n // 2] + f"\n… ({len(text) - n}자 생략) …\n" + text[-n // 2:]


_OVERFLOW_PATTERNS = (
    "prompt is too long", "context length", "context_length", "contextwindowexceeded", "context window",
    "maximum context", "too many tokens", "input is too long", "exceeds the maximum", "request too large",
)


def is_context_overflow(*texts: str | None) -> bool:
    joined = " ".join(t for t in texts if t).lower()
    return any(p in joined for p in _OVERFLOW_PATTERNS)
