"""로그인된 각 CLI 에서 쓸 수 있는 모델 목록 조회 (모델 호출 없음, 토큰 비용 없음).

- claude: Agent SDK get_server_info() 의 models
- codex : app-server model/list
- agy   : `agy models` 출력 ("<slug> <표시 이름>")
결과는 .duet/models.json 에 캐시한다.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path

from .clis import which


def parse_agy_models(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"^([a-z0-9][a-z0-9.\-_]+)\s+(.+)$", line)
        if not m or line.lower().startswith("fetching"):
            continue
        slug, name = m.groups()
        eff = re.search(r"-(low|medium|high)$", slug)
        out.append({"id": slug, "name": name.strip(), "effort": eff.group(1) if eff else None})
    return out


async def agy_models(timeout: float = 30) -> list[dict]:
    exe = which("agy")
    if not exe:
        raise RuntimeError("agy CLI 없음")
    proc = await asyncio.create_subprocess_exec(exe, "models", stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.DEVNULL,
                                                start_new_session=True)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    models = parse_agy_models(out.decode(errors="replace"))
    if proc.returncode or not models:
        msg = (err.decode(errors="replace") or out.decode(errors="replace")).strip().splitlines()
        raise RuntimeError(msg[-1] if msg else f"agy models 종료 코드 {proc.returncode}")
    return models


async def claude_models() -> list[dict]:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

    kw: dict = {"setting_sources": ["user"]}
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
    out = []
    for m in info.get("models") or []:
        mid = m.get("resolvedModel") or m.get("value")
        if not mid:
            continue
        out.append({"id": mid, "alias": m.get("value"), "name": m.get("displayName") or mid,
                    "efforts": m.get("supportedEffortLevels") or [], "description": m.get("description") or ""})
    return out


async def codex_models() -> list[dict]:
    exe = which("codex")
    if not exe:
        raise RuntimeError("codex CLI 없음")
    proc = await asyncio.create_subprocess_exec(
        exe, "app-server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024, start_new_session=True)

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
        res = await call(2, "model/list", {})
    finally:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
        await proc.wait()
    out = []
    for m in res.get("data") or res.get("models") or []:
        mid = m.get("model") or m.get("id")
        if not mid:
            continue
        efforts = [e.get("reasoningEffort") if isinstance(e, dict) else e
                   for e in (m.get("supportedReasoningEfforts") or [])]
        out.append({"id": mid, "name": m.get("displayName") or mid, "efforts": [e for e in efforts if e],
                    "default": bool(m.get("isDefault")), "description": m.get("description") or ""})
    return out


PROBES = {"claude": claude_models, "codex": codex_models, "agy": agy_models}


async def list_all(clis: list[str], timeout: float = 40) -> dict[str, dict]:
    """{cli: {"models": [...]} 또는 {"error": "..."}}"""
    names = [c for c in clis if c in PROBES]
    results = await asyncio.gather(*(asyncio.wait_for(PROBES[c](), timeout) for c in names),
                                   return_exceptions=True)
    out: dict[str, dict] = {}
    for c, r in zip(names, results):
        if isinstance(r, BaseException):
            out[c] = {"error": f"{type(r).__name__}: {r}", "models": []}
        else:
            out[c] = {"models": r}
    return out


def cache_path(dotduet: Path) -> Path:
    return dotduet / "models.json"


def read_cache(dotduet: Path) -> dict | None:
    try:
        return json.loads(cache_path(dotduet).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


async def refresh(dotduet: Path, clis: list[str]) -> dict:
    data = {"updated": time.time(), "clis": await list_all(clis)}
    try:
        cache_path(dotduet).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
    return data
