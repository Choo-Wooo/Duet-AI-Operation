#!/usr/bin/env bash
# ======================================================================
#  duet macOS · Linux 환경 점검·설치 (setup-windows.bat 과 같은 역할)
#
#    bash duet/setup.sh           점검하고, 빠진 것은 하나씩 물어본 뒤 설치
#    bash duet/setup.sh --check   점검만 (설치하지 않음)
#    bash duet/setup.sh --yes     묻지 않고 빠진 것을 모두 설치
#
#  점검 항목: Python 3.10+ 과 uv, Git(2.45+ 권장), Node.js,
#            claude / codex / agy CLI 설치·PATH·로그인, duet 가상환경(.duet/venv)
#  설치는 사용자 권한이 기본이다 (uv·공식 CLI 설치 스크립트·Homebrew).
#  Linux 에서 git·Node.js 를 배포판 패키지로 설치할 때만 sudo 를 쓴다.
# ======================================================================
set -u

MODE=ask
for a in "$@"; do
  case "$a" in
    --check|-c|/check) MODE=check ;;
    --yes|-y|/yes) MODE=yes ;;
    -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

DUET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$DUET_DIR/.." && pwd)"
OS="$(uname -s)"
FAIL=0
WARN=0
CLI_COUNT=0
RC_CHANGED=""
USER_PATH="$PATH"   # 사용자의 셸이 보는 PATH (이 실행이 덧붙인 폴더와 구분)

case "$OS" in
  Darwin|Linux) ;;
  *) echo "이 스크립트는 macOS · Linux 전용입니다. Windows 는 duet\\setup-windows.bat 을 실행하세요."; exit 1 ;;
esac

# 사용자 설치 폴더 (설치 스크립트들이 여기에 넣지만 PATH 에 없는 경우가 많다)
USER_BINS=("$HOME/.local/bin" "$HOME/.cargo/bin" "$HOME/.claude/local" "$HOME/.npm-global/bin" "$HOME/.bun/bin" "$HOME/.volta/bin")
[ "$OS" = Darwin ] && USER_BINS+=("/opt/homebrew/bin" "/usr/local/bin")

ok()   { echo "  [OK] $*"; }
warn() { echo "  [주의] $*"; WARN=$((WARN + 1)); }
fail() { echo "  [X]  $*"; FAIL=$((FAIL + 1)); }
info() { echo "       $*"; }

# ask "질문" → ANSWER=Y/N.  curl | bash 로 실행돼 표준 입력이 스크립트일 때도 터미널에서 읽는다.
ask() {
  ANSWER=N
  [ "$MODE" = check ] && return 0
  [ "$MODE" = yes ] && { ANSWER=Y; return 0; }
  local reply=""
  if [ -t 0 ]; then
    read -r -p "      $1 [y/N] " reply
  elif { : </dev/tty; } 2>/dev/null; then
    read -r -p "      $1 [y/N] " reply </dev/tty
  else
    info "(입력할 터미널이 없어 건너뜁니다: $1)"
  fi
  case "$reply" in y|Y|yes|YES|ㅛ) ANSWER=Y ;; esac
}

has() { command -v "$1" >/dev/null 2>&1; }

