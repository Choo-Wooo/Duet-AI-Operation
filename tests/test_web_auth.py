"""Trusted browser enrollment, isolation, revocation and request boundaries."""
import asyncio
import hashlib
import json
import os
import time
from contextlib import asynccontextmanager

import pytest
from aiohttp import ClientSession, CookieJar, WSMsgType, WSServerHandshakeError
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from duet.core.config import Config
from duet.core.events import EventBus
from duet.web.auth import DeviceRegistry, TRUST_SECONDS, normalize_origin
from duet.web.server import WebUI, build_app


@asynccontextmanager
async def running(root, token="first-token", *, port=None, cookie_jar=None):
    cfg = Config(root)
    cfg.dir.mkdir(parents=True, exist_ok=True)
    cfg.settings["git_snapshots"] = False
    ui = WebUI(cfg, EventBus(), [], True, token)
    client = TestClient(TestServer(build_app(ui), port=port), cookie_jar=cookie_jar)
    await client.start_server()
    try:
        yield client, ui
    finally:
        await ui.close()
        await ui.orch.close()
        for ws in list(ui.clients):
            await ws.close()
        await client.close()


def origin(client):
    return str(client.make_url("/")).rstrip("/")


async def enroll(client, token="first-token"):
    response = await client.get("/", params={"t": token}, headers={"User-Agent": "Mozilla/5.0 (iPhone) AppleWebKit Safari/604.1"})
    assert response.status == 200
    cookie = next(value for name, value in response.cookies.items() if name.startswith("duet_device_"))
    session_response = await client.get("/api/session")
    assert session_response.status == 200
    return cookie, (await session_response.json())["device"]


def test_browser_remembered_for_refresh_docs_ws_and_token_rotation(tmp_path):
    async def go():
        (tmp_path / "hello.md").write_text("# Hello")
        jar = CookieJar(unsafe=True)
        async with running(tmp_path, cookie_jar=jar) as (client, ui):
            assert (await client.get("/api/session?t=first-token")).status == 403
            cookie, device = await enroll(client)
            assert cookie["httponly"] and cookie["samesite"] == "Lax"
            assert int(cookie["max-age"]) > TRUST_SECONDS - 10
            assert not cookie["secure"]
            assert device["current"] and device["label"] == "iPhone · Safari"
            for route in ("/", "/api/state", "/api/docs", "/api/doc?path=hello.md"):
                response = await client.get(route)
                assert response.status == 200
                assert response.headers["Cache-Control"] == "no-store"
                assert response.headers["Referrer-Policy"] == "no-referrer"
            ws = await client.ws_connect("/ws", headers={"Origin": origin(client)})
            assert (await ws.receive_json())["type"] == "hello"
            await ws.send_json({"type": "state", "rid": "state"})
            assert (await ws.receive_json())["type"] == "state"
            await ws.close()
            first_port = client.server.port
        async with running(tmp_path, "rotated-token", port=first_port, cookie_jar=jar) as (client, ui):
            assert (await client.get("/")).status == 200
            assert (await (await client.get("/api/session")).json())["device"]["id"] == device["id"]
            # The old runtime token no longer enrolls an unrelated browser.
            async with ClientSession() as other:
                assert (await other.get(client.make_url("/?t=first-token"))).status == 403
                assert (await other.get(client.make_url("/"))).status == 403
                assert (await other.get(client.make_url("/?t=rotated-token"))).status == 200
    asyncio.run(go())


