"""오케스트레이터가 사람에게 묻는 창구 (TUI / 콘솔이 구현)."""
from __future__ import annotations

from typing import Protocol

from .policy import ApprovalRequest, Decision


class HumanUI(Protocol):
    async def ask_approval(self, req: ApprovalRequest, reason: str, opinion: str | None) -> Decision: ...

    async def ask_choice(self, title: str, body: str, options: list[tuple[str, str]]) -> str: ...
