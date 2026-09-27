"""Antigravity CLI(agy) 어댑터 — `agy -p … --output-format stream-json` 헤드리스 실행.

- 로그인·설정·AGENTS.md/GEMINI.md·MCP 는 설치된 agy 가 그대로 쓴다 (API 직접 호출 없음).
- 턴마다 agy 프로세스를 하나 띄우고, 대화는 --conversation <id> 로 이어간다.
- 권한: 헤드리스 agy 는 훅의 allow 를 무시하므로(#1053) --dangerously-skip-permissions 로 띄우고,
  작업 폴더의 .agents/hooks.json PreToolUse 훅(agy_hook.py)이 duet 정책에 물어 거부할 것만 막는다.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import sys
import tempfile
from collections import deque
from pathlib import Path
from typing import Any

from ..core.clis import which
from ..core.policy import PLAN_DENY_TEXT, ApprovalRequest, read_only_command, tool_matches
from .base import AgentAdapter, TurnResult, clip, is_context_overflow

HOOK_NAME = "duet-policy"
HOOK_SCRIPT = Path(__file__).with_name("agy_hook.py")

# agy 도구 → duet 정책에서 쓰는 도구 이름 (Claude 도구 이름에 맞춤)
READ_TOOLS = {
    "view_file": "Read", "list_dir": "LS", "find_by_name": "Glob", "grep_search": "Grep",
    "read_resource": "ReadMcpResourceTool", "list_resources": "ListMcpResourcesTool",
    "command_status": "BashOutput", "list_permissions": "Read", "manage_task": "TodoWrite",
    "wait": "BashOutput", "wait_5_seconds": "BashOutput", "finish": "TodoWrite",
    "invoke_subagent": "Agent", "manage_subagents": "Agent", "define_subagent": "Agent",
}
NETWORK = {"search_web": "WebSearch", "read_url_content": "WebFetch"}
FILE_WRITE_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content", "sed_file",
                    "notebook_edit"}
PLAN_OK = {"view_file", "list_dir", "find_by_name", "grep_search", "search_web", "read_url_content",
           "finish", "manage_task", "wait", "wait_5_seconds"}


def _paths_in(args: dict) -> list[str]:
    out = []
    for k, v in (args or {}).items():
        lk = k.lower()
        if isinstance(v, str) and ("file" in lk or "path" in lk) and v:
            out.append(v)
    return out


def _detail(name: str, args: dict) -> str:
    args = args or {}
    if name == "run_command":
        return "$ " + str(args.get("CommandLine") or args.get("command") or "")
    paths = _paths_in(args)
    if paths:
        return f"{name} {paths[0]}"
    for key in ("Query", "query", "Url", "url", "Pattern", "pattern", "SearchPath"):
        if args.get(key):
            return f"{name} {args[key]}"
    s = json.dumps(args, ensure_ascii=False)
    return f"{name} {s[:160]}"


def to_request(role: str, name: str, args: dict) -> ApprovalRequest:
    """agy 도구 호출을 duet 승인 요청으로 바꾼다."""
    args = args or {}
    if name == "run_command":
        cmd = str(args.get("CommandLine") or args.get("command") or "")
        return ApprovalRequest(role, "command", "$ " + cmd, command=cmd, tool=name)
    if name in FILE_WRITE_TOOLS:
        paths = _paths_in(args)
        return ApprovalRequest(role, "file", f"{name} {' '.join(paths)}", paths=paths, tool=name)
    if name in READ_TOOLS:
        return ApprovalRequest(role, "tool", _detail(name, args), tool=READ_TOOLS[name], detail={"input": args})
    if name in NETWORK:
        return ApprovalRequest(role, "tool", _detail(name, args), tool=NETWORK[name], detail={"input": args})
    if name in ("ask_question", "ask_permission", "ask_custom_permission"):
        return ApprovalRequest(role, "tool", _detail(name, args), tool="AskUserQuestion", detail={"input": args})
    return ApprovalRequest(role, "tool", _detail(name, args), tool=name, detail={"input": args})


def install_hook(workdir: Path, python: str | None = None) -> Path:
    """작업 폴더 .agents/hooks.json 에 duet 훅을 넣는다 (사용자 훅은 보존). git 에서는 로컬 제외 처리."""
    path = workdir / ".agents" / "hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    created = not path.exists()
    cmd = f'"{python or sys.executable}" "{HOOK_SCRIPT}"'
    data[HOOK_NAME] = {"enabled": True, "PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": cmd, "timeout": 3600}]}]}
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if created:
        _git_exclude(workdir, ".agents/hooks.json")
    return path


def _git_exclude(workdir: Path, pattern: str) -> None:
    """커밋에 섞이지 않게 .git/info/exclude 에 추가 (워크트리면 공용 git 디렉터리)."""
    git = workdir / ".git"
    try:
        if git.is_file():  # 워크트리: "gitdir: <main>/.git/worktrees/<id>"
            gd = Path(git.read_text().split(":", 1)[1].strip())
            common = gd.parent.parent if gd.parent.name == "worktrees" else gd
        elif git.is_dir():
            common = git
        else:
            return
        ex = common / "info" / "exclude"
        ex.parent.mkdir(parents=True, exist_ok=True)
        lines = ex.read_text().splitlines() if ex.exists() else []
        if pattern not in lines:
            ex.write_text("\n".join(lines + [pattern]) + "\n")
    except OSError:
        pass


class AgyAdapter(AgentAdapter):
    proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self.exe = which("agy")
        if not self.exe:
            raise RuntimeError("agy(Antigravity CLI)를 찾을 수 없습니다. 설치 후 `agy` 로 로그인하세요.")
        if self.session_id and self.fork_session:
            # agy 는 대화 복제를 지원하지 않는다: 새 대화로 시작하고 작업 기억으로 맥락을 잡는다
            self.emit("notice", text="agy 는 대화 복제를 지원하지 않아 새 대화로 시작합니다.", level="warn")
            self.session_id = None
            self.restore_failed = True
        self.fork_session = False
        self._stderr: deque[str] = deque(maxlen=30)
        self._token = secrets.token_hex(16)
        self._sock = str(Path(tempfile.gettempdir()) / f"duet-agy-{os.getpid()}-{secrets.token_hex(4)}.sock")
        self._server = await asyncio.start_unix_server(self._on_hook, path=self._sock)
        install_hook(self.project)
        self._system_sent = bool(self.session_id)
        self._hook_calls = 0
        self._tool_steps = 0
        # 훅이 실제로 불리는지 확인되기 전까지는 전체 허용 모드로 띄우되, 불리지 않으면 안전 모드로 바꾼다
        self.safe_mode = False

    # ---------------- 훅 브리지 ----------------
    async def _on_hook(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        answer: dict[str, Any] = {"decision": "deny", "reason": "잘못된 요청"}
        try:
            line = await reader.readline()
            msg = json.loads(line.decode() or "{}")
            self._hook_calls += 1
            if msg.get("token") != self._token:
                answer = {"decision": "deny", "reason": "duet 훅 토큰 불일치"}
            else:
                call = (msg.get("payload") or {}).get("toolCall") or {}
                answer = await self._decide(str(call.get("name") or ""), call.get("args") or {})
        except Exception as e:
            answer = {"decision": "deny", "reason": f"duet 훅 처리 오류: {e}"}
        try:
            writer.write(json.dumps(answer, ensure_ascii=False).encode() + b"\n")
            await writer.drain()
            writer.close()
        except Exception:
            pass

    async def _decide(self, name: str, args: dict) -> dict:
        cmd = str((args or {}).get("CommandLine") or "") if name == "run_command" else ""
        viewer = tool_matches(name, self.role.auto_tools) and name != "generate_image"  # 역할의 보기용 도구(브라우저 등)
        if self.plan_read_only and name not in PLAN_OK and not viewer and not (cmd and read_only_command(cmd)):
            return {"decision": "deny", "reason": PLAN_DENY_TEXT}
        req = to_request(self.role.name, name, args)
        d = await self.approve(req)
        if d.allow:
            return {"decision": "allow", "reason": d.reason or "duet 허용"}
        return {"decision": "deny", "reason": d.reason or "duet 정책에서 거부됨"}

    # ---------------- 턴 ----------------
    def _argv(self, prompt: str) -> list[str]:
        argv = [self.exe, "-p", prompt, "--output-format", "stream-json"]
        if not self.safe_mode:
            argv.append("--dangerously-skip-permissions")
        if self.session_id:
            argv += ["--conversation", self.session_id]
        if self.role.model:
            argv += ["--model", self.role.model]
        if self.role.effort and not (self.role.model or "").endswith(("-low", "-medium", "-high")):
            argv += ["--effort", self.role.effort]
        return argv

    async def run_turn(self, prompt: str) -> TurnResult:
        prompt = self.recovery_prompt(prompt)
        if not self._system_sent and self.system_append:
            prompt = f"[duet 역할 지침 — 이 대화 내내 따를 것]\n{self.system_append}\n\n---\n\n{prompt}"
        self.busy = True
        result = TurnResult(text="")
        texts: list[str] = []
        partial: dict[int, list[str]] = {}
        self._hook_calls = self._tool_steps = 0
        env = {**os.environ, "DUET_AGY_BRIDGE": self._sock, "DUET_AGY_TOKEN": self._token}
        err_task = None
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self._argv(prompt), cwd=str(self.project), env=env, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=64 * 1024 * 1024,
                start_new_session=True)
            err_task = asyncio.create_task(self._read_stderr(self.proc))
            final: dict | None = None
            assert self.proc.stdout
            async for raw in self.proc.stdout:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("{"):
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = ev.get("event")
                if kind == "init":
                    self.session_id = ev.get("conversation_id") or self.session_id
                    self._system_sent = True
                elif kind == "step_update":
                    self._on_step(ev.get("step_update") or {}, partial, texts)
                elif kind == "result":
                    final = ev.get("result") or {}
            rc = await self.proc.wait()
            await err_task
            if self._tool_steps and not self._hook_calls and not self.safe_mode:
                # 전체 허용으로 띄웠는데 duet 훅이 한 번도 불리지 않았다 → 정책이 적용되지 않은 것
                self.safe_mode = True
                self.emit("notice", level="warn",
                          text="agy 가 duet 권한 훅을 부르지 않았습니다. 다음 턴부터 agy 를 기본 권한 모드로 띄웁니다 "
                               "(헤드리스에서 명령 실행·파일 쓰기가 자동 거부될 수 있음). .agents/hooks.json 을 확인하세요.")
            if final is None:
                result.ok = False
                tail = "\n".join(list(self._stderr)[-5:])
                result.error = f"agy 가 결과 없이 끝났습니다 (코드 {rc})" + (f"\n{tail}" if tail else "")
                if rc in (-signal.SIGINT, -signal.SIGTERM, 130):
                    result.interrupted = True
            else:
                self.session_id = final.get("conversation_id") or self.session_id
                status = str(final.get("status") or "")
                usage = final.get("usage") or {}
                result.tokens = int(usage.get("total_tokens") or 0) or None
                if final.get("response"):
                    if not texts or texts[-1].strip() != str(final["response"]).strip():
                        texts.append(str(final["response"]))
                if status.upper() == "CANCELLED":
                    result.interrupted = True
                elif status.upper() not in ("SUCCESS", ""):
                    result.ok = False
                    result.error = str(final.get("error") or final.get("message") or status)
                denied = final.get("denied_actions") or []
                if denied:
                    self.emit("notice", level="warn",
                              text="agy 가 거부된 동작: " + ", ".join(str(d.get("display_name") or d.get("action"))
                                                                    for d in denied))
        except asyncio.CancelledError:
            # Review timeout interrupts first. Reap before losing the process
            # handle so a delayed response cannot outlive this connection.
            if self.proc is not None:
                if self.proc.returncode is None:
                    try:
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                await self.proc.wait()
            raise
        except Exception as e:
            result.ok = False
            tail = "\n".join(list(self._stderr)[-5:])
            result.error = f"{type(e).__name__}: {e}" + (f"\n{tail}" if tail else "")
        finally:
            if err_task is not None:
                err_task.cancel()
                await asyncio.gather(err_task, return_exceptions=True)
            self.busy = False
            self.proc = None
        result.text = texts[-1] if texts else ""
        result.full_text = "\n\n".join(texts)
        result.context_tokens = self.context_tokens or None
        if not result.ok and is_context_overflow(result.error, result.text):
            result.context_overflow = True
        if result.tokens:
            self.bus.emit("usage", self.label, cost_usd=0.0, tokens=result.tokens)
        return result

    def _on_step(self, st: dict, partial: dict[int, list[str]], texts: list[str]) -> None:
        idx = int(st.get("step_index") or 0)
        stype = st.get("step_type")
        state = st.get("state")
        usage = st.get("usage") or {}
        if usage.get("input_tokens"):
            self.context_tokens = int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_tokens") or 0)
        if stype == "agent_response":
            delta = st.get("text_delta") or st.get("delta")
            if isinstance(delta, str) and delta:
                partial.setdefault(idx, []).append(delta)
            full = st.get("text") or st.get("response")
            if isinstance(full, str) and full:
                partial[idx] = [full]
            if state == "DONE":
                text = "".join(partial.pop(idx, []))
                if text.strip():
                    texts.append(text)
                    self.emit("text", text=text)
        elif stype == "tool":
            info = st.get("tool_info") or {}
            name = st.get("tool_name") or info.get("name") or "tool"
            params = info.get("parameters") or {}
            if state == "ACTIVE":
                self._tool_steps += 1
                self.emit("tool", name=name, detail=_detail(name, params))
            elif state in ("DONE", "ERROR"):
                err = info.get("error") or {}
                out = err.get("message") if isinstance(err, dict) else str(err)
                if not out:
                    for key in ("result", "output", "response"):
                        if info.get(key):
                            out = info[key] if isinstance(info[key], str) else json.dumps(info[key], ensure_ascii=False)
                            break
                self.emit("tool_output", text=clip(str(out or ""), 800), ok=state == "DONE")

    async def _read_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr
        async for raw in proc.stderr:
            self._stderr.append(raw.decode(errors="replace").rstrip())

    async def interrupt(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGINT)
            except (ProcessLookupError, PermissionError):
                pass

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        server = getattr(self, "_server", None)
        if server:
            server.close()
            self._server = None
        sock = getattr(self, "_sock", None)
        if sock:
            try:
                os.unlink(sock)
            except OSError:
                pass
