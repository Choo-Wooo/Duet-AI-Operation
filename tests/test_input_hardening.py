"""B2-12: external role names only; subprocess installation is mocked."""
import asyncio
from types import SimpleNamespace
import pytest
from duet.core.config import Config, Role
from duet.tui.app import _safe_id
from duet import bootstrap, commands
from duet.tests.test_agreement import orch


@pytest.mark.parametrize('name', ['../../x', 'bad.name', 'a'*33, 'x#id', 'bad name', ''])
def test_invalid_role_registration_and_load(orch, name):
    before = dict(orch.cfg.roles)
    with pytest.raises(ValueError): orch.add_role(Role(name, 'claude'))
    assert orch.cfg.roles == before
    orch.cfg.roles[name] = Role(name, 'claude')
    orch.cfg.save_roles()
    with pytest.raises(ValueError): orch.cfg.load()


def test_internal_session_and_valid_role_names(orch):
    for name in ['연구자', 'A_1-x', 'a'*32]:
        orch.add_role(Role(name, 'claude'))
    assert orch.role_for('연구자#task1').name == '연구자#task1'


def test_invalid_proposed_role_rejected_before_approval(orch):
    async def run():
        await orch._propose_role('../../escape claude model description')
        assert not orch.ui.questions
        assert '../../escape' not in orch.cfg.roles
        assert any('../../escape' in e.data.get('text', '') for e in orch.events)
    asyncio.run(run())


def test_config_invalid_role_error_names_offender(orch):
    orch.cfg.roles['bad.name'] = Role('bad.name', 'claude')
    orch.cfg.save_roles()
    with pytest.raises(ValueError, match='bad.name'): orch.cfg.load()


def test_tui_ids_distinguish_korean_roles():
    names = ['연구자', '설계자', '가', '나', '가#1']
    ids = [_safe_id(n) for n in names]
    assert len(set(ids)) == len(names)
    assert all(i.startswith('r-') and i.isascii() for i in ids)
    assert ids == [_safe_id(n) for n in names]


def test_bootstrap_marker_changes_with_python_version(tmp_path, monkeypatch):
    root = tmp_path/'tool'; root.mkdir()
    (root/'requirements.txt').write_text('pytest')
    venv = tmp_path/'.duet/venv'; venv.mkdir(parents=True)
    vpy = venv/'python'; vpy.touch()
    monkeypatch.setattr(bootstrap, '_venv_python', lambda _: vpy)
    monkeypatch.setattr(bootstrap.sys, 'prefix', str(venv))
    installs = []
    monkeypatch.setattr(bootstrap, '_install', lambda *a: installs.append(a))
    monkeypatch.delenv('DUET_NO_VENV', raising=False)
    monkeypatch.setattr(bootstrap.sys, 'version_info', (3, 12))
    bootstrap.ensure_environment(root, tmp_path, [])
    first = (venv/'.duet-requirements').read_text()
    monkeypatch.setattr(bootstrap.sys, 'version_info', (3, 13))
    bootstrap.ensure_environment(root, tmp_path, [])
    assert len(installs) == 2
    assert (venv/'.duet-requirements').read_text() != first
