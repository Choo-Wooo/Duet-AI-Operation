"""시작할 때 각 CLI 가 어떤 계정(구독 / API 키)으로 로그인돼 있는지 확인한다.

모델 호출은 하지 않으므로 토큰·비용이 들지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import os

from .clis import which

PLAN_NAMES = {
    "free": "Free", "go": "Go", "plus": "Plus", "pro": "Pro", "prolite": "Pro Lite", "team": "Team",
    "business": "Business", "enterprise": "Enterprise", "edu": "Edu", "edu_plus": "Edu Plus", "edu_pro": "Edu Pro",
}


async def _claude_account() -> str:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    kw = {"setting_sources": ["user"]}
    cli = which("claude")
    if cli:
        kw["cli_path"] = cli
    client = ClaudeSDKClient(ClaudeAgentOptions(**kw))
    await client.connect()
    try:
        info = await client.get_server_info() or {}
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass
    acc = info.get("account") or {}
    sub = acc.get("subscriptionType") or acc.get("subscription_type") or ""
    provider = acc.get("apiProvider") or ""
    email = acc.get("email") or acc.get("emailAddress") or ""
    label = sub or "알 수 없음"
    if provider and provider != "firstParty":
        label += f" ({provider})"
    if email:
        label += f" · {email}"
    if not sub or "api" in sub.lower() or (provider and provider != "firstParty"):
        return f"경고: Claude 계정 {label} — 구독이 아니라 API 키/클라우드 과금일 수 있습니다. 터미널에서 claude 실행 후 /status 로 확인하세요."
    return f"Claude 계정: {label} (구독 — 상태 줄 $ 는 API 요금 환산값이며 실제 청구 아님)"


async def _codex_account() -> str:
    exe = which("codex")
    if not exe:
        return "Codex 계정: codex CLI 없음"
    proc = await asyncio.create_subprocess_exec(
        exe, "app-server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024)

    async def call(rid: int, method: str, params: dict) -> dict:
        proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
        await proc.stdin.drain()
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("codex app-server 종료")
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("id") == rid and "method" not in msg:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "오류"))
                return msg.get("result") or {}

    try:
        await call(1, "initialize", {"clientInfo": {"name": "duet", "title": "duet", "version": "0"},
                                     "capabilities": {"experimentalApi": False, "requestAttestation": False}})
        proc.stdin.write(b'{"jsonrpc":"2.0","method":"initialized","params":{}}\n')
        await proc.stdin.drain()
        res = await call(2, "account/read", {})
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
    acc = res.get("account")
    if not acc:
        return "경고: Codex 로그인 정보가 없습니다. 터미널에서 codex login 을 실행하세요."
    t = acc.get("type")
    if t == "chatgpt":
        plan = acc.get("planType") or "unknown"
        email = acc.get("email") or ""
        return f"Codex 계정: ChatGPT {PLAN_NAMES.get(plan, plan)} 플랜" + (f" · {email}" if email else "") + " (구독)"
    if t == "apiKey":
        return "경고: Codex 가 OpenAI API 키로 로그인돼 있어 사용량만큼 API 요금이 청구됩니다. ChatGPT 구독으로 쓰려면 codex login 으로 다시 로그인하세요."
    return f"경고: Codex 계정 유형 {t} — 구독이 아닐 수 있습니다."


async def _probe(clis: list[str], timeout: float) -> list[str]:
    jobs = []
    if "claude" in clis:
        jobs.append(("Claude", _claude_account()))
    if "codex" in clis:
        jobs.append(("Codex", _codex_account()))
    out = []
    results = await asyncio.gather(*(asyncio.wait_for(j, timeout) for _, j in jobs), return_exceptions=True)
    for (name, _), r in zip(jobs, results):
        if isinstance(r, BaseException):
            out.append(f"{name} 계정을 확인하지 못했습니다 ({type(r).__name__}: {r})")
        else:
            out.append(r)
    return out


def check_accounts(clis: list[str], timeout: float = 25) -> list[str]:
    msgs = []
    if "claude" in clis and os.environ.get("ANTHROPIC_API_KEY"):
        msgs.append("경고: ANTHROPIC_API_KEY 환경변수가 설정돼 있습니다. Claude Code 가 구독 대신 API 키로 과금될 수 있습니다.")
    if "codex" in clis and os.environ.get("OPENAI_API_KEY"):
        msgs.append("참고: OPENAI_API_KEY 환경변수가 설정돼 있습니다 (Codex 는 로그인 방식에 따라 무시할 수 있음).")
    try:
        msgs.extend(asyncio.run(_probe(clis, timeout)))
    except Exception as e:
        msgs.append(f"계정 확인 실패: {e}")
    return msgs
