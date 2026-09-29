"""환경 점검 (`python3 duet --doctor`, 고칠 수 있는 것은 `--doctor --fix`).

표준 라이브러리만 쓰고 파이썬 3.8 에서도 동작한다 (가상환경을 만들기 전에도 실행되도록).
macOS·Linux 를 대상으로 한다. Windows 지원은 작업 중이다.

점검: 운영체제, duet 을 돌릴 파이썬, uv, git, 프로젝트 저장소, Node(npx), 세 CLI(claude·codex·agy)의
설치·PATH·버전·로그인, 역할 설정과 설치된 CLI 의 일치, 가상환경, PyPI 연결.
"""
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()
OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
MARK = {OK: "[ OK ]", WARN: "[주의]", FAIL: "[필수]", INFO: "[참고]"}

# 공식 설치 명령 (macOS·Linux, 사용자 권한으로 설치)
INSTALL = {
    "claude": "curl -fsSL https://claude.ai/install.sh | bash",
    "codex": "curl -fsSL https://chatgpt.com/codex/install.sh | sh",
    "agy": "curl -fsSL https://antigravity.google/cli/install.sh | bash",
    "uv": "curl -LsSf https://astral.sh/uv/install.sh | sh",
}
LOGIN = {"claude": "claude   (처음 실행 시 브라우저 로그인, 또는 세션 안에서 /login)",
         "codex": "codex login", "agy": "agy   (처음 실행 시 브라우저 로그인)"}
ROLE_OF = {"claude": "설계자·디자이너", "codex": "구현자", "agy": "리서처"}
# CLI 설치 프로그램들이 쓰는 폴더 (PATH 에 없으면 찾지 못하는 경우가 많다)
EXTRA_DIRS = [HOME / ".local" / "bin", HOME / ".claude" / "local", HOME / ".npm-global" / "bin",
              HOME / ".bun" / "bin", HOME / ".volta" / "bin", HOME / ".cargo" / "bin",
              Path("/opt/homebrew/bin"), Path("/usr/local/bin")]


class Check:
    def __init__(self, status, title, detail="", fix=None, action=None):
        self.status, self.title, self.detail = status, title, detail
        self.fix = fix or []          # 사람이 실행할 명령
        self.action = action          # --fix 로 duet 이 대신 할 수 있는 일 (설명, 함수)


