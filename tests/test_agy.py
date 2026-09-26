"""agy 어댑터: stream-json 파싱, 대화 이어가기, PreToolUse 훅 → duet 정책 연결 (가짜 agy 로 시험)."""
import asyncio
import json
import os
import stat
import sys
import textwrap

import pytest

from duet.adapters import agy as agy_mod
from duet.adapters.agy import AgyAdapter, install_hook, to_request
from duet.core.config import Role
from duet.core.events import EventBus
from duet.core.models import parse_agy_models
from duet.core.policy import Decision

FAKE_AGY = textwrap.dedent(r'''
    #!{py}
    import json, os, subprocess, sys
    args = sys.argv[1:]
    log = os.environ["FAKE_AGY_LOG"]
    with open(log, "a") as f:
        f.write(json.dumps(args) + "\n")
    conv = args[args.index("--conversation") + 1] if "--conversation" in args else "conv-1"
    def out(obj):
        print(json.dumps(obj), flush=True)
    out({"event": "init", "conversation_id": conv, "init": {"permission_mode": "request-review"}})
    hooks = json.load(open(os.path.join(os.getcwd(), ".agents", "hooks.json")))
    cmd = hooks["duet-policy"]["PreToolUse"][0]["hooks"][0]["command"]
    def hook(name, a):
        p = subprocess.run(cmd, shell=True, input=json.dumps({"toolCall": {"name": name, "args": a}}),
                           capture_output=True, text=True)
        return json.loads(p.stdout or "{}")
    step = 1
    for name, a in [("view_file", {"AbsolutePath": "README.md"}), ("run_command", {"CommandLine": "rm -rf build"}),
                    ("write_to_file", {"TargetFile": "src/a.py"})]:
        d = hook(name, a)
        out({"event": "step_update", "step_update": {"conversation_id": conv, "step_index": step, "state": "ACTIVE",
             "step_type": "tool", "tool_name": name, "tool_info": {"name": name, "parameters": a}}})
        st = {"conversation_id": conv, "step_index": step, "step_type": "tool", "tool_name": name,
              "tool_info": {"name": name, "parameters": a}}
        if d.get("decision") == "deny":
            st["state"] = "ERROR"; st["tool_info"]["error"] = {"message": "denied: " + d.get("reason", "")}
        else:
            st["state"] = "DONE"
        out({"event": "step_update", "step_update": st})
        step += 1
    for piece in ["안녕", "하세요"]:
        out({"event": "step_update", "step_update": {"conversation_id": conv, "step_index": step, "state": "ACTIVE",
             "step_type": "agent_response", "text_delta": piece}})
    out({"event": "step_update", "step_update": {"conversation_id": conv, "step_index": step, "state": "DONE",
         "step_type": "agent_response", "usage": {"input_tokens": 12272, "cache_read_tokens": 100}}})
    out({"event": "result", "result": {"conversation_id": conv, "status": "SUCCESS", "response": "안녕하세요",
         "usage": {"total_tokens": 12586}}})
''')


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "agy"
    exe.parent.mkdir()
    exe.write_text(FAKE_AGY.replace("{py}", sys.executable).lstrip())
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "agy-args.log"
    monkeypatch.setenv("FAKE_AGY_LOG", str(log))
    monkeypatch.setattr(agy_mod, "which", lambda name: str(exe))
    return log


def test_parse_models():
    text = "Fetching available models...\ngemini-3.8-flash-medium Gemini 3.8 Flash (Medium)\nclaude-sonnet-4-6 Claude Sonnet 4.6 (Thinking)\n"
    ms = parse_agy_models(text)
    assert [m["id"] for m in ms] == ["gemini-3.8-flash-medium", "claude-sonnet-4-6"]
    assert ms[0]["effort"] == "medium" and ms[1]["effort"] is None


