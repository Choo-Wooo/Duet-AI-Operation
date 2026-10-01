"""Claude Code 어댑터 — claude-agent-sdk (설치된 claude CLI 를 그대로 구동)."""
from __future__ import annotations

import json
import asyncio
import inspect
from collections import deque

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from ..core.clis import which
from ..core.policy import ApprovalRequest, Decision
from ..core.policy import PLAN_DENY_TEXT, PLAN_TOOLS, NETWORK_TOOLS, read_only_command, tool_matches
from ..core.prompts import REVIEW_SYSTEM
from .base import AgentAdapter, TurnResult, clip, is_context_overflow
from ..core.usage_wait import classify


def _context_size(usage) -> int:
    """한 번의 API 요청에 들어간 입력 크기 = 새 입력 + 캐시 읽기 + 캐시 쓰기."""
    if not isinstance(usage, dict):
        return 0
    return sum(int(usage.get(k) or 0) for k in
               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))

FILE_TOOLS = {"Edit": "file_path", "Write": "file_path", "MultiEdit": "file_path", "NotebookEdit": "notebook_path"}
READONLY_TOOLS = ["Read", "Grep", "Glob", "LS"]


def _tool_detail(name: str, inp: dict) -> str:
    if name == "Bash":
        return "$ " + str(inp.get("command", ""))
    if name in FILE_TOOLS:
        return f"{name} {inp.get(FILE_TOOLS[name], '')}"
    if name in ("Read", "LS"):
        return f"{name} {inp.get('file_path') or inp.get('path', '')}"
    if name in ("Grep", "Glob"):
        return f"{name} {inp.get('pattern', '')}"
    if name in ("WebFetch", "WebSearch"):
        return f"{name} {inp.get('url') or inp.get('query', '')}"
    s = json.dumps(inp, ensure_ascii=False)
    return f"{name} {s[:160]}"


def _result_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for c in content:
        if isinstance(c, dict) and c.get("type") == "text":
            parts.append(c.get("text", ""))
    return "\n".join(parts)


