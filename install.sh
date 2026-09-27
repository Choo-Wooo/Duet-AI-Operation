#!/usr/bin/env bash
# duet 설치 스크립트 (macOS · Linux)
#
#   curl -fsSL https://raw.githubusercontent.com/Choo-Wooo/Duet-AI-Operation/main/install.sh | bash
#
# 현재 폴더(작업할 프로젝트 폴더)에 ./duet 을 내려받고 duet/setup.sh 로 환경을 점검·설치한다.
# 환경 변수:
#   DUET_DIR     설치 위치 (기본: ./duet)
#   DUET_REF     브랜치·태그 (기본: main)
#   DUET_REPO    저장소 주소
#   DUET_NO_UV=1 uv 를 설치하지 않는다
#   DUET_FIX=1   빠진 것을 묻지 않고 모두 설치 (setup.sh --yes)
#   DUET_CHECK=1 점검만 하고 설치하지 않음 (setup.sh --check)
set -euo pipefail

REPO="${DUET_REPO:-https://github.com/Choo-Wooo/Duet-AI-Operation.git}"
REF="${DUET_REF:-main}"
DEST="${DUET_DIR:-duet}"

say() { printf '[duet] %s\n' "$*"; }
die() { printf '[duet] %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) die "이 스크립트는 macOS · Linux 전용입니다. Windows 는 저장소를 클론한 뒤 python duet 으로 실행하세요." ;;
esac

command -v git >/dev/null 2>&1 || {
  if [ "$(uname -s)" = "Darwin" ]; then die "git 이 없습니다: xcode-select --install 후 다시 실행하세요."; fi
  die "git 이 없습니다: sudo apt install git (Debian·Ubuntu) 또는 sudo dnf install git (Fedora) 후 다시 실행하세요."
}

# uv: 파이썬 3.10+ 이 없어도 받아 오고, 의존성 설치가 빠르다.
if ! command -v uv >/dev/null 2>&1 && [ "${DUET_NO_UV:-0}" != "1" ]; then
  for d in "$HOME/.local/bin" "$HOME/.cargo/bin"; do
    [ -x "$d/uv" ] && export PATH="$d:$PATH"
  done
fi
if ! command -v uv >/dev/null 2>&1 && [ "${DUET_NO_UV:-0}" != "1" ]; then
  if command -v curl >/dev/null 2>&1; then
    say "uv 를 설치합니다 (파이썬·의존성 관리)."
    curl -LsSf https://astral.sh/uv/install.sh | sh || say "uv 설치에 실패했습니다. 시스템 파이썬으로 계속합니다."
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
  else
    say "curl 이 없어 uv 설치를 건너뜁니다."
  fi
fi

if [ -d "$DEST/.git" ]; then
  say "$DEST 가 이미 있습니다. 최신으로 갱신합니다 ($REF)."
  git -C "$DEST" fetch --quiet origin "$REF"
  git -C "$DEST" checkout --quiet "$REF" 2>/dev/null || true
  git -C "$DEST" pull --quiet --ff-only origin "$REF" || say "로컬 변경이 있어 갱신하지 못했습니다. 그대로 둡니다."
elif [ -e "$DEST" ]; then
  die "$DEST 가 이미 있고 git 저장소가 아닙니다. 옮기거나 DUET_DIR 로 다른 위치를 지정하세요."
else
  say "duet 을 내려받습니다: $REPO ($REF) → $DEST"
  git clone --quiet --depth 1 --branch "$REF" "$REPO" "$DEST"
fi

SETUP_ARGS=()
[ "${DUET_FIX:-0}" = "1" ] && SETUP_ARGS+=(--yes)
[ "${DUET_CHECK:-0}" = "1" ] && SETUP_ARGS+=(--check)

say "환경을 점검하고 빠진 것을 설치합니다 (duet/setup.sh)."
set +e
bash "$DEST/setup.sh" ${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}
set -e

echo
say "설치가 끝났습니다."
say "  다시 점검·설치:  bash $DEST/setup.sh   (점검만: --check, 모두 자동: --yes)"
say "  자세한 진단:     python3 $DEST --doctor"
say "  실행:            python3 $DEST --web   (터미널 UI: python3 $DEST)"
