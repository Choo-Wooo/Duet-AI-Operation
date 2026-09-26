from __future__ import annotations

from pathlib import Path

from ..core.config import Role
from ..core.events import EventBus
from .base import AgentAdapter, Approver, TurnResult


def make_adapter(role: Role, project: Path, bus: EventBus, approver: Approver | None, session_id: str | None,
                 system_append: str, reviewer: bool = False, fake: bool = False,
                 fork_session: bool = False) -> AgentAdapter:
    if fake:
        from .fake import FakeAdapter
        return FakeAdapter(role, project, bus, approver, session_id, system_append, reviewer, fork_session)
    if role.cli == "claude":
        from .claude import ClaudeAdapter
        return ClaudeAdapter(role, project, bus, approver, session_id, system_append, reviewer, fork_session)
    if role.cli == "codex":
        from .codex import CodexAdapter
        return CodexAdapter(role, project, bus, approver, session_id, system_append, reviewer, fork_session)
    if role.cli == "agy":
        from .agy import AgyAdapter
        return AgyAdapter(role, project, bus, approver, session_id, system_append, reviewer, fork_session)
    raise ValueError(f"지원하지 않는 CLI: {role.cli}")


__all__ = ["AgentAdapter", "TurnResult", "make_adapter"]