# locate 이름 → FOUND (PATH), OFF_PATH (PATH 밖의 알려진 폴더)
locate() {
  FOUND=""; OFF_PATH=""
  FOUND="$(PATH="$USER_PATH" command -v "$1" 2>/dev/null || true)"
  [ -n "$FOUND" ] && return 0
  local d dirs=("${USER_BINS[@]}")
  for d in "$HOME"/.nvm/versions/node/*/bin; do [ -d "$d" ] && dirs+=("$d"); done
  for d in "${dirs[@]}"; do
    if [ -x "$d/$1" ]; then OFF_PATH="$d/$1"; return 0; fi
  done
}

shell_rc() {
  case "$(basename "${SHELL:-}")" in
    zsh) echo "$HOME/.zshrc" ;;
    bash) if [ "$OS" = Darwin ]; then echo "$HOME/.bash_profile"; else echo "$HOME/.bashrc"; fi ;;
    *) echo "$HOME/.profile" ;;
  esac
}

# add_path 폴더 : 셸 설정 파일에 PATH 한 줄 추가 (이미 있으면 그대로), 이 실행에도 반영
add_path() {
  local dir="$1" rc line
  rc="$(shell_rc)"
  line="export PATH=\"${dir/#$HOME/\$HOME}:\$PATH\"  # duet setup"
  if ! grep -qsF "$line" "$rc"; then
    printf '\n%s\n' "$line" >> "$rc"
    RC_CHANGED="$rc"
  fi
  export PATH="$dir:$PATH"
  USER_PATH="$dir:$USER_PATH"
}

# 설치 직후 새로 생긴 사용자 폴더를 이 실행의 PATH 에 넣는다
refresh_path() {
  local d
  for d in "${USER_BINS[@]}"; do
    [ -d "$d" ] && case ":$PATH:" in *":$d:"*) ;; *) export PATH="$PATH:$d" ;; esac
  done
  [ -x /opt/homebrew/bin/brew ] && eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null
  hash -r
}

# 배포판 패키지 관리자로 설치 (sudo)
pkg_install() {
  local sudo=""
  [ "$(id -u)" -ne 0 ] && sudo="sudo"
  if has apt-get; then $sudo apt-get update -qq && $sudo apt-get install -y "$@"
  elif has dnf; then $sudo dnf install -y "$@"
  elif has pacman; then $sudo pacman -S --noconfirm "$@"
  elif has zypper; then $sudo zypper install -y "$@"
  elif has apk; then $sudo apk add "$@"
  else info "패키지 관리자를 찾지 못했습니다. 직접 설치하세요: $*"; return 1
  fi
}

run_script() {  # 공식 설치 스크립트 실행 (curl | sh)
  info "실행: curl -fsSL $1 | $2"
  curl -fsSL "$1" | "$2"
}

echo
echo " duet 환경 점검 (macOS · Linux)"
echo " duet 폴더   : $DUET_DIR"
echo " 프로젝트    : $PROJECT_DIR"
[ "$MODE" = check ] && echo " 모드        : 점검만"
[ "$MODE" = yes ] && echo " 모드        : 빠진 것 모두 자동 설치"
echo
[ "$(id -u)" -eq 0 ] && warn "root 로 실행 중입니다. CLI 로그인·설정이 root 계정에 따로 생기므로 일반 사용자로 실행하세요."
has curl || warn "curl 이 없습니다. 자동 설치를 쓰려면 curl 을 먼저 설치하세요."

# ---------------------------------------------------------------- Python · uv
echo "[1/6] Python 3.10 이상 · uv"
refresh_path
find_python() {
  PY=""; PYVER=""
  local c v
  for c in python3.13 python3.12 python3.11 python3.10 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    has "$c" || [ -x "$c" ] || continue
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      PY="$(command -v "$c" 2>/dev/null || echo "$c")"
      PYVER="$("$c" -c 'import platform; print(platform.python_version())')"
      return 0
    fi
  done
}
find_python
locate uv
if [ -n "$FOUND" ]; then
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')  - 의존성 설치가 빠르고, 필요하면 파이썬도 받아 씁니다"
else
  if [ -n "$OFF_PATH" ]; then
    warn "uv 가 $OFF_PATH 에 있지만 PATH 에 없습니다."
    ask "$(shell_rc) 에 $(dirname "$OFF_PATH") 를 PATH 로 추가할까요?"
    [ "$ANSWER" = Y ] && add_path "$(dirname "$OFF_PATH")" && ok "PATH 에 추가했습니다."
  else
    ANSWER=N
    [ "${DUET_NO_UV:-0}" = 1 ] || ask "uv 를 설치할까요? (권장, 사용자 폴더 ~/.local/bin 에 설치)"
    if [ "$ANSWER" = Y ] && has curl; then
      run_script https://astral.sh/uv/install.sh sh && refresh_path
      locate uv
      [ -n "$OFF_PATH" ] && add_path "$(dirname "$OFF_PATH")"
    fi
    if has uv; then ok "uv 설치됨"; else info "uv 없음 - 없어도 동작하지만 있으면 설치가 빠르고 파이썬 버전 문제를 피합니다."; fi
  fi
fi
if [ -z "$PY" ] && ! has uv; then
  ask "Python 3.10+ 이 없습니다. uv 를 설치해 파이썬 3.12 를 받을까요?"
  if [ "$ANSWER" = Y ] && has curl; then
    run_script https://astral.sh/uv/install.sh sh && refresh_path
  fi
fi
if [ -z "$PY" ] && has uv; then
  ask "uv 로 파이썬 3.12 를 받을까요?"
  if [ "$ANSWER" = Y ]; then
    uv python install 3.12 >/dev/null 2>&1 && PY="$(uv python find 3.12 2>/dev/null)" \
      && PYVER="$("$PY" -c 'import platform; print(platform.python_version())')"
  fi
fi
if [ -n "$PY" ]; then
  ok "Python $PYVER  - $PY"
elif has uv; then
  ok "Python 3.10+ 이 없지만 처음 실행할 때 uv 가 파이썬 3.12 를 받아 씁니다."
else
  fail "Python 3.10 이상도 uv 도 없습니다. curl -LsSf https://astral.sh/uv/install.sh | sh (권장) 또는 brew install python@3.12 / sudo apt install python3.12"
fi

# ---------------------------------------------------------------- Git
echo "[2/6] Git"
git_version() {
  GITVER=""; GITNUM=0
  has git || return 0
  # macOS 의 /usr/bin/git 은 개발자 도구가 없으면 설치 창만 띄우므로 버전 출력으로 확인
  GITVER="$(git --version 2>/dev/null | awk '{print $3}')"
  [ -z "$GITVER" ] && return 0
  local a b
  a="$(echo "$GITVER" | cut -d. -f1)"; b="$(echo "$GITVER" | cut -d. -f2)"
  GITNUM=$((a * 100 + b))
}
git_version
if [ -z "$GITVER" ]; then
  ask "Git 을 설치할까요?"
  if [ "$ANSWER" = Y ]; then
    if [ "$OS" = Darwin ]; then
      if has brew; then brew install git; else xcode-select --install; info "개발자 도구 설치 창을 마친 뒤 다시 실행하세요."; fi
    else
      pkg_install git
    fi
    refresh_path; git_version
  fi
elif [ "$GITNUM" -lt 245 ]; then
  warn "Git $GITVER - 하위 폴더 프로젝트의 병렬 작업에는 2.45 이상이 필요합니다."
  if [ "$OS" = Darwin ] && has brew; then
    ask "Homebrew 로 최신 Git 을 설치할까요?"
    [ "$ANSWER" = Y ] && { brew install git || brew upgrade git; refresh_path; git_version; }
  else
    info "배포판의 최신 git 으로 올리세요 (Ubuntu: sudo add-apt-repository ppa:git-core/ppa && sudo apt install git)."
  fi
fi
if [ -n "$GITVER" ]; then
  [ "$GITNUM" -ge 245 ] && ok "Git $GITVER"
  if [ -z "$(git config --global user.email 2>/dev/null)" ]; then
    info "전역 user.email 이 없어 duet 스냅샷 커밋은 'duet' 이름으로 기록됩니다 (git config --global user.email ...)."
  fi
else
  fail "Git 이 없습니다. macOS: xcode-select --install, Linux: sudo apt install git"
fi

# ---------------------------------------------------------------- Node.js
echo "[3/6] Node.js  - 디자이너의 브라우저 도구 npx, npm 으로 CLI 설치"
NODEVER="$(node --version 2>/dev/null || true)"
if [ -z "$NODEVER" ]; then
  ask "Node.js LTS 를 설치할까요?"
  if [ "$ANSWER" = Y ]; then
    if has brew; then brew install node
    elif [ "$OS" = Linux ]; then pkg_install nodejs npm
    else info "Homebrew 가 없습니다. https://nodejs.org 에서 설치하세요."
    fi
    refresh_path
    NODEVER="$(node --version 2>/dev/null || true)"
  fi
fi
if [ -n "$NODEVER" ]; then
  ok "Node.js $NODEVER"
else
  warn "Node.js 가 없습니다. 디자이너의 브라우저 도구(Playwright MCP)를 쓸 수 없습니다. https://nodejs.org"
fi

# ---------------------------------------------------------------- CLI
echo "[4/6] 에이전트 CLI  - 하나 이상 필요"

# find_cli 이름 → EXE (PATH 밖에 있으면 PATH 추가를 제안)
find_cli() {
  EXE=""
  locate "$1"
  if [ -n "$FOUND" ]; then EXE="$FOUND"; return 0; fi
  if [ -n "$OFF_PATH" ]; then
    warn "$1 이 $OFF_PATH 에 있지만 PATH 에 없습니다."
    ask "$(shell_rc) 에 $(dirname "$OFF_PATH") 를 PATH 로 추가할까요?"
    [ "$ANSWER" = Y ] && add_path "$(dirname "$OFF_PATH")" && info "PATH 에 추가했습니다."
    EXE="$OFF_PATH"
  fi
}

cli_version() { "$1" --version 2>/dev/null </dev/null | head -1; }

check_claude() {
  find_cli claude
  if [ -z "$EXE" ]; then
    ask "Claude Code 를 설치할까요? (공식 설치 스크립트)"
    if [ "$ANSWER" = Y ] && has curl; then run_script https://claude.ai/install.sh bash; refresh_path; find_cli claude; fi
  fi
  if [ -z "$EXE" ]; then
    warn "claude 없음 - Claude Code 역할을 쓸 수 없습니다. curl -fsSL https://claude.ai/install.sh | bash"
    return 0
  fi
  CLI_COUNT=$((CLI_COUNT + 1))
  local v; v="$(cli_version "$EXE")"
  if "$EXE" auth status 2>/dev/null </dev/null | grep -Eq '"loggedIn"[[:space:]]*:[[:space:]]*true'; then
    ok "claude $v - 로그인됨"
  else
    warn "claude $v - 로그인 안 됨. 터미널에서 claude 를 실행해 /login 하세요."
  fi
}

check_codex() {
  find_cli codex
  if [ -z "$EXE" ]; then
    ask "Codex CLI 를 설치할까요? (공식 설치 스크립트)"
    if [ "$ANSWER" = Y ]; then
      if has curl; then run_script https://chatgpt.com/codex/install.sh sh; fi
      refresh_path; find_cli codex
      if [ -z "$EXE" ] && has npm; then info "npm i -g @openai/codex"; npm i -g @openai/codex; refresh_path; find_cli codex; fi
    fi
  fi
  if [ -z "$EXE" ]; then
    warn "codex 없음 - Codex 역할을 쓸 수 없습니다. curl -fsSL https://chatgpt.com/codex/install.sh | sh 또는 npm i -g @openai/codex"
    return 0
  fi
  CLI_COUNT=$((CLI_COUNT + 1))
  local v; v="$(cli_version "$EXE")"
  if "$EXE" login status 2>&1 </dev/null | grep -q '^Logged in'; then
    ok "$v - 로그인됨"
  else
    warn "$v - 로그인 안 됨. 터미널에서 codex login 을 실행하세요."
  fi
}

check_agy() {
  find_cli agy
  if [ -z "$EXE" ]; then
    ask "Antigravity CLI(agy) 를 설치할까요? (공식 설치 스크립트)"
    if [ "$ANSWER" = Y ] && has curl; then run_script https://antigravity.google/cli/install.sh bash; refresh_path; find_cli agy; fi
  fi
  if [ -z "$EXE" ]; then
    warn "agy 없음 - Antigravity 역할을 쓸 수 없습니다. https://antigravity.google 안내에 따라 설치하세요."
    return 0
  fi
  CLI_COUNT=$((CLI_COUNT + 1))
  local v; v="$(cli_version "$EXE")"
  local to=""
  has timeout && to="timeout 60"
  if $to "$EXE" models </dev/null >/dev/null 2>&1; then
    ok "agy $v - 로그인됨"
  else
    warn "agy $v - 로그인 안 됨. 터미널에서 agy 를 실행해 로그인하세요."
  fi
}

check_claude
check_codex
check_agy
[ "$CLI_COUNT" -eq 0 ] && fail "claude / codex / agy 중 설치된 CLI 가 없습니다."

# ---------------------------------------------------------------- 가상환경
echo "[5/6] duet 가상환경  - $PROJECT_DIR/.duet/venv"
VENV="$PROJECT_DIR/.duet/venv"
if [ -x "$VENV/bin/python" ] && [ -f "$VENV/pyvenv.cfg" ] && [ -f "$VENV/.duet-requirements" ]; then
  ok "준비됨"
elif [ -z "$PY" ] && ! has uv; then
  fail "Python 이 없어 가상환경을 만들 수 없습니다."
else
  ask "가상환경을 만들고 의존성을 설치할까요? (1~2분)"
  if [ "$ANSWER" != Y ]; then
    warn "아직 없습니다. 처음 실행할 때 자동으로 만듭니다."
  else
    if [ -n "$PY" ]; then
      (cd "$PROJECT_DIR" && "$PY" "$DUET_DIR" --list-saves >/dev/null)
    else
      (cd "$PROJECT_DIR" && uv run --no-project --python 3.12 python "$DUET_DIR" --list-saves >/dev/null)
    fi
    RC=$?
    if [ -f "$VENV/.duet-requirements" ]; then
      ok "가상환경을 만들었습니다."
    else
      fail "가상환경을 만들지 못했습니다. 종료 코드 $RC  (원인 확인: python3 $(basename "$DUET_DIR") --doctor)"
    fi
  fi
fi

# ---------------------------------------------------------------- 요약
echo "[6/6] 요약"
echo
if [ "$FAIL" -gt 0 ]; then
  echo "  문제 ${FAIL}개, 주의 ${WARN}개 - 위의 [X] 항목을 해결한 뒤 다시 실행하세요."
else
  echo "  필수 항목 모두 준비됨. 주의 ${WARN}개"
  echo
  echo "  프로젝트 폴더에서 실행:"
  echo "    cd \"$PROJECT_DIR\""
  RUNPY=python3
  if [ -n "$PY" ]; then
    RUNPY="$(basename "$PY")"
    [ "$(command -v "$RUNPY" 2>/dev/null)" = "$PY" ] || RUNPY="$PY"
  fi
  echo "    $RUNPY $(basename "$DUET_DIR") --web"
fi
if [ -n "$RC_CHANGED" ]; then
  echo
  echo "  PATH 를 $RC_CHANGED 에 추가했습니다. 새 터미널을 열거나: source \"$RC_CHANGED\""
fi
echo
[ "$FAIL" -gt 0 ] && exit 1
exit 0
