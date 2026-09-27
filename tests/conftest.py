"""플랫폼에 따라 돌릴 수 없는 시험을 건너뛴다 (Windows 의 심볼릭 링크 권한, FIFO, 오래된 git)."""
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest


def _can_symlink() -> bool:
    with tempfile.TemporaryDirectory() as d:
        try:
            (Path(d) / "link").symlink_to(Path(d) / "target")
            return True
        except (OSError, NotImplementedError):
            return False


def _git_version() -> tuple[int, ...]:
    try:
        out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    except OSError:
        return ()
    m = re.search(r"(\d+)\.(\d+)", out)
    return tuple(map(int, m.groups())) if m else ()


SKIPS = {
    "symlink": (_can_symlink(), "심볼릭 링크를 만들 수 없는 환경 (Windows 는 개발자 모드 또는 관리자 권한 필요)"),
    "fifo": (hasattr(os, "mkfifo"), "이 플랫폼에는 named pipe(FIFO) 파일이 없습니다"),
    "git238": (_git_version() >= (2, 38), "git 2.38 이상(merge-tree --write-tree)이 필요합니다"),
}


def pytest_configure(config):
    for name, (_, reason) in SKIPS.items():
        config.addinivalue_line("markers", f"{name}: {reason}")


def pytest_collection_modifyitems(config, items):
    for item in items:
        for name, (ok, reason) in SKIPS.items():
            if not ok and name in item.keywords:
                item.add_marker(pytest.mark.skip(reason=reason))
