"""Codex 어댑터 — `codex app-server` (stdio JSON-RPC 2.0, v2 프로토콜).

사용자의 ~/.codex/config.toml, AGENTS.md, MCP 설정을 app-server 가 그대로 사용합니다.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from typing import Any

from .. import __version__
from ..core.clis import which
from ..core.procs import group_kwargs, reap
from ..core.policy import ApprovalRequest, Decision
from ..core.prompts import REVIEW_SYSTEM
from .base import AgentAdapter, TurnResult, clip, is_context_overflow
from ..core.usage_wait import classify


class RpcError(Exception):
    def __init__(self, err: dict):
        super().__init__(err.get("message", str(err)))
        self.err = err


class CodexAdapter(AgentAdapter):
    proc: asyncio.subprocess.Process | None = None
    _compact_fut: asyncio.Future | None = None

    async def start(self) -> None:
        exe = which("codex")
        if not exe:
            raise RuntimeError("codex CLI 를 찾을 수 없습니다 (npm i -g @openai/codex).")
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._items: dict[str, dict] = {}
        self._turn_id: str | None = None
        self._turn_fut: asyncio.Future | None = None
        self._texts: list[str] = []
        self._tokens_total = 0
        self._tokens_now = 0
        self._compact_fut: asyncio.Future | None = None
        self._stderr: deque[str] = deque(maxlen=30)
        self._closed = False
        self.proc = await asyncio.create_subprocess_exec(
            exe, "app-server",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            cwd=str(self.project), limit=64 * 1024 * 1024, env=os.environ.copy(), **group_kwargs(),
        )
        self._reader = asyncio.create_task(self._read_loop())
        self._err_reader = asyncio.create_task(self._read_stderr())
        await self.request("initialize", {
            "clientInfo": {"name": "duet", "title": "duet orchestrator", "version": __version__},
            "capabilities": {"experimentalApi": False, "requestAttestation": False},
        })
        await self.notify("initialized", {})

        params = self._thread_params()
        if self.session_id:
            try:
                method = "thread/fork" if self.fork_session else "thread/resume"
                try:
                    res = await self.request(method, {"threadId": self.session_id, **params})
                except RpcError as e:
                    if not self.fork_session or e.err.get("code") != -32601:
                        raise
                    self.emit("notice", text="Codex가 thread/fork를 지원하지 않아 resume합니다. "
                              "저장 시점 이후 기억이 포함될 수 있습니다.", level="warn")
                    res = await self.request("thread/resume", {"threadId": self.session_id, **params})
                self.session_id = res["thread"]["id"]
                self.fork_session = False
                return
            except Exception:
                self.emit("notice", text="이전 Codex 스레드를 이어갈 수 없어 새 스레드를 시작합니다.", level="warn")
                self.restore_failed = True
        self.fork_session = False
        res = await self.request("thread/start", params)
        self.session_id = res["thread"]["id"]

    @staticmethod
    def sandbox_off() -> bool:
        """Codex 자체 샌드박스를 끌지 (DUET_CODEX_SANDBOX=off).

        Codex 샌드박스(리눅스 bubblewrap)는 Docker 같은 컨테이너 안에서 네임스페이스를 만들지 못해
        읽기 명령조차 실패한다. 컨테이너에서는 끄고, 명령은 duet 권한 정책(승인 요청)으로만 거른다.
        """
        return os.environ.get("DUET_CODEX_SANDBOX", "").strip().lower() in ("off", "0", "false", "none",
                                                                           "danger-full-access")

    def _thread_params(self) -> dict:
        read_only = self.reviewer or self.role.permissions == "read_only"
        p: dict[str, Any] = {
            "cwd": str(self.project),
            "sandbox": ("danger-full-access" if self.sandbox_off()
                        else "read-only" if read_only else "workspace-write"),
            # untrusted: 신뢰 목록 밖의 명령은 실행 전에 승인 요청 → duet 정책으로 라우팅
            "approvalPolicy": "untrusted" if self.sandbox_off() or not self.reviewer else "never",
            "developerInstructions": REVIEW_SYSTEM if self.reviewer else self.system_append,
        }
        if self.role.model:
            p["model"] = self.role.model
        if self.context_limit:
            # 턴 도중에도 이 크기를 넘으면 Codex 가 스스로 자동 압축한다
            p["config"] = {"model_auto_compact_token_limit": int(self.context_limit)}
        return p

    # ---------------- JSON-RPC ----------------
    async def _send(self, obj: dict) -> None:
        assert self.proc and self.proc.stdin
        self.proc.stdin.write((json.dumps(obj, ensure_ascii=False) + "\n").encode())
        await self.proc.stdin.drain()

    async def request(self, method: str, params: dict, timeout: float | None = 120) -> Any:
        rid = self._next_id
        self._next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return await asyncio.wait_for(fut, timeout)

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        while True:
            line = await self.proc.stderr.readline()
            if not line:
                return
            self._stderr.append(line.decode(errors="replace").rstrip())

    async def _read_loop(self) -> None:
        assert self.proc and self.proc.stdout
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if "method" in msg and "id" in msg:
                    asyncio.create_task(self._on_server_request(msg))
                elif "method" in msg:
                    self._on_notification(msg["method"], msg.get("params") or {})
                elif "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_exception(RpcError(msg["error"]))
                        else:
                            fut.set_result(msg.get("result"))
        finally:
            err = RuntimeError("codex app-server 가 종료되었습니다.\n" + "\n".join(list(self._stderr)[-5:]))
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(err)
            self._pending.clear()
            if self._turn_fut and not self._turn_fut.done():
                self._turn_fut.set_exception(err)

    # ---------------- 알림 ----------------
    def _on_notification(self, method: str, p: dict) -> None:
        if method == "item/started":
            item = p.get("item") or {}
            t = item.get("type")
            self._items[item.get("id", "")] = item
            if t == "commandExecution":
                self.emit("tool", name="shell", detail="$ " + str(item.get("command", "")))
            elif t == "fileChange":
                paths = [c.get("path", "") for c in item.get("changes") or []]
                self.emit("tool", name="patch", detail="patch " + ", ".join(paths))
            elif t == "mcpToolCall":
                self.emit("tool", name="mcp", detail=f"{item.get('server')}.{item.get('tool')}")
            elif t == "webSearch":
                self.emit("tool", name="web", detail=f"web search {item.get('query', '')}")
        elif method == "item/completed":
            item = p.get("item") or {}
            t = item.get("type")
            self._items[item.get("id", "")] = item
            if t == "agentMessage" and (item.get("text") or "").strip():
                self._texts.append(item["text"])
                self.emit("text", text=item["text"])
            elif t == "commandExecution":
                code = item.get("exitCode")
                self.emit("tool_output", text=clip(item.get("aggregatedOutput") or "", 800) + f"\n(exit {code})",
                          ok=code == 0)
            elif t == "fileChange":
                self.emit("tool_output", text=f"patch {item.get('status')}", ok=item.get("status") != "failed")
        elif method == "thread/tokenUsage/updated":
            usage = p.get("tokenUsage") or {}
            total = (usage.get("total") or {}).get("totalTokens")
            if isinstance(total, int):
                self._tokens_now = total
            last_in = (usage.get("last") or {}).get("inputTokens")
            if isinstance(last_in, int) and last_in > 0:
                self.context_tokens = last_in  # 마지막 요청의 입력 크기 = 현재 컨텍스트
        elif method == "thread/compacted":
            if self._compact_fut and not self._compact_fut.done():
                self._compact_fut.set_result(True)
        elif method == "account/rateLimits/updated":
            limits = p.get("rateLimits") or {}
            stamps = []
            for window in (limits.get("primary"), limits.get("secondary")):
                if isinstance(window, dict) and isinstance(window.get("usedPercent"), (int, float)) and window["usedPercent"] >= 100:
                    from ..core.usage_wait import reset_time
                    stamp = reset_time(window.get("resetsAt"))
                    if stamp:
                        stamps.append(stamp)
            self._usage_reset_at = max(stamps) if stamps else None
        elif method == "error":
            err = p.get("error") or {}
            msg = err.get("message") or "codex 오류"
            if p.get("willRetry"):
                self.emit("notice", text=f"codex: {msg}", level="warn")
            else:
                detail = err.get("additionalDetails")
                self.emit("error", text=msg + (f" ({detail})" if detail else ""))
        elif method in ("warning", "configWarning", "deprecationNotice"):
            text = p.get("message") or p.get("summary")
            if text:
                self.emit("notice", text=f"codex: {text}"[:300], level="warn")
        elif method == "turn/completed":
            turn = p.get("turn") or {}
            if self._compact_fut and not self._compact_fut.done() and not self.busy:
                self._compact_fut.set_result(turn.get("status") == "completed")
            if self._turn_fut and not self._turn_fut.done() and (self._turn_id in (None, turn.get("id"))):
                self._turn_fut.set_result(turn)

    # ---------------- 서버 → 클라이언트 요청 (승인) ----------------
    async def _on_server_request(self, msg: dict) -> None:
        method, p, rid = msg["method"], msg.get("params") or {}, msg["id"]
        try:
            if method == "item/commandExecution/requestApproval":
                cmd = p.get("command") or ""
                req = ApprovalRequest(self.role.name, "command", "$ " + cmd, command=cmd,
                                      detail={"reason": p.get("reason"), "cwd": p.get("cwd")})
                d = await self.approve(req)
                await self._reply(rid, {"decision": self._v2_decision(d)})
            elif method == "item/fileChange/requestApproval":
                item = self._items.get(p.get("itemId", ""), {})
                paths = [c.get("path", "") for c in item.get("changes") or []]
                if p.get("grantRoot"):
                    paths.append(p["grantRoot"])
                req = ApprovalRequest(self.role.name, "file", "patch " + (", ".join(paths) or "(파일 변경)"),
                                      paths=paths, detail={"reason": p.get("reason")})
                d = await self.approve(req)
                await self._reply(rid, {"decision": self._v2_decision(d)})
            elif method == "item/permissions/requestApproval":
                perms = p.get("permissions") or {}
                req = ApprovalRequest(self.role.name, "permissions",
                                      f"권한 확장: {json.dumps(perms, ensure_ascii=False)[:200]}",
                                      detail={"reason": p.get("reason")})
                d = await self.approve(req)
                granted = {k: v for k, v in perms.items() if v is not None} if d.allow else {}
                await self._reply(rid, {"permissions": granted, "scope": "session" if d.scope == "session" else "turn"})
            elif method in ("execCommandApproval", "applyPatchApproval"):  # v1 호환
                cmd = " ".join(p.get("command") or []) if method == "execCommandApproval" else ""
                paths = list((p.get("fileChanges") or {}).keys())
                req = (ApprovalRequest(self.role.name, "command", "$ " + cmd, command=cmd)
                       if cmd else ApprovalRequest(self.role.name, "file", "patch " + ", ".join(paths), paths=paths))
                d = await self.approve(req)
                dec: Any = ("approved_for_session" if d.scope == "session" else "approved") if d.allow \
                    else {"denied": {"rejection": d.reason or "거부됨"}}
                await self._reply(rid, {"decision": dec})
            else:
                await self._send({"jsonrpc": "2.0", "id": rid,
                                  "error": {"code": -32601, "message": f"duet 은 {method} 를 지원하지 않습니다."}})
        except Exception as e:
            try:
                await self._send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": str(e)}})
            except Exception:
                pass

    @staticmethod
    def _v2_decision(d: Decision) -> str:
        if d.allow:
            return "acceptForSession" if d.scope == "session" else "accept"
        return "cancel" if d.interrupt else "decline"

    async def _reply(self, rid: Any, result: dict) -> None:
        await self._send({"jsonrpc": "2.0", "id": rid, "result": result})

    # ---------------- 턴 ----------------
    async def run_turn(self, prompt: str) -> TurnResult:
        prompt = self.recovery_prompt(prompt)
        self.busy = True
        self._texts = []
        self._turn_id = None
        self._tokens_now = self._tokens_total
        self._turn_fut = asyncio.get_running_loop().create_future()
        result = TurnResult(text="")
        try:
            params: dict[str, Any] = {"threadId": self.session_id,
                                      "input": [{"type": "text", "text": prompt, "text_elements": []}]}
            if self.sandbox_off():
                # 샌드박스 없이: 신뢰 목록 밖의 명령·파일 변경은 모두 승인 요청 → duet 정책이 단계(plan 읽기 전용 등)에 맞게 판정
                params["sandboxPolicy"] = {"type": "dangerFullAccess"}
                params["approvalPolicy"] = "untrusted"  # 심사 세션도: 샌드박스가 없으니 신뢰 밖 명령은 막는다
            elif self.plan_read_only:
                params["sandboxPolicy"] = {"type": "readOnly", "networkAccess": False}
                params["approvalPolicy"] = "never"
            elif self.agreement_phase or getattr(self, "_agreement_sandbox_used", False):
                read_only = self.reviewer or self.role.permissions == "read_only"
                params["sandboxPolicy"] = ({"type": "readOnly", "networkAccess": False} if read_only else
                                           {"type": "workspaceWrite", "writableRoots": [str(self.project)],
                                            "networkAccess": False})
                params["approvalPolicy"] = "never" if self.reviewer else "untrusted"
            if "sandboxPolicy" in params:
                self._agreement_sandbox_used = True
            if self.role.effort:
                params["effort"] = self.role.effort
            res = await self.request("turn/start", params)
            self._turn_id = (res.get("turn") or {}).get("id")
            turn = await self._turn_fut
            status = turn.get("status")
            if status == "failed":
                result.ok = False
                err = turn.get("error") or {}
                result.error = err.get("message") or "turn failed"
                classify(result, err)
                if err.get("codexErrorInfo") == "contextWindowExceeded":
                    result.context_overflow = True
            elif status == "interrupted":
                result.interrupted = True
        except Exception as e:
            result.ok = False
            result.error = f"{type(e).__name__}: {e}"
            classify(result, getattr(e, "err", None))
            if self.plan_read_only:
                result.error = "REPORT deviation: plan readOnly 턴을 실행할 수 없습니다. " + result.error
        finally:
            self.busy = False
        result.text = self._texts[-1] if self._texts else ""
        result.full_text = "\n\n".join(self._texts)
        result.context_tokens = self.context_tokens or None
        if not result.ok and is_context_overflow(result.error):
            result.context_overflow = True
        used = max(0, self._tokens_now - self._tokens_total)
        self._tokens_total = self._tokens_now
        result.tokens = used
        if used:
            self.bus.emit("usage", self.label, cost_usd=0.0, tokens=used)
        classify(result)
        if result.usage_limited and result.usage_reset_at is None:
            result.usage_reset_at = getattr(self, "_usage_reset_at", None)
        return result

    async def compact(self, instructions: str = "") -> bool:
        """app-server 의 thread/compact/start 로 스레드를 압축한다.
        (Codex 압축은 지시문을 받지 않으므로 보존 내용은 작업 기억 파일이 맡는다.)"""
        if not self.session_id or self.busy:
            return False
        self._compact_fut = asyncio.get_running_loop().create_future()
        try:
            await self.request("thread/compact/start", {"threadId": self.session_id})
            ok = await asyncio.wait_for(self._compact_fut, self.compact_timeout)
        except Exception:
            ok = False
        finally:
            self._compact_fut = None
        if ok:
            self.context_tokens = 0
        return bool(ok)

    async def interrupt(self) -> None:
        if self.busy and self._turn_id:
            try:
                await self.request("turn/interrupt", {"threadId": self.session_id, "turnId": self._turn_id},
                                   timeout=10)
            except Exception:
                pass

    async def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()  # type: ignore[union-attr]
                await asyncio.wait_for(self.proc.wait(), 3)
            except Exception:
                pass
        if self.proc:
            await reap(self.proc)  # 자손(MCP 서버·명령)까지 정리하고 파이프를 닫는다
        for t in (getattr(self, "_reader", None), getattr(self, "_err_reader", None)):
            if t:
                t.cancel()
