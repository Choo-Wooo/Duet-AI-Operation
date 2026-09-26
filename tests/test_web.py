"""웹 UI 서버: 토큰 인증, WebSocket 초기 상태·명령·설정·역할 편집·승인 응답."""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from duet.core.config import Config, Role
from duet.core.dialogue import Dialogue
from duet.core.events import EventBus
from duet.core.policy import ApprovalRequest
from duet.web.server import WebUI, build_app


def make_ui(tmp_path):
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles = {"architect": Role("architect", "claude", permissions="read_only"),
                 "implementer": Role("implementer", "codex", max_sessions=2)}
    cfg.settings["git_snapshots"] = False
    cfg.save_roles()
    cfg.save_state()
    Dialogue(tmp_path).ensure()
    return WebUI(cfg, EventBus(), ["시작"], True, "tok123")


def test_web_flow(tmp_path):
    async def go():
        ui = make_ui(tmp_path)
        client = TestClient(TestServer(build_app(ui)))
        await client.start_server()
        try:
            r = await client.get("/")
            assert r.status == 403
            r = await client.get("/?t=tok123")
            assert r.status == 200 and "duet" in await r.text()
            r = await client.get("/api/state")  # 쿠키로 인증
            assert r.status == 200 and (await r.json())["main"] == "architect"
            ws = await client.ws_connect("/ws?t=tok123")
            hello = json.loads((await ws.receive()).data)
            assert hello["type"] == "hello" and hello["state"]["startup"] == ["시작"]

            async def call(obj):
                await ws.send_str(json.dumps(obj))
                while True:
                    m = json.loads((await ws.receive(timeout=5)).data)
                    if m.get("rid") == obj.get("rid"):
                        return m

            m = await call({"type": "setting", "key": "max_parallel", "value": "6", "rid": 1})
            assert ui.cfg.settings["max_parallel"] == 6 and "max_parallel = 6" in m["text"]
            m = await call({"type": "setting", "key": "max_parallel", "value": "0", "rid": 2})
            assert "범위" in m["text"] and ui.cfg.settings["max_parallel"] == 6
            m = await call({"type": "setting", "key": "auto_merge", "value": False, "rid": 3})
            assert ui.cfg.settings["auto_merge"] is False
            m = await call({"type": "role_set", "name": "implementer", "field": "max_sessions", "value": "3", "rid": 4})
            assert ui.cfg.roles["implementer"].max_sessions == 3
            m = await call({"type": "role_add", "name": "researcher", "cli": "agy", "model": "gemini-3.8-flash-medium",
                            "permissions": "read_only", "max_sessions": 2, "brief": "조사", "rid": 5})
            assert ui.cfg.roles["researcher"].cli == "agy"
            m = await call({"type": "input", "text": "/work", "rid": 6})
            assert "병렬 작업" in m["text"]
            m = await call({"type": "read", "path": "../etc/passwd", "rid": 7})
            assert m.get("error")
            m = await call({"type": "read", "path": "DIALOGUE.md", "rid": 8})
            assert "DIALOGUE" in m["path"] and m.get("text") is not None

            # 승인 요청 → 브라우저에 pending → 응답
            task = asyncio.ensure_future(ui.ask_approval(ApprovalRequest("implementer", "command", "$ rm -rf x",
                                                                         command="rm -rf x"), "위험", None))
            while True:
                m = json.loads((await ws.receive(timeout=5)).data)
                if m["type"] == "pending":
                    break
            await ws.send_str(json.dumps({"type": "answer", "id": m["id"], "answer": "n", "reason": "안 됨"}))
            d = await asyncio.wait_for(task, 5)
            assert not d.allow and d.reason == "안 됨"
            await ws.close()
        finally:
            await ui.close()
            await ui.orch.close()
            await client.close()
    asyncio.run(go())