def test_registry_contains_hashes_only_and_cookie_registration_is_not_repeated(tmp_path):
    async def go():
        async with running(tmp_path) as (client, ui):
            cookie, device = await enroll(client)
            response = await client.get("/?t=first-token")
            assert not any(name.startswith("duet_device_") for name in response.cookies)
            path = ui.cfg.dir / "private" / "web-devices.json"
            raw = path.read_text()
            assert cookie.value not in raw and "first-token" not in raw
            rows = json.loads(raw)["devices"]
            assert len(rows) == 1 and rows[0]["credential_hash"] == hashlib.sha256(cookie.value.encode()).hexdigest()
            exposed = await (await client.get("/api/devices")).json()
            assert exposed["devices"] == [device]
            assert "credential_hash" not in json.dumps(exposed) and "origin" not in exposed["devices"][0]
            assert (path.parent / ".gitignore").read_text() == "*\n"
            if os.name != "nt":
                assert path.stat().st_mode & 0o777 == 0o600
                assert path.parent.stat().st_mode & 0o777 == 0o700
    asyncio.run(go())


def test_devices_on_different_projects_and_ports_do_not_replace_or_authorize_each_other(tmp_path):
    async def go():
        jar = CookieJar(unsafe=True)
        async with running(tmp_path / "first", cookie_jar=jar) as (first, first_ui):
            first_cookie, _ = await enroll(first)
            async with running(tmp_path / "second", cookie_jar=jar) as (second, second_ui):
                assert (await second.get("/")).status == 403
                second_cookie, _ = await enroll(second)
                assert second_cookie.key != first_cookie.key
                assert (await first.get("/")).status == 200
                assert (await second.get("/")).status == 200
                async with running(tmp_path / "first", cookie_jar=jar) as (third, _):
                    assert (await third.get("/")).status == 403
                    third_cookie, _ = await enroll(third)
                    assert third_cookie.key != first_cookie.key
                    # Even renaming another port's credential cannot cross its binding.
                    async with ClientSession() as clean:
                        response = await clean.get(third.make_url("/"), headers={"Cookie": f"{third_cookie.key}={first_cookie.value}"})
                        assert response.status == 403
    asyncio.run(go())


@pytest.mark.parametrize("bad_cookie", ["garbage", "0" * 24 + "." + "A" * 43, "tamper"])
def test_bad_cookie_cannot_authenticate_even_with_same_ip_user_agent(tmp_path, bad_cookie):
    async def go():
        async with running(tmp_path) as (client, ui):
            cookie, _ = await enroll(client)
            value = cookie.value[:-1] + ("B" if cookie.value[-1] != "B" else "A") if bad_cookie == "tamper" else bad_cookie
            async with ClientSession() as clean:
                response = await clean.get(client.make_url("/api/state"), headers={"Cookie": f"{cookie.key}={value}", "User-Agent": "Mozilla/5.0 (iPhone) AppleWebKit Safari/604.1"})
                assert response.status == 403
    asyncio.run(go())


def test_expired_cookie_denies_http_and_existing_ws_commands(tmp_path):
    async def go():
        async with running(tmp_path) as (client, ui):
            _, device = await enroll(client)
            ws = await client.ws_connect("/ws", headers={"Origin": origin(client)})
            await ws.receive_json()
            path = ui.cfg.dir / "private" / "web-devices.json"
            data = json.loads(path.read_text())
            data["devices"][0].update(created_at=time.time() - 2000, last_seen=time.time() - 1000, expires_at=time.time() - 1)
            path.write_text(json.dumps(data))
            assert (await client.get("/api/state")).status == 403
            await ws.send_json({"type": "setting", "key": "max_parallel", "value": 31})
            message = await ws.receive(timeout=3)
            assert message.type == WSMsgType.CLOSE and message.data == 4001
            assert ui.cfg.settings["max_parallel"] != 31
            # The current runtime URL is still the explicit way to enroll again.
            _, replacement = await enroll(client)
            assert replacement["id"] != device["id"]
    asyncio.run(go())


