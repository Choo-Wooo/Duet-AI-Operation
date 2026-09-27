"""승인·선택 팝업."""
from __future__ import annotations

from time import monotonic

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Static

from ..core.policy import ApprovalRequest, Decision

MODAL_CSS = """
ApprovalScreen, ChoiceScreen { align: center middle; background: $background 60%; }
#box { width: 90; max-width: 95%; height: auto; border: thick $warning; background: $panel; padding: 1 2; }
#box.choice { border: thick $accent; }
#title { text-style: bold; margin-bottom: 1; }
#body { margin-bottom: 1; }
#buttons { height: auto; margin-top: 1; }
#buttons Button { margin-right: 1; }
#reason { margin-top: 1; width: 100%; }
"""


class ApprovalScreen(ModalScreen[Decision]):
    DEFAULT_CSS = MODAL_CSS
    BINDINGS = [
        Binding("y", "allow", "허용"),
        Binding("n", "deny", "거부"),
    ]

    def __init__(self, req: ApprovalRequest, reason: str, opinion: str | None):
        super().__init__()
        self.req, self.reason, self.opinion = req, reason, opinion
        self._keys_ready_at = float("inf")

    def compose(self) -> ComposeResult:
        body = Text()
        body.append("요청 역할  ", style="dim")
        body.append(self.req.role + "\n", style="bold")
        body.append("요청 내용  ", style="dim")
        body.append(self.req.summary + "\n", style="bold yellow")
        body.append("분류 사유  ", style="dim")
        body.append(self.reason + "\n")
        if self.req.detail.get("reason"):
            body.append("에이전트 설명  ", style="dim")
            body.append(str(self.req.detail["reason"]) + "\n")
        if self.opinion:
            body.append("설계자 의견  ", style="dim")
            body.append(self.opinion + "\n", style="cyan")
        with Vertical(id="box"):
            yield Static(Text("⚖  승인 요청", style="bold"), id="title")
            yield Static(body, id="body")
            with Horizontal(id="buttons"):
                yield Button("Y 허용", id="allow", variant="success")
                yield Button("세션 동안 허용", id="allow_session", variant="primary")
                yield Button("N 거부", id="deny", variant="error")
            yield Input(placeholder="거부 이유(선택) — 입력 후 Enter 면 거부", id="reason")

    def on_mount(self) -> None:
        self._keys_ready_at = monotonic() + 0.5
        self.query_one("#deny", Button).focus()

    async def _on_key(self, event) -> None:
        if monotonic() < self._keys_ready_at:
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        getattr(self, "action_" + (event.button.id or "deny"))()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.action_deny()

    def action_allow(self) -> None:
        if monotonic() < self._keys_ready_at:
            return
        self.dismiss(Decision(True, "사람 허용", by="human"))

    def action_allow_session(self) -> None:
        if monotonic() < self._keys_ready_at:
            return
        self.dismiss(Decision(True, "사람 허용(세션)", scope="session", by="human"))

    def action_deny(self) -> None:
        if monotonic() < self._keys_ready_at:
            return
        why = self.query_one("#reason", Input).value.strip()
        self.dismiss(Decision(False, why or "사람이 거부", by="human"))

    def check_action(self, action: str, parameters) -> bool | None:
        if monotonic() < self._keys_ready_at:
            return False
        # 거부 이유 입력 중에는 y/a/n 단축키를 끈다
        if action in ("allow", "allow_session", "deny") and isinstance(self.focused, Input):
            return False
        return True


class ChoiceScreen(ModalScreen[str]):
    DEFAULT_CSS = MODAL_CSS

    def __init__(self, title: str, body: str, options: list[tuple[str, str]]):
        super().__init__()
        self.t, self.b, self.options = title, body, options

    def compose(self) -> ComposeResult:
        with Vertical(id="box", classes="choice"):
            yield Static(Text(self.t, style="bold"), id="title")
            yield Static(Text(self.b), id="body")
            with Horizontal(id="buttons"):
                for i, (key, label) in enumerate(self.options):
                    yield Button(f"{i + 1} {label}", id=f"opt{i}", variant="primary" if i == 0 else "default")

    def on_mount(self) -> None:
        self.query_one("#opt0", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        idx = int((event.button.id or "opt0")[3:])
        self.dismiss(self.options[idx][0])

    def on_key(self, event) -> None:
        if event.character and event.character.isdigit():
            i = int(event.character) - 1
            if 0 <= i < len(self.options):
                event.stop()
                self.dismiss(self.options[i][0])
