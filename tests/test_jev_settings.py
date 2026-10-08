"""JEV settings are persistent, but credentials never enter shared snapshots."""
import asyncio
import json
import os
import stat
import subprocess

import pytest
import yaml

from duet import commands
from duet.core.config import Config
from duet.core.jev_settings import clear_api_key, get_api_key, key_status, set_api_key
from duet.core.saves import write_save
from duet.tests.test_web import make_ui


def test_secret_storage_precedence_permissions_and_export(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-test-credential")
    cfg = Config(tmp_path)
    assert get_api_key(cfg) == "env-test-credential"
    assert key_status(cfg) == {"configured": True, "source": "environment"}
    set_api_key(cfg, "saved-test-credential")
    assert get_api_key(cfg) == "saved-test-credential"
    assert key_status(cfg) == {"configured": True, "source": "saved"}
    secret = cfg.dir / "private" / "jev.json"
    if os.name != "nt":
        assert stat.S_IMODE(secret.stat().st_mode) == 0o600
        assert stat.S_IMODE(secret.parent.stat().st_mode) == 0o700
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    ignored = subprocess.run(["git", "check-ignore", ".duet/private/jev.json"], cwd=tmp_path,
                             capture_output=True, text=True)
    assert ignored.returncode == 0
    cfg.save_roles()
    cfg.save_state()
    (tmp_path / "DIALOGUE.md").write_text("# DIALOGUE\n", encoding="utf-8")
    write_save(cfg, "example")
    exported = "".join(p.read_text() for p in (cfg.saves_dir / "example").rglob("*") if p.is_file())
    assert "saved-test-credential" not in exported
    assert "env-test-credential" not in exported
    assert not (cfg.saves_dir / "example" / "private").exists()
    clear_api_key(cfg)
    assert get_api_key(cfg) == "env-test-credential"
    monkeypatch.delenv("TYPESAFE_API_KEY")
    assert get_api_key(cfg) is None


@pytest.mark.symlink
def test_secret_paths_reject_symlinks(tmp_path):
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    target = tmp_path / "outside"
    target.mkdir()
    (cfg.dir / "private").symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="심볼릭"):
        set_api_key(cfg, "test-key")
    assert get_api_key(cfg) is None
    assert key_status(cfg)["source"] == "unavailable"
    assert not list(target.iterdir())


@pytest.mark.fifo
def test_secret_fifo_does_not_block(tmp_path):
    cfg = Config(tmp_path)
    private = cfg.dir / "private"
    private.mkdir(parents=True)
    os.mkfifo(private / "jev.json")
    assert get_api_key(cfg) is None
    assert key_status(cfg)["source"] == "unavailable"


@pytest.mark.parametrize("value", ["", "has whitespace", "x\ny", None, 123, "x" * 4097])
def test_key_validation_does_not_echo_input(tmp_path, value):
    cfg = Config(tmp_path)
    with pytest.raises(ValueError, match="API 키는 공백 없는"):
        set_api_key(cfg, value)
    assert not (cfg.dir / "private").exists()


@pytest.mark.parametrize("key,value", [
    ("jev_mode", "auto"), ("jev_timeout_sec", 0), ("jev_timeout_sec", float("nan")),
    ("jev_timeout_sec", True), ("jev_model", "https://example.com/steal"),
    ("jev_flow", "yes"), ("jev_choices", 1), ("jev_api_key", "not-for-yaml"),
    ("jev_confidence_threshold", -.01), ("jev_confidence_threshold", 1.01),
    ("jev_confidence_threshold", float("nan")), ("jev_confidence_threshold", float("inf")),
    ("jev_confidence_threshold", True), ("jev_confidence_threshold", "0.8"),
])
def test_config_rejects_invalid_jev_settings(tmp_path, key, value):
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles_file.write_text(yaml.safe_dump({"main": "architect", "roles": {"architect": {}},
                                             "settings": {key: value}}), encoding="utf-8")
    with pytest.raises(ValueError):
        cfg.load()


def test_confidence_default_loads_in_legacy_config(tmp_path):
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles_file.write_text(yaml.safe_dump({"main": "architect", "roles": {"architect": {}},
                                             "settings": {}}), encoding="utf-8")
    cfg.load()
    assert cfg.settings["jev_confidence_threshold"] == .8


