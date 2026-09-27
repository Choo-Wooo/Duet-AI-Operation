#!/usr/bin/env bash
# duet 설치 스크립트 (macOS · Linux)
#
#   curl -fsSL https://raw.githubusercontent.com/Choo-Wooo/Duet-AI-Operation/main/install.sh | bash
#
# 현재 폴더(작업할 프로젝트 폴더)에 ./duet 을 내려받고 환경을 점검한다.
# 환경 변수:
#   DUET_DIR     설치 위치 (기본: ./duet)
#   DUET_REF     브랜치·태그 (기본: main)
#   DUET_REPO    저장소 주소
#   DUET_NO_UV=1 uv 를 설치하지 않는다
#   DUET_FIX=1   점검 후 고칠 수 있는 항목을 묻지 않고 고친다 (--doctor --fix --yes)
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

PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

DOCTOR_ARGS=(--doctor)
[ "${DUET_FIX:-0}" = "1" ] && DOCTOR_ARGS+=(--fix --yes)

say "환경을 점검합니다."
echo
set +e
if [ -n "$PY" ]; then
  "$PY" "$DEST" "${DOCTOR_ARGS[@]}"
elif command -v uv >/dev/null 2>&1; then
  uv run --no-project --python 3.12 python "$DEST" "${DOCTOR_ARGS[@]}"
else
  say "파이썬이 없습니다. uv 를 설치하거나 python3 (3.10+) 를 설치하세요."
fi
set -e

echo
say "설치가 끝났습니다. 다음 순서로 시작하세요."
say "  1) 위 점검에 [필수] 항목이 있으면 안내된 명령을 실행하거나: python3 $DEST --doctor --fix"
say "  2) 웹 UI:   python3 $DEST --web"
say "     터미널:  python3 $DEST"
say "  처음 실행할 때 .duet/venv 에 의존성을 설치합니다 (1~2분)."
