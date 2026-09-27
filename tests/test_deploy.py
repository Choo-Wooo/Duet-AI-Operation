"""배포·환경 구성: doctor 점검, bootstrap 의 lock·uv 설치 경로, install.sh (macOS·Linux)."""
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from duet import bootstrap, doctor

DUET_DIR = Path(doctor.__file__).resolve().parent

pytestmark = pytest.mark.skipif(os.name == "nt", reason="macOS·Linux 전용")


def _fake_exe(d, name, body):
    p = d / name
    p.write_text("#!/bin/sh\n" + textwrap.dedent(body))
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return p


@pytest.fixture
def fake_path(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(doctor, "HOME", home)
    monkeypatch.setattr(doctor, "EXTRA_DIRS", [home / ".local" / "bin"])
    return bindir, home


# ------------------------------------------------------------------ doctor
def test_find_exe_outside_path(fake_path):
    bindir, home = fake_path
    local = home / ".local" / "bin"
    local.mkdir(parents=True)
    _fake_exe(local, "claude", "echo 1.0\n")
    on_path, off_path = doctor.find_exe("claude")
    assert on_path is None and off_path == str(local / "claude")


def test_cli_off_path_offers_path_fix(fake_path, monkeypatch):
    bindir, home = fake_path
    local = home / ".local" / "bin"
    local.mkdir(parents=True)
    _fake_exe(local, "codex", "echo codex-cli 0.9.1\n")
    monkeypatch.setenv("SHELL", "/bin/zsh")
    checks, exe = doctor.check_cli("codex", login=False)
    fail = [c for c in checks if c.status == doctor.WARN]
    assert fail and fail[0].action
    desc, fn = fail[0].action
    fn()
    fn()  # 두 번 실행해도 한 줄만
    rc = (home / ".zshrc").read_text()
    assert rc.count("# duet doctor") == 1 and "$HOME/.local/bin" in rc


def test_cli_missing_suggests_official_installer(fake_path):
    checks, exe = doctor.check_cli("agy", login=False)
    assert exe is None
    assert checks[0].status == doctor.INFO
    assert doctor.INSTALL["agy"] in checks[0].fix


def test_login_status_parsing(fake_path):
    bindir, _ = fake_path
    exe = _fake_exe(bindir, "claude", """\
        if [ "$1" = auth ]; then echo '{"loggedIn": false}'; fi
    """)
    assert doctor._login_status("claude", str(exe), 10)[0] == doctor.FAIL
    exe = _fake_exe(bindir, "claude", """\
        if [ "$1" = auth ]; then echo '{"loggedIn": true, "authMethod": "oauth_token"}'; fi
    """)
    assert doctor._login_status("claude", str(exe), 10)[0] == doctor.OK
    exe = _fake_exe(bindir, "codex", """\
        echo "Not logged in"; exit 1
    """)
    assert doctor._login_status("codex", str(exe), 10)[0] == doctor.FAIL


def test_roles_with_uninstalled_cli(tmp_path):
    (tmp_path / ".duet").mkdir()
    (tmp_path / ".duet" / "roles.yaml").write_text(
        "roles:\n  architect:\n    cli: claude\n  implementer:\n    cli: codex\n  researcher:\n    cli: agy\n")
    c = doctor.check_roles(tmp_path, ["claude", "codex"])
    assert c.status == doctor.FAIL and "agy" in c.detail
    assert doctor.check_roles(tmp_path, ["claude", "codex", "agy"]).status == doctor.OK


def test_apply_fixes_asks_and_dedupes():
    calls = []
    act = ("같은 일", lambda: calls.append(1) or "done")
    checks = [doctor.Check(doctor.FAIL, "a", action=act), doctor.Check(doctor.FAIL, "b", action=act),
              doctor.Check(doctor.OK, "c", action=("ok 는 건너뜀", lambda: calls.append(2) or ""))]
    assert doctor.apply_fixes(checks, ask=lambda q: "n", out=lambda s: None) == 0
    assert doctor.apply_fixes(checks, ask=lambda q: "y", out=lambda s: None) == 1
    assert calls == [1]


def test_report_counts_failures():
    lines = []
    fails = doctor.report([doctor.Check(doctor.OK, "x"), doctor.Check(doctor.FAIL, "y", fix=["do it"])],
                          out=lines.append)
    assert fails == 1 and any("do it" in l for l in lines)


def test_doctor_runs_with_system_python(tmp_path):
    """가상환경 없이, 의존성 없이 표준 라이브러리만으로 실행된다."""
    proj = tmp_path / "proj"
    proj.mkdir()
    link = proj / "duet"
    link.symlink_to(DUET_DIR)
    env = dict(os.environ, DUET_NO_VENV="0")
    p = subprocess.run([sys.executable, "-S", str(link), "--doctor", "--no-login"],
                       cwd=proj, capture_output=True, text=True, timeout=120, env=env)
    assert "duet 환경 점검" in p.stdout, p.stdout + p.stderr
    assert "운영체제" in p.stdout and "파이썬" in p.stdout
    assert not (proj / ".duet" / "venv").exists()


# ------------------------------------------------------------------ bootstrap
def test_lock_file_pins_every_requirement():
    req = (DUET_DIR / "requirements.txt").read_text().splitlines()
    names = [l.split(">")[0].split("=")[0].strip().lower() for l in req if l.strip() and not l.startswith("#")]
    lock = (DUET_DIR / "requirements.lock").read_text().lower()
    for n in names:
        assert "\n%s==" % n in "\n" + lock, n


def test_requirements_hash_changes_with_lock(tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("pyyaml>=6\n")
    h1 = bootstrap._requirements_hash(req)
    (tmp_path / "requirements.lock").write_text("pyyaml==6.0.2\n")
    assert bootstrap._requirements_hash(req) != h1


def test_install_cmd_prefers_uv(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: "/x/uv" if n == "uv" else None)
    monkeypatch.delenv("DUET_USE_PIP", raising=False)
    cmd = bootstrap._install_cmd(tmp_path / "py", tmp_path / "r.lock", "pip")
    assert cmd[:3] == ["/x/uv", "pip", "install"]
    monkeypatch.setenv("DUET_USE_PIP", "1")
    assert bootstrap._install_cmd(tmp_path / "py", tmp_path / "r.lock", "pip")[1:3] == ["-m", "pip"]
    assert bootstrap._install_cmd(tmp_path / "py", tmp_path / "r.lock", "uv")[0] == "/x/uv"


def test_install_falls_back_to_ranges(monkeypatch, tmp_path):
    (tmp_path / "requirements.txt").write_text("a\n")
    (tmp_path / "requirements.lock").write_text("a==1\n")
    ran = []

    def fake_run(cmd, check=False, **kw):
        ran.append(Path(cmd[-1]).name)
        if cmd[-1].endswith(".lock"):
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda n: "/x/uv" if n == "uv" else None)
    monkeypatch.setattr(bootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(bootstrap, "_say", lambda m: None)
    bootstrap._install(tmp_path / "venv", tmp_path / "requirements.txt", "uv")
    assert ran == ["requirements.lock", "requirements.txt"]


# ------------------------------------------------------------------ install.sh
def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def local_repo(tmp_path):
    """install.sh 가 내려받을 저장소 (현재 작업 트리를 복사해 커밋)."""
    repo = tmp_path / "remote"
    subprocess.run(["cp", "-R", str(DUET_DIR), str(repo)], check=True)
    for junk in (".git", ".pytest_cache"):
        subprocess.run(["rm", "-rf", str(repo / junk)], check=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("add", "-A", cwd=repo)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x", cwd=repo)
    return repo


def _run_install(cwd, repo, **env):
    e = dict(os.environ, DUET_REPO=str(repo), DUET_NO_UV="1", DUET_CHECK="1", **env)
    return subprocess.run(["bash", str(DUET_DIR / "install.sh")], cwd=cwd, capture_output=True,
                          text=True, timeout=240, env=e, stdin=subprocess.DEVNULL)


def test_install_sh_clones_and_runs_setup(tmp_path, local_repo):
    proj = tmp_path / "proj"
    proj.mkdir()
    p = _run_install(proj, local_repo)
    assert p.returncode == 0, p.stdout + p.stderr
    assert (proj / "duet" / "__main__.py").exists()
    assert "duet 환경 점검" in p.stdout and "[6/6] 요약" in p.stdout and "python3 duet --web" in p.stdout
    assert (proj / "duet" / "setup.sh").exists()
    # 두 번째 실행은 갱신만
    p = _run_install(proj, local_repo)
    assert p.returncode == 0 and "최신으로 갱신" in p.stdout


def test_install_sh_refuses_non_git_folder(tmp_path, local_repo):
    proj = tmp_path / "proj"
    (proj / "duet").mkdir(parents=True)
    p = _run_install(proj, local_repo)
    assert p.returncode != 0 and "git 저장소가 아닙니다" in p.stderr


# ------------------------------------------------------------------ setup.sh (setup-windows.bat 과 같은 역할)
FAKE_CLIS = {
    "claude": """case "$1" in --version) echo "9.9.9 (Claude Code)";; auth) echo '{"loggedIn": true}';; esac\n""",
    "codex": """case "$1" in --version) echo "codex-cli 1.0";; login) echo "Not logged in"; exit 1;; esac\n""",
    "agy": """case "$1" in --version) echo "1.2.3";; models) echo m1;; esac\n""",
}


@pytest.fixture
def setup_env(tmp_path):
    """setup.sh 를 격리해 돌릴 환경: 필요한 도구만 있는 PATH, 가짜 HOME, 준비된 가상환경."""
    import shutil
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for tool in ("python3", "git", "bash", "sh", "env", "awk", "cut", "grep", "sed", "head", "basename",
                 "dirname", "id", "uname", "cat", "timeout"):
        found = shutil.which(tool)
        if found:
            (bindir / tool).symlink_to(os.path.realpath(found))
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "duet").symlink_to(DUET_DIR)
    venv = proj / ".duet" / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    (venv / "pyvenv.cfg").write_text("home = x\n")
    (venv / ".duet-requirements").write_text("x\n")
    env = {"PATH": str(bindir), "HOME": str(home), "SHELL": "/bin/zsh", "LANG": "C.UTF-8"}

    def run(*args, clis=("claude", "codex", "agy"), where="local"):
        target = home / ".local" / "bin" if where == "local" else bindir
        for name in clis:
            _fake_exe(target, name, FAKE_CLIS[name])
        return subprocess.run(["bash", str(proj / "duet" / "setup.sh"), *args], cwd=proj, env=env,
                              capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
    return run, home


def test_setup_sh_check_reports_without_changing(setup_env):
    run, home = setup_env
    p = run("--check")
    assert p.returncode == 0, p.stdout + p.stderr
    out = p.stdout
    for step in ("[1/6]", "[2/6]", "[3/6]", "[4/6]", "[5/6]", "[6/6]"):
        assert step in out
    assert out.count("PATH 에 없습니다") >= 3        # 세 CLI 가 ~/.local/bin 에만 있음
    assert "codex-cli 1.0 - 로그인 안 됨" in out
    assert "claude 9.9.9 (Claude Code) - 로그인됨" in out and "agy 1.2.3 - 로그인됨" in out
    assert "[5/6]" in out and "준비됨" in out
    assert not (home / ".zshrc").exists()


def test_setup_sh_yes_adds_path_once(setup_env):
    run, home = setup_env
    for _ in range(2):
        p = run("--yes")
        assert p.returncode == 0, p.stdout + p.stderr
    rc = (home / ".zshrc").read_text()
    assert rc.count("# duet setup") == 1 and '$HOME/.local/bin' in rc


def test_setup_sh_on_path_and_no_cli_fails(setup_env):
    run, home = setup_env
    p = run("--check", clis=())
    assert p.returncode == 1 and "설치된 CLI 가 없습니다" in p.stdout
    p = run("--check", clis=("claude",), where="path")
    assert p.returncode == 0 and "PATH 에 없습니다" not in p.stdout.split("[4/6]")[1]
