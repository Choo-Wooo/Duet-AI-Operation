"""실행 환경 자동 준비 (표준 라이브러리만 사용).

1. 파이썬 3.10 이상을 찾는다 (현재 인터프리터 → PATH의 python3.1x → Homebrew → uv).
2. <프로젝트>/.duet/venv 가상환경을 만들고 requirements.txt 를 설치한다.
3. 설치 내용이 바뀌었을 때만 다시 설치한다 (해시 비교).
4. 그 가상환경의 파이썬으로 duet 을 다시 실행한다 (os.execv).
"""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

MIN_PY = (3, 10)
PREFERRED = ["3.13", "3.12", "3.11", "3.10"]


def _say(msg):
    print("[duet] " + msg, flush=True)


def _venv_python(venv):
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _py_version(exe):
    try:
        out = subprocess.run(
            [str(exe), "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
            capture_output=True, text=True, timeout=15,
        )
        if out.returncode != 0:
            return None
        major, minor = out.stdout.strip().split(".")
        return int(major), int(minor)
    except Exception:
        return None


def _find_python():
    """3.10 이상인 파이썬 실행 파일 경로를 돌려준다. 없으면 None."""
    candidates = [sys.executable]
    for v in PREFERRED:
        candidates.append(shutil.which("python" + v))
    candidates.append(shutil.which("python3"))
    for prefix in ("/opt/homebrew/bin", "/usr/local/bin"):
        for v in PREFERRED:
            candidates.append(prefix + "/python" + v)
    seen = set()
    for c in candidates:
        if not c or c in seen or not Path(c).exists():
            continue
        seen.add(c)
        ver = _py_version(c)
        if ver and ver >= MIN_PY:
            return c
    return None


def _requirements_hash(req_file):
    data = req_file.read_bytes() if req_file.exists() else b""
    return hashlib.sha256(data + sys.platform.encode()).hexdigest()[:16]


def _create_venv(venv):
    py = _find_python()
    if py:
        _say("가상환경을 만듭니다: %s (python %s)" % (venv, ".".join(map(str, _py_version(py)))))
        subprocess.run([py, "-m", "venv", str(venv)], check=True)
        return "pip"
    uv = shutil.which("uv")
    if uv:
        _say("파이썬 3.10+ 이 없어 uv 로 파이썬 3.12 가상환경을 만듭니다.")
        subprocess.run([uv, "venv", "--python", "3.12", str(venv)], check=True)
        return "uv"
    _say("파이썬 3.10 이상이 필요합니다. 다음 중 하나를 설치한 뒤 다시 실행하세요.")
    _say("  brew install python@3.12      (Homebrew)")
    _say("  curl -LsSf https://astral.sh/uv/install.sh | sh   (uv)")
    sys.exit(1)


def _install(venv, req_file, installer):
    vpy = _venv_python(venv)
    _say("의존성을 설치합니다 (처음 한 번만, 1~2분 걸릴 수 있습니다)...")
    if installer == "uv" or not _has_pip(vpy):
        uv = shutil.which("uv")
        if uv:
            cmd = [uv, "pip", "install", "--python", str(vpy), "-r", str(req_file)]
        else:
            subprocess.run([str(vpy), "-m", "ensurepip", "--upgrade"], check=True)
            cmd = [str(vpy), "-m", "pip", "install", "-q", "-r", str(req_file)]
    else:
        subprocess.run([str(vpy), "-m", "pip", "install", "-q", "--upgrade", "pip"], check=False)
        cmd = [str(vpy), "-m", "pip", "install", "-q", "-r", str(req_file)]
    subprocess.run(cmd, check=True)


def _has_pip(vpy):
    return subprocess.run([str(vpy), "-m", "pip", "--version"], capture_output=True).returncode == 0


def ensure_environment(duet_dir, project_dir, argv):
    if "--no-venv" in argv or os.environ.get("DUET_NO_VENV") == "1":
        return
    venv = project_dir / ".duet" / "venv"
    vpy = _venv_python(venv)
    req_file = duet_dir / "requirements.txt"
    marker = venv / ".duet-requirements"
    want = _requirements_hash(req_file) + '-py%d.%d' % sys.version_info[:2]

    inside = Path(sys.prefix).resolve() == venv.resolve()
    if inside and marker.exists() and marker.read_text().strip() == want:
        return

    try:
        installer = "pip"
        if not vpy.exists():
            venv.parent.mkdir(parents=True, exist_ok=True)
            installer = _create_venv(venv)
        if not marker.exists() or marker.read_text().strip() != want:
            _install(venv, req_file, installer)
            marker.write_text(want)
    except subprocess.CalledProcessError as e:
        _say("환경 준비에 실패했습니다: %s" % e)
        _say("문제가 계속되면 .duet/venv 폴더를 지우고 다시 실행하세요.")
        sys.exit(1)

    if inside:
        return
    os.environ["DUET_BOOTSTRAPPED"] = "1"
    args = [str(vpy), str(duet_dir)] + list(argv)
    os.execv(str(vpy), args)