class ClaudeAdapter(AgentAdapter):
    client: ClaudeSDKClient | None = None
    approval_timeout = 3600.0

    async def _bounded_approve(self, req):
        try:
            return await asyncio.wait_for(self.approve(req), self.approval_timeout)
        except asyncio.TimeoutError:
            reason = '사람 승인 대기 중 타임아웃'
            self.emit('notice', text=reason, level='warn')
            return Decision(False, reason, by='timeout')

    def _options(self, resume: str | None) -> ClaudeAgentOptions:
        append = REVIEW_SYSTEM if self.reviewer else self.system_append
        kw = dict(
            cwd=str(self.project),
            system_prompt={"type": "preset", "preset": "claude_code", "append": append},
            setting_sources=["user", "project", "local"],  # CLAUDE.md, settings, MCP, 스킬 그대로
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            resume=resume,
            stderr=self._on_stderr,
            hooks={"PreToolUse": [HookMatcher(hooks=[self._pre_tool_use], timeout=self.approval_timeout + 30)]},
        )
        if resume and self.fork_session:
            if "fork_session" in inspect.signature(ClaudeAgentOptions).parameters:
                kw["fork_session"] = True
            else:
                self.emit("notice", text="Claude SDK가 세션 분기를 지원하지 않아 resume합니다. "
                          "저장 시점 이후 기억이 포함될 수 있습니다.", level="warn")
                self.fork_session = False
        cli = which("claude")
        if cli:
            kw["cli_path"] = cli  # 설치된 claude 중 가장 최신 버전 (설정·로그인은 ~/.claude 그대로)
        if self.role.model:
            kw["model"] = self.role.model
        if self.role.effort:
            kw["effort"] = self.role.effort
        if self.reviewer:
            kw["allowed_tools"] = READONLY_TOOLS
        elif self.role.mcp:  # 역할 전용 MCP (예: 디자이너의 브라우저 도구). 사용자 MCP 설정에 더해진다
            kw["mcp_servers"] = dict(self.role.mcp)
        return ClaudeAgentOptions(**kw)

    def _on_stderr(self, line: str) -> None:
        self._stderr.append(line)

    async def start(self) -> None:
        self._stderr: deque[str] = deque(maxlen=30)
        try:
            self.client = ClaudeSDKClient(self._options(self.session_id))
            await self.client.connect()
        except Exception:
            if not self.session_id:
                raise
            # 저장된 세션을 이어갈 수 없으면 새 세션으로
            self.emit("notice", text="이전 Claude 세션을 이어갈 수 없어 새 세션을 시작합니다.", level="warn")
            await self.close()
            self.restore_failed = True
            self.fork_session = False
            self.session_id = None
            self.client = ClaudeSDKClient(self._options(None))
            await self.client.connect()

    async def _can_use_tool(self, tool_name: str, tool_input: dict, context):
        if tool_name == "Bash":
            req = ApprovalRequest(self.role.name, "command", "$ " + str(tool_input.get("command", "")),
                                  command=str(tool_input.get("command", "")), tool=tool_name)
        elif tool_name in FILE_TOOLS:
            path = str(tool_input.get(FILE_TOOLS[tool_name], ""))
            req = ApprovalRequest(self.role.name, "file", f"{tool_name} {path}", paths=[path], tool=tool_name)
        else:
            req = ApprovalRequest(self.role.name, "tool", _tool_detail(tool_name, tool_input), tool=tool_name,
                                  detail={"input": tool_input})
        decision = await self._bounded_approve(req)
        if decision.allow:
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(message=decision.reason or "거부됨", interrupt=decision.interrupt)

    async def _pre_tool_use(self, data, tool_use_id, context):
        name, inp = data.get("tool_name", ""), data.get("tool_input") or {}
        readable = (name in PLAN_TOOLS or tool_matches(name, self.role.auto_tools)
                    or (name == "Bash" and read_only_command(str(inp.get("command", "")))))
        if self.plan_read_only and readable:
            return {}  # 읽기 전용 단계에서도 허용: 기존 권한 규칙/can_use_tool 에 맡긴다
        if self.plan_read_only and name not in NETWORK_TOOLS:
            allow, reason = False, PLAN_DENY_TEXT
        elif self.plan_read_only and name in NETWORK_TOOLS:
            decision = await self._bounded_approve(ApprovalRequest(self.role.name, "tool", _tool_detail(name, inp),
                                                         tool=name, detail={"input": inp}))
            allow, reason = decision.allow, decision.reason
        elif self.verification_command is not None and name == "Bash":
            cmd = str(inp.get("command", ""))
            decision = await self._bounded_approve(ApprovalRequest(self.role.name, "command", "$ " + cmd,
                                                         command=cmd, tool=name))
            allow, reason = decision.allow, decision.reason
        else:
            return {}  # 기존 권한 규칙/can_use_tool에 맡긴다.
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                       "permissionDecision": "allow" if allow else "deny",
                                       "permissionDecisionReason": reason}}

    async def run_turn(self, prompt: str) -> TurnResult:
        assert self.client is not None
        self.busy = True
        texts: list[str] = []
        result = TurnResult(text="")
        try:
            await self.client.query(self.recovery_prompt(prompt))
            async for msg in self.client.receive_response():
                if type(msg).__name__ == "RateLimitEvent":
                    info = getattr(msg, "rate_limit_info", None)
                    if getattr(info, "status", None) == "rejected":
                        result.usage_limited = True
                        result.usage_reset_at = getattr(info, "resets_at", None)
                if isinstance(msg, AssistantMessage):
                    if getattr(msg, "error", None) == "rate_limit":
                        result.ok = False
                        result.error = "rate_limit"
                        result.usage_limited = True
                    ctx = _context_size(getattr(msg, "usage", None))
                    if ctx:
                        self.context_tokens = ctx
                    for b in msg.content:
                        if isinstance(b, TextBlock) and b.text.strip():
                            texts.append(b.text)
                            self.emit("text", text=b.text)
                        elif isinstance(b, ToolUseBlock):
                            self.emit("tool", name=b.name, detail=_tool_detail(b.name, b.input or {}))
                elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                    for b in msg.content:
                        if isinstance(b, ToolResultBlock):
                            self.emit("tool_output", text=clip(_result_text(b.content), 800),
                                      ok=not bool(b.is_error))
                elif isinstance(msg, ResultMessage):
                    self.session_id = msg.session_id or self.session_id
                    self.fork_session = False
                    # total_cost_usd 는 세션 누적값이므로 이번 턴 증가분만 보고한다
                    total = msg.total_cost_usd
                    if total is not None:
                        prev = getattr(self, "_cost_total", 0.0)
                        result.cost_usd = total - prev if total >= prev else total
                        self._cost_total = total
                    usage = msg.usage or {}
                    result.tokens = sum(int(usage.get(k) or 0) for k in
                                        ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                         "cache_creation_input_tokens"))
                    if msg.is_error:
                        result.ok = False
                        result.error = msg.result or "\n".join(getattr(msg, "errors", None) or []) or msg.subtype
                        if getattr(msg, "api_error_status", None) == 429:
                            result.usage_limited = True
                    if msg.result and not texts:
                        texts.append(msg.result)
        except Exception as e:  # CLI 오류, 연결 끊김 등
            result.ok = False
            tail = "\n".join(list(self._stderr)[-5:])
            result.error = f"{type(e).__name__}: {e}" + (f"\n{tail}" if tail else "")
        finally:
            self.busy = False
        result.text = texts[-1] if texts else ""
        result.full_text = "\n\n".join(texts)
        result.context_tokens = self.context_tokens or None
        if not result.ok and is_context_overflow(result.error, result.text):
            result.context_overflow = True
        if result.cost_usd is not None or result.tokens:
            self.bus.emit("usage", self.label, cost_usd=result.cost_usd or 0.0, tokens=result.tokens or 0)
        if result.ok:
            result.usage_limited = False
        return classify(result)

    async def compact(self, instructions: str = "") -> bool:
        """Claude Code 의 /compact 로 대화를 요약·압축한다. instructions 로 보존할 내용을 지정한다."""
        if not self.client or self.reviewer:
            return False
        ok = True

        async def run() -> None:
            nonlocal ok
            await self.client.query(("/compact " + " ".join(instructions.split())).strip())
            async for msg in self.client.receive_response():
                if isinstance(msg, ResultMessage):
                    self.session_id = msg.session_id or self.session_id
                    if msg.is_error:
                        ok = False
                    if msg.total_cost_usd is not None:
                        self._cost_total = msg.total_cost_usd

        try:
            # 압축이 끝나지 않으면 세션을 붙잡고 있지 않도록 시간 제한을 둔다 (실패로 처리 → 호출 쪽이 교체·계속 판단)
            await asyncio.wait_for(run(), self.compact_timeout)
        except asyncio.TimeoutError:
            ok = False
            try:
                await self.client.interrupt()
            except Exception:
                pass
        except Exception:
            ok = False
        if ok:
            self.context_tokens = 0  # 다음 요청에서 다시 측정
        return ok

    async def interrupt(self) -> None:
        if self.client and self.busy:
            try:
                await self.client.interrupt()
            except Exception:
                pass

    async def close(self) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