def test_revoke_another_browser_closes_ws_and_denies_its_http(tmp_path):
    async def go():
        async with running(tmp_path) as (client, ui):
            _, own = await enroll(client)
            async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as other:
                assert (await other.get(client.make_url("/?t=first-token"))).status == 200
                other_device = (await (await other.get(client.make_url("/api/session"))).json())["device"]
                ws = await other.ws_connect(client.make_url("/ws"), headers={"Origin": origin(client)})
                await ws.receive_json()
                revoke = asyncio.create_task(client.post("/api/devices/revoke", json={"id": other_device["id"]}, headers={"Origin": origin(client)}))
                closed = await ws.receive(timeout=3)
                assert closed.type == WSMsgType.CLOSE and closed.data == 4001
                response = await revoke
                assert response.status == 200 and not (await response.json())["current"]
                assert (await other.get(client.make_url("/api/state"))).status == 403
                rows = (await (await client.get("/api/devices")).json())["devices"]
                assert [row["id"] for row in rows] == [own["id"]]
    asyncio.run(go())


def test_revoke_in_another_server_closes_idle_socket(tmp_path, monkeypatch):
    monkeypatch.setattr("duet.web.server.AUTH_RECHECK_SECONDS", 0.02)
    async def go():
        async with running(tmp_path) as (first, ui):
            _, device = await enroll(first)
            ws = await first.ws_connect("/ws", headers={"Origin": origin(first)})
            await ws.receive_json()
            async with running(tmp_path) as (second, _):
                await enroll(second)
                response = await second.post("/api/devices/revoke", json={"id": device["id"]}, headers={"Origin": origin(second)})
                assert response.status == 200
                message = await ws.receive(timeout=3)
                assert message.type == WSMsgType.CLOSE and message.data == 4001
                assert not ui.clients
    asyncio.run(go())


@pytest.mark.parametrize("endpoint", ["/api/logout", "/api/devices/revoke"])
def test_forget_current_browser_clears_cookie_and_closes_socket(tmp_path, endpoint):
    async def go():
        async with running(tmp_path) as (client, ui):
            cookie, device = await enroll(client)
            ws = await client.ws_connect("/ws", headers={"Origin": origin(client)})
            await ws.receive_json()
            revoke = asyncio.create_task(client.post(endpoint, json={"id": device["id"]}, headers={"Origin": origin(client)}))
            assert (await ws.receive(timeout=3)).type == WSMsgType.CLOSE
            response = await revoke
            assert response.status == 200 and (await response.json())["current"]
            assert response.cookies[cookie.key]["max-age"] == "0"
            assert (await client.get("/")).status == 403
            async with ClientSession() as replay:
                assert (await replay.get(client.make_url("/"), headers={"Cookie": f"{cookie.key}={cookie.value}"})).status == 403
    asyncio.run(go())


@pytest.mark.parametrize("origin_value", ["http://evil.invalid", "null", "http://{host}/", "https://{host}", "http://{host}.evil.invalid", "http://user@{host}"])
def test_cross_origin_requests_cannot_use_token_or_cookie(tmp_path, origin_value):
    async def go():
        async with running(tmp_path) as (client, ui):
            _, device = await enroll(client)
            headers = {"Origin": origin_value.format(host=client.make_url("/").authority)}
            for route in ("/api/state", "/api/docs", "/api/session", "/api/devices", "/?t=first-token"):
                assert (await client.get(route, headers=headers)).status == 403
            assert (await client.post("/api/devices/revoke", json={"id": device["id"]}, headers=headers)).status == 403
            with pytest.raises(WSServerHandshakeError):
                await client.ws_connect("/ws?t=first-token", headers=headers)
            assert (await client.get("/api/session")).status == 200
    asyncio.run(go())


def test_mutations_require_same_origin_json_and_ws_cookie_requires_origin(tmp_path):
    async def go():
        async with running(tmp_path) as (client, ui):
            _, device = await enroll(client)
            assert (await client.post("/api/devices/revoke", json={"id": device["id"]})).status == 403
            assert (await client.post("/api/logout", data="{}", headers={"Origin": origin(client)})).status == 403
            assert (await client.post("/api/logout", json=[], headers={"Origin": origin(client)})).status == 400
            assert (await client.post("/api/logout", data="{", headers={"Origin": origin(client), "Content-Type": "application/json"})).status == 400
            assert (await client.get("/api/logout")).status == 405
            assert (await client.get("/api/state", headers={"Sec-Fetch-Site": "cross-site"})).status == 403
            with pytest.raises(WSServerHandshakeError):
                await client.ws_connect("/ws")
            assert (await client.get("/api/session")).status == 200
    asyncio.run(go())


