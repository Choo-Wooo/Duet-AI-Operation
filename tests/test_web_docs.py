import asyncio
import os
import pytest
from aiohttp.test_utils import TestClient, TestServer
from duet.tests.test_web import make_ui
from duet.web.server import build_app


async def with_client(root, check):
    ui = make_ui(root)
    client = TestClient(TestServer(build_app(ui)))
    await client.start_server()
    try: await check(client, ui)
    finally:
        await ui.close()
        await ui.orch.close()
        await client.close()


def test_docs_access_sort_and_auth(tmp_path):
    included = ['docs/a.md', '.duet/memory/implementer.md', 'top.md']
    excluded = ['.git/a.md', 'node_modules/a.md', '.venv/a.md', 'duet/a.md', '.hidden/a.md',
                '.duet/venv/a.md', '.duet/work/a.md', 'docs/.secret/a.md']
    for n, p in enumerate(included + excluded):
        f = tmp_path / p; f.parent.mkdir(parents=True, exist_ok=True); f.write_text('# hello')
        os.utime(f, (100 + n, 100 + n))
    async def check(c, ui):
        for url in ['/api/docs', '/api/doc?path=top.md']:
            assert (await c.get(url)).status == 403
            assert (await c.get(url, params={'t': 'tok123'}, headers={'Origin': 'http://evil.invalid'})).status == 403
        r = await c.get('/api/docs?t=tok123'); assert r.status == 200
        rows = await r.json(); paths = [x['path'] for x in rows]
        assert all(p in paths for p in included) and not any(p in paths for p in excluded)
        assert [r['mtime'] for r in rows] == sorted([r['mtime'] for r in rows], reverse=True)
        for p in included + excluded:
            r = await c.get('/api/doc', params={'t':'tok123', 'path':p})
            assert r.status == (200 if p in included else 400)
        assert (await c.get('/static/md.js')).status == 200
    async def run():
        from duet.core.config import Config
        from duet.core.events import EventBus
        from duet.web.server import WebUI
        cfg = Config(tmp_path); cfg.settings['git_snapshots'] = False
        ui = WebUI(cfg, EventBus(), [], True, 'tok123')
        c = TestClient(TestServer(build_app(ui))); await c.start_server()
        try: await check(c, ui)
        finally: await ui.close(); await ui.orch.close(); await c.close()
    asyncio.run(run())


@pytest.mark.parametrize('path', ['../out.md', '/tmp/out.md', 'plain.txt', 'big.md', 'escape.md', 'missing.md', '.duet/state.md'])
def test_doc_rejects_bad_paths(tmp_path, path):
    (tmp_path/'plain.txt').write_text('x')
    (tmp_path/'big.md').write_bytes(b'x' * (1024*1024+1))
    (tmp_path/'escape.md').symlink_to(tmp_path.parent/'out.md')
    async def check(c, ui):
        r = await c.get('/api/doc', params={'t':'tok123', 'path':path})
        assert r.status in (400,404)
    asyncio.run(with_client(tmp_path, check))


def test_docs_cannot_bypass_exclusions_with_internal_symlinks(tmp_path):
    (tmp_path/'docs').mkdir()
    (tmp_path/'docs/a.md').write_text('allowed')
    (tmp_path/'.secret').mkdir()
    (tmp_path/'.secret/a.md').write_text('hidden')
    (tmp_path/'alias').symlink_to(tmp_path/'docs', target_is_directory=True)
    (tmp_path/'secret.md').symlink_to(tmp_path/'.secret/a.md')
    async def check(c, ui):
        rows = await (await c.get('/api/docs?t=tok123')).json()
        assert 'alias/a.md' not in [x['path'] for x in rows]
        for path in ['alias/a.md','secret.md']:
            assert (await c.get('/api/doc', params={'t':'tok123','path':path})).status == 400
    asyncio.run(with_client(tmp_path, check))


def test_docs_limit_and_size_boundary(tmp_path):
    for i in range(2002):
        p = tmp_path/f'{i:04}.md'; p.write_text('x'); os.utime(p, (i,i))
    (tmp_path/'limit.md').write_bytes(b'x'*(1024*1024))
    async def check(c, ui):
        rows = await (await c.get('/api/docs?t=tok123')).json()
        assert len(rows) == 2000 and '0000.md' not in [r['path'] for r in rows]
        r = await c.get('/api/doc?t=tok123&path=limit.md')
        assert r.status == 200 and len((await r.json())['content']) == 1024*1024
    asyncio.run(with_client(tmp_path, check))


@pytest.mark.parametrize('name', ['bad name', 'équipe', 'x'*33, 'a#b', '', '<script>'])
def test_role_validation_ws(tmp_path, name):
    async def check(c, ui):
        ws = await c.ws_connect('/ws?t=tok123'); await ws.receive_json()
        async def call(name, rid):
            await ws.send_json({'type':'role_add','name':name,'cli':'claude','rid':rid})
            while True:
                m = await ws.receive_json(timeout=3)
                if m.get('rid') == rid: return m
        bad = await call(name, 'bad')
        assert bad['status'] == 400 and name not in ui.cfg.roles
        good = await call('정상_역할-1', 'good')
        assert '정상_역할-1' in ui.cfg.roles
        await ws.close()
    asyncio.run(with_client(tmp_path, check))