def _run(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return None, "시간 초과"
    except OSError as e:
        return None, str(e)


def _first_version(text):
    m = re.search(r"\d+\.\d+(?:\.\d+)?", text or "")
    return m.group(0) if m else ""


def _path_dirs():
    return [Path(p) for p in os.environ.get("PATH", "").split(os.pathsep) if p]


def find_exe(name):
    """(PATH 에서 찾은 경로, PATH 밖의 알려진 폴더에서 찾은 경로)"""
    on_path = shutil.which(name)
    if on_path:
        return on_path, None
    dirs = list(EXTRA_DIRS)
    nvm = HOME / ".nvm" / "versions" / "node"
    if nvm.is_dir():
        dirs += sorted(nvm.glob("*/bin"))
    for d in dirs:
        p = d / name
        if p.is_file() and os.access(str(p), os.X_OK):
            return None, str(p)
    return None, None


def _shell_rc():
    shell = os.path.basename(os.environ.get("SHELL", ""))
    if shell == "zsh":
        return HOME / ".zshrc"
    if shell == "bash":
        return HOME / (".bash_profile" if sys.platform == "darwin" else ".bashrc")
    return HOME / ".profile"


def _add_path_action(directory):
    rc = _shell_rc()
    line = 'export PATH="%s:$PATH"  # duet doctor' % directory.replace(str(HOME), "$HOME")

    def do():
        text = rc.read_text() if rc.exists() else ""
        if line not in text:
            with rc.open("a") as f:
                f.write(("\n" if text and not text.endswith("\n") else "") + line + "\n")
        return "%s 에 PATH 를 추가했습니다. 새 터미널을 열거나 `source %s` 하세요." % (rc, rc)
    return ("%s 에 %s 를 PATH 로 추가" % (rc, directory), do)


def _install_action(name):
    cmd = INSTALL[name]

    def do():
        rc = subprocess.call(cmd, shell=True)
        return "설치 명령을 실행했습니다 (종료 코드 %s). 새 터미널에서 다시 점검하세요." % rc
    return ("공식 설치 스크립트 실행: %s" % cmd, do)


# ---------------------------------------------------------------- 점검 항목
def check_os():
    sysname = platform.system()
    if sysname in ("Darwin", "Linux"):
        return Check(OK, "운영체제", "%s %s (%s)" % (sysname, platform.release(), platform.machine()))
    if sysname == "Windows":
        return Check(WARN, "운영체제", "Windows 지원은 작업 중입니다. 지금은 macOS·Linux 에서 쓰는 것을 권합니다.")
    return Check(WARN, "운영체제", sysname + " 는 확인하지 않은 환경입니다.")


def check_root():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return Check(WARN, "실행 권한", "root 로 실행 중입니다. CLI 로그인·설정이 root 계정에 따로 생기므로 일반 사용자로 실행하세요.")
    return None


def check_python(duet_dir):
    import importlib.util
    spec = importlib.util.spec_from_file_location("duet_bootstrap", str(Path(duet_dir) / "bootstrap.py"))
    boot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(boot)
    py = boot._find_python()
    uv = shutil.which("uv") or find_exe("uv")[1]
    if py:
        ver = ".".join(map(str, boot._py_version(py) or ()))
        return Check(OK, "파이썬", "%s (%s) — .duet/venv 가상환경을 이 파이썬으로 만듭니다" % (py, ver)), uv
    if uv:
        return Check(OK, "파이썬", "3.10 이상이 없지만 uv 가 파이썬 3.12 를 받아 가상환경을 만듭니다"), uv
    return Check(FAIL, "파이썬", "3.10 이상 파이썬도 uv 도 없습니다. uv 를 설치하면 파이썬을 자동으로 받아 씁니다.",
                 fix=[INSTALL["uv"], "또는: brew install python@3.12 / sudo apt install python3.12"],
                 action=_install_action("uv")), uv


def check_uv(uv):
    if uv:
        rc, out = _run([uv, "--version"])
        return Check(OK, "uv", "%s %s — 의존성 설치가 빠르고, 필요하면 파이썬도 받아 씁니다" % (uv, _first_version(out)))
    return Check(INFO, "uv (권장)", "없어도 동작하지만, 있으면 의존성 설치가 빠르고 파이썬 버전 문제를 피할 수 있습니다.",
                 fix=[INSTALL["uv"]], action=_install_action("uv"))


def check_git():
    git = shutil.which("git")
    if not git:
        fix = ["xcode-select --install   (또는 brew install git)"] if sys.platform == "darwin" else \
              ["sudo apt install git   (Debian·Ubuntu)", "sudo dnf install git   (Fedora·RHEL)"]
        return [Check(FAIL, "git", "git 이 없습니다. 턴 스냅샷·롤백·병렬 작업에 필요합니다.", fix=fix)]
    rc, out = _run([git, "--version"])
    ver = _first_version(out)
    out_checks = []
    major_minor = tuple(int(x) for x in ver.split(".")[:2]) if ver else (0, 0)
    if major_minor < (2, 45):
        out_checks.append(Check(WARN, "git", "%s — 하위 폴더 프로젝트의 병렬 작업 병합에는 2.45 이상이 필요합니다" % ver,
                                fix=["brew upgrade git" if sys.platform == "darwin" else "배포판의 최신 git 으로 업데이트"]))
    else:
        out_checks.append(Check(OK, "git", ver))
    rc, email = _run([git, "config", "--global", "user.email"])
    if not (email or "").strip():
        out_checks.append(Check(INFO, "git 사용자", "전역 user.email 이 없어 duet 스냅샷 커밋은 'duet' 이름으로 기록됩니다.",
                                fix=['git config --global user.name "이름"', 'git config --global user.email "메일"']))
    return out_checks


def check_project(project):
    git = shutil.which("git")
    if not git:
        return None
    rc, _ = _run([git, "-C", str(project), "rev-parse", "--is-inside-work-tree"])
    if rc != 0:
        return Check(INFO, "프로젝트 저장소", "%s 는 아직 git 저장소가 아닙니다. duet 을 처음 실행하면 git init 을 합니다." % project)
    rc, branch = _run([git, "-C", str(project), "symbolic-ref", "--quiet", "--short", "HEAD"])
    rc2, _ = _run([git, "-C", str(project), "rev-parse", "--verify", "-q", "HEAD"])
    if rc2 != 0:
        return Check(INFO, "프로젝트 저장소", "커밋이 아직 없습니다. 첫 턴이 끝나면 스냅샷 커밋이 생기고, 그 뒤 병렬 작업을 쓸 수 있습니다.")
    if rc != 0:
        return Check(WARN, "프로젝트 저장소", "HEAD 가 브랜치가 아닙니다(detached). 병렬 작업 전에 작업 브랜치를 체크아웃하세요.")
    return Check(OK, "프로젝트 저장소", "브랜치 %s" % branch.strip())


def check_node():
    npx = shutil.which("npx")
    if npx:
        rc, out = _run(["node", "--version"])
        return Check(OK, "Node.js (npx)", "%s — 디자이너 브라우저 도구(Playwright MCP) 사용 가능" % out.strip())
    fix = ["brew install node"] if sys.platform == "darwin" else ["sudo apt install nodejs npm   (또는 nvm)"]
    return Check(INFO, "Node.js (선택)", "없으면 디자이너의 브라우저 스크린샷 도구(Playwright MCP)를 쓸 수 없습니다.", fix=fix)


def _login_status(name, exe, timeout):
    if name == "codex":
        rc, out = _run([exe, "login", "status"], timeout)
        if rc == 0:
            return OK, out.strip().splitlines()[0] if out.strip() else "로그인됨"
        return (FAIL, "로그인이 필요합니다") if rc is not None else (WARN, "확인하지 못했습니다 (%s)" % out)
    if name == "claude":
        rc, out = _run([exe, "auth", "status"], timeout)
        m = re.search(r"\{.*\}", out or "", re.S)
        if m:  # {"loggedIn": true, "authMethod": ..., "apiProvider": ...}
            import json
            try:
                info = json.loads(m.group(0))
            except ValueError:
                info = {}
            if info.get("loggedIn") is False:
                return FAIL, "로그인이 필요합니다"
            if info.get("loggedIn"):
                how = info.get("authMethod") or ""
                note = " — API 키 과금일 수 있습니다" if "api" in how.lower() and "oauth" not in how.lower() else ""
                return (WARN if note else OK), "로그인됨 (%s)%s" % (how or "확인됨", note)
        if rc == 0:
            return OK, "로그인됨"
        if rc is None:
            return WARN, "확인하지 못했습니다 (%s)" % out
        if re.search(r"unknown|not a command|usage", out, re.I):
            return INFO, "이 버전은 로그인 확인 명령이 없습니다. duet 시작 시 계정 확인에서 표시됩니다."
        return FAIL, "로그인이 필요합니다"
    if name == "agy":
        rc, out = _run([exe, "models"], timeout)
        if rc == 0 and re.search(r"gemini|claude|gpt", out, re.I):
            n = len([l for l in out.splitlines() if re.match(r"^[a-z0-9][a-z0-9.\-_]+\s", l.strip())])
            return OK, "로그인됨 · 모델 %d개" % n
        if rc is None:
            return WARN, "확인하지 못했습니다 (%s). 처음 실행이면 agy 를 한 번 직접 실행해 로그인하세요." % out
        return FAIL, "로그인이 필요합니다"
    return INFO, ""


def check_cli(name, login=True, timeout=40):
    on_path, off_path = find_exe(name)
    title = "%s CLI (%s)" % (name, ROLE_OF[name])
    if not on_path and not off_path:
        return [Check(INFO, title, "설치되지 않았습니다. 이 CLI 를 쓰는 역할은 다른 CLI 로 대체됩니다.",
                      fix=[INSTALL[name], "설치 후 로그인: " + LOGIN[name]], action=_install_action(name))], None
    checks = []
    exe = on_path or off_path
    if off_path:
        d = str(Path(off_path).parent)
        checks.append(Check(WARN, title, "%s 에 설치돼 있지만 PATH 에 없습니다" % off_path,
                            fix=['echo \'export PATH="%s:$PATH"\' >> %s' % (d.replace(str(HOME), "$HOME"), _shell_rc())],
                            action=_add_path_action(d)))
    rc, out = _run([exe, "--version"])
    ver = _first_version(out)
    if not off_path:
        checks.append(Check(OK, title, "%s %s" % (exe, ver)))
    if login:
        st, msg = _login_status(name, exe, timeout)
        checks.append(Check(st, "%s 로그인" % name, msg, fix=[LOGIN[name]] if st in (FAIL, WARN) else None))
    return checks, exe


def check_roles(project, installed):
    f = Path(project) / ".duet" / "roles.yaml"
    if not f.exists():
        return None
    used = {}
    role = None
    for line in f.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            role = m.group(1)
        m = re.match(r"^    cli:\s*(\w+)", line)
        if m and role:
            used[role] = m.group(1)
    missing = ["%s(%s)" % (r, c) for r, c in used.items() if c not in installed]
    if missing:
        return Check(FAIL, "역할 설정", "설치되지 않은 CLI 를 쓰는 역할: %s. CLI 를 설치하거나 역할·모델 화면에서 CLI 를 바꾸세요."
                     % ", ".join(missing))
    return Check(OK, "역할 설정", ", ".join("%s=%s" % kv for kv in used.items()))


def check_venv(project, duet_dir):
    venv = Path(project) / ".duet" / "venv"
    marker = venv / ".duet-requirements"
    if not venv.exists():
        return Check(INFO, "가상환경", "아직 없습니다. 처음 실행할 때 .duet/venv 를 만들고 의존성을 설치합니다 (1~2분).")
    if not marker.exists():
        return Check(WARN, "가상환경", "의존성 설치가 끝나지 않았습니다. 다시 실행하면 이어서 설치합니다.",
                     fix=["rm -rf .duet/venv   (문제가 계속되면 지우고 다시 실행)"])
    return Check(OK, "가상환경", str(venv))


def check_network():
    import urllib.request
    try:
        urllib.request.urlopen("https://pypi.org/simple/pip/", timeout=6)
        return Check(OK, "네트워크", "PyPI 연결 가능")
    except Exception as e:  # noqa: BLE001
        return Check(WARN, "네트워크", "PyPI(pypi.org) 에 연결하지 못했습니다 (%s). 의존성 설치에 필요합니다. 프록시 설정을 확인하세요." % e)


# ---------------------------------------------------------------- 실행
def collect(duet_dir, project, login=True, network=True, timeout=40):
    checks = [check_os()]
    for c in (check_root(),):
        if c:
            checks.append(c)
    py, uv = check_python(duet_dir)
    checks += [py, check_uv(uv)]
    checks += check_git()
    c = check_project(project)
    if c:
        checks.append(c)
    checks.append(check_node())
    installed = []
    for name in ("claude", "codex", "agy"):
        cs, exe = check_cli(name, login=login, timeout=timeout)
        checks += cs
        if exe:
            installed.append(name)
    if not installed:
        checks.append(Check(FAIL, "CLI", "claude·codex·agy 중 하나 이상이 필요합니다.",
                            fix=[INSTALL["claude"], INSTALL["codex"], INSTALL["agy"]]))
    c = check_roles(project, installed)
    if c:
        checks.append(c)
    checks.append(check_venv(project, duet_dir))
    if network:
        c = check_network()
        if c:
            checks.append(c)
    return checks


def report(checks, out=print):
    out("duet 환경 점검")
    out("")
    for c in checks:
        out("%s %s: %s" % (MARK[c.status], c.title, c.detail))
        for f in c.fix:
            out("         → %s" % f)
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    out("")
    if fails:
        out("해결이 필요한 항목 %d개, 주의 %d개. 위의 → 명령을 실행하거나 `python3 duet --doctor --fix` 를 쓰세요." % (fails, warns))
    elif warns:
        out("실행할 수 있습니다. 주의 항목 %d개를 확인하세요." % warns)
    else:
        out("모두 준비됐습니다. `python3 duet --web` 으로 시작하세요.")
    return fails


def apply_fixes(checks, assume_yes=False, ask=input, out=print):
    done = 0
    seen = set()
    for c in checks:
        if not c.action or c.status == OK:
            continue
        desc, fn = c.action
        if desc in seen:
            continue
        seen.add(desc)
        if not assume_yes:
            try:
                ans = ask("%s — %s 할까요? [y/N] " % (c.title, desc)).strip().lower()
            except EOFError:
                ans = ""
            if ans not in ("y", "yes", "ㅛ"):
                continue
        try:
            out("  " + fn())
            done += 1
        except Exception as e:  # noqa: BLE001
            out("  실패: %s" % e)
    return done


def main(duet_dir, project, argv):
    fix = "--fix" in argv
    yes = "--yes" in argv or "-y" in argv
    login = "--no-login" not in argv
    checks = collect(duet_dir, project, login=login)
    fails = report(checks)
    if fix:
        print("")
        n = apply_fixes(checks, assume_yes=yes)
        print("고친 항목 %d개. 새 터미널에서 `python3 duet --doctor` 로 다시 점검하세요." % n if n else "자동으로 고칠 항목이 없습니다.")
    return 1 if fails else 0