@pytest.mark.parametrize("corrupt", ["{", "[]", '{"version":1,"devices":[{}]}', '{"version":2,"devices":[]}'])
def test_corrupt_registry_fails_closed_without_reset_or_token_reenrollment(tmp_path, corrupt):
    async def go():
        async with running(tmp_path) as (client, ui):
            await enroll(client)
            path = ui.cfg.dir / "private" / "web-devices.json"
            path.write_text(corrupt)
            for route in ("/", "/?t=first-token", "/api/state", "/api/session", "/api/devices"):
                assert (await client.get(route)).status == 503
            assert path.read_text() == corrupt
    asyncio.run(go())


@pytest.mark.symlink
def test_registry_refuses_symlink_without_writing_target(tmp_path):
    async def go():
        async with running(tmp_path) as (client, ui):
            folder = ui.cfg.dir / "private"
            folder.mkdir()
            target = tmp_path / "untouched.json"
            original = '{"version":1,"devices":[]}'
            target.write_text(original)
            (folder / "web-devices.json").symlink_to(target)
            assert (await client.get("/?t=first-token")).status == 503
            assert target.read_text() == original
    asyncio.run(go())


@pytest.mark.parametrize("tls", [False, True])
def test_secure_cookie_uses_real_transport_not_forwarded_headers(tmp_path, tls):
    async def go():
        cfg = Config(tmp_path)
        cfg.dir.mkdir()
        cfg.settings["git_snapshots"] = False
        ui = WebUI(cfg, EventBus(), [], True, "tok")
        app = build_app(ui)
        request = make_mocked_request("GET", "/?t=tok", app=app, sslcontext=object() if tls else None,
                                      headers={"Host": "duet.example:8765", "X-Forwarded-Proto": "https",
                                               "Forwarded": "proto=https;host=other.example"})
        async def page(_):
            return web.Response(text="hello")
        try:
            response = await app.middlewares[0](request, page)
            assert response.status == 200
            cookie = next(value for name, value in response.cookies.items() if name.startswith("duet_device_"))
            assert bool(cookie["secure"]) is tls
            registry = DeviceRegistry(cfg.project, cfg.dir)
            expected_origin = ("https" if tls else "http") + "://duet.example:8765"
            assert registry.authenticate(cookie.value, expected_origin)
        finally:
            await ui.close()
            await ui.orch.close()
    asyncio.run(go())


def test_failed_credential_write_does_not_claim_enrollment(tmp_path, monkeypatch):
    def fail_write(*args, **kwargs):
        raise PermissionError("not writable")
    monkeypatch.setattr("duet.web.auth.atomic_write", fail_write)
    async def go():
        async with running(tmp_path) as (client, ui):
            response = await client.get("/?t=first-token")
            assert response.status == 503
            assert not any(name.startswith("duet_device_") for name in response.cookies)
            assert not (ui.cfg.dir / "private" / "web-devices.json").exists()
    asyncio.run(go())


def test_normalize_origin_matches_scheme_host_and_effective_port():
    assert normalize_origin("http://LOCALHOST") == "http://localhost:80"
    assert normalize_origin("https://localhost:443") == "https://localhost:443"
    assert normalize_origin("http://[::1]:8765") == "http://[::1]:8765"
    for value in ("null", "//host", "http://host/", "http://host?x", "http://user@host", "http://host:invalid", "http://host:0", "http://host:99999", "http://host\n"):
        assert normalize_origin(value) is None