def test_web_confidence_threshold_validation_and_persistence(tmp_path):
    async def run():
        ui = make_ui(tmp_path)
        try:
            assert ui.state()["settings"]["jev_confidence_threshold"] == .8
            for value in ("0", ".75", ".8", "1"):
                response = await ui.handle({"type": "setting", "key": "jev_confidence_threshold", "value": value})
                assert response["state"]["settings"]["jev_confidence_threshold"] == float(value)
                cfg = Config(tmp_path)
                cfg.load()
                assert cfg.settings["jev_confidence_threshold"] == float(value)
            for value in ("-.01", "1.01", "nan", "inf", "invalid", True):
                await ui.handle({"type": "setting", "key": "jev_confidence_threshold", "value": value})
                assert ui.cfg.settings["jev_confidence_threshold"] == 1
        finally:
            await ui.close()
            await ui.orch.close()
    asyncio.run(run())


def test_web_key_and_configuration_never_echo_secret(tmp_path, monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    async def run():
        ui = make_ui(tmp_path)
        try:
            key = "web-test-secret-unique"
            statuses = []
            ui.bus.subscribe(lambda ev: statuses.append(ev.data) if ev.kind == "status" else None)
            response = await ui.handle({"type": "jev_key", "action": "save", "key": key})
            assert get_api_key(ui.cfg) == key
            assert key not in json.dumps(response)
            assert key not in json.dumps(ui.state())
            assert key not in json.dumps(list(ui.history))
            assert key not in ui.cfg.roles_file.read_text()
            assert "api_key" not in ui.state()["settings"]
            assert ui.state()["jev"] == {"configured": True, "source": "saved"}
            assert statuses[-1]["jev"]["source"] == "saved"
            assert key not in json.dumps(statuses)
            result = await ui.handle({"type": "setting", "key": "jev_mode", "value": "shadow"})
            assert ui.cfg.settings["jev_mode"] == "shadow"
            loaded = Config(tmp_path)
            loaded.load()
            assert loaded.settings["jev_mode"] == "shadow"
            result = await ui.handle({"type": "setting", "key": "jev_timeout_sec", "value": "nan"})
            assert ui.cfg.settings["jev_timeout_sec"] == 8
            await ui.handle({"type": "setting", "key": "jev_model", "value": "bad model"})
            assert ui.cfg.settings["jev_model"] == "jev-1.13.0"
            await ui.handle({"type": "jev_key", "action": "clear"})
            assert not ui.state()["jev"]["configured"]
            await asyncio.sleep(.6)  # status broadcasts are coalesced at 500 ms
            assert statuses[-1]["jev"]["source"] == "none"
            result = await ui.handle({"type": "jev_test"})
            assert "먼저 JEV API 키" in result["text"]
        finally:
            await ui.close()
            await ui.orch.close()
    asyncio.run(run())


def test_tui_mode_setting_and_status_hide_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-key-private")

    async def run():
        ui = make_ui(tmp_path)
        try:
            result = await commands.handle(ui.orch, "/jev on")
            assert "적용" in result and "환경변수" in result
            assert "test-key-private" not in result
            assert ui.cfg.settings["jev_mode"] == "on"
            result = await commands.handle(ui.orch, "/jev test-key-private")
            assert "test-key-private" not in result
        finally:
            await ui.close()
            await ui.orch.close()
    asyncio.run(run())


def test_connection_check_uses_saved_key_and_never_exposes_errors(tmp_path, monkeypatch):
    from duet.core.jev import JevClient

    async def run():
        ui = make_ui(tmp_path)
        set_api_key(ui.cfg, "test-connection-secret")
        calls = []

        async def success(self):
            calls.append(self)
            return {"ok": True, "message": "연결 성공"}

        try:
            monkeypatch.setattr(JevClient, "test_connection", success)
            response = await ui.handle({"type": "jev_test"})
            assert response["jev_test"] == {"ok": True}
            assert len(calls) == 1 and ui.cfg.settings["jev_mode"] == "off"
            assert "test-connection-secret" not in json.dumps(response)

            async def failure(self):
                raise RuntimeError("test-connection-secret must not be echoed")

            monkeypatch.setattr(JevClient, "test_connection", failure)
            response = await ui.handle({"type": "jev_test"})
            assert response["jev_test"] == {"ok": False}
            assert "test-connection-secret" not in json.dumps(response)
        finally:
            await ui.close()
            await ui.orch.close()
    asyncio.run(run())


@pytest.mark.symlink
def test_web_file_browser_cannot_expose_credentials_via_symlink(tmp_path):
    ui = make_ui(tmp_path)
    set_api_key(ui.cfg, "private-key-value")
    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "innocent.md").symlink_to(ui.cfg.dir / "private" / "jev.json")
    response = ui._read_file("docs/innocent.md")
    assert response.get("error")
    assert "private-key-value" not in json.dumps(response)