def test_request_mapping():
    r = to_request("researcher", "run_command", {"CommandLine": "ls -la"})
    assert r.kind == "command" and r.command == "ls -la"
    r = to_request("researcher", "replace_file_content", {"TargetFile": "/p/a.py", "Instruction": "x"})
    assert r.kind == "file" and r.paths == ["/p/a.py"]
    assert to_request("r", "view_file", {"AbsolutePath": "a"}).tool == "Read"
    assert to_request("r", "search_web", {"query": "x"}).tool == "WebSearch"


def test_hook_is_neutral_outside_duet(tmp_path):
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("DUET_AGY")}
    p = subprocess.run([sys.executable, str(agy_mod.HOOK_SCRIPT)], input='{"toolCall":{"name":"run_command"}}',
                       capture_output=True, text=True, env=env)
    assert p.returncode == 0 and p.stdout.strip() == ""


def test_install_hook_keeps_user_hooks_and_excludes(tmp_path):
    (tmp_path / ".git" / "info").mkdir(parents=True)
    (tmp_path / ".agents").mkdir()
    (tmp_path / ".agents" / "hooks.json").write_text(json.dumps({"mine": {"enabled": True}}))
    install_hook(tmp_path)
    data = json.loads((tmp_path / ".agents" / "hooks.json").read_text())
    assert "mine" in data and "duet-policy" in data
    # 사용자 파일이 원래 있었으면 exclude 하지 않는다
    assert not (tmp_path / ".git" / "info" / "exclude").exists()
    other = tmp_path / "w"
    (other / ".git" / "info").mkdir(parents=True)
    install_hook(other)
    assert ".agents/hooks.json" in (other / ".git" / "info" / "exclude").read_text()


def test_turn_bridge_and_resume(tmp_path, fake_agy):
    seen = []

    async def approver(req):
        seen.append(req)
        if req.kind == "command" and "rm -rf" in req.command:
            return Decision(False, "삭제 금지", by="policy")
        return Decision(True, "ok", by="auto")

    async def go():
        bus = EventBus()
        events = []
        bus.subscribe(events.append)
        role = Role("researcher", "agy", "gemini-3.8-flash-medium")
        ad = AgyAdapter(role, tmp_path, bus, approver, None, "역할 지침 XYZ")
        await ad.start()
        tr1 = await ad.run_turn("첫 질문")
        tr2 = await ad.run_turn("두 번째")
        await ad.close()
        return ad, tr1, tr2, events

    ad, tr1, tr2, events = asyncio.run(go())
    assert tr1.ok and tr1.text == "안녕하세요" and tr1.tokens == 12586
    assert ad.session_id == "conv-1" and ad.context_tokens == 12372
    kinds = [(r.kind, r.tool) for r in seen]
    assert ("tool", "Read") in kinds and ("command", "run_command") in kinds and ("file", "write_to_file") in kinds
    outs = [e for e in events if e.kind == "tool_output"]
    assert any(not e.data["ok"] and "삭제 금지" in e.data["text"] for e in outs)
    calls = [json.loads(l) for l in fake_agy.read_text().splitlines()]
    assert "역할 지침 XYZ" in calls[0][1] and "--conversation" not in calls[0]
    assert calls[1][calls[1].index("--conversation") + 1] == "conv-1" and "역할 지침" not in calls[1][1]
    assert "--dangerously-skip-permissions" in calls[0] and calls[0][calls[0].index("--model") + 1] == "gemini-3.8-flash-medium"
    assert "--effort" not in calls[0]


def test_plan_read_only_denies_writes_in_hook(tmp_path, fake_agy):
    seen = []

    async def approver(req):
        seen.append(req)
        return Decision(True, "ok")

    async def go():
        ad = AgyAdapter(Role("researcher", "agy"), tmp_path, EventBus(), approver, None, "")
        await ad.start()
        ad.plan_read_only = True
        await ad.run_turn("계획")
        await ad.close()
    asyncio.run(go())
    assert [r.tool for r in seen] == ["Read"]  # 명령·쓰기는 정책까지 가지 않고 훅에서 거부
