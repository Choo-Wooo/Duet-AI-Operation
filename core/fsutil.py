"""파일 시스템 링크 다루기 (macOS·Linux·Windows 공통).

Windows 는 관리자 권한·개발자 모드 없이는 심볼릭 링크를 만들 수 없어서, 폴더 링크는
정션(junction)으로 만든다. 파이썬 3.10 의 Path.is_symlink() 는 정션을 링크로 보지 않으므로
링크 여부는 is_link() 로 확인한다.
"""
from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from .procs import WINDOWS, hidden_kwargs


def is_junction(path: Path | str) -> bool:
    if not WINDOWS:
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT) and \
        getattr(st, "st_reparse_tag", None) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)


def is_link(path: Path | str) -> bool:
    """심볼릭 링크 또는 (Windows) 정션."""
    return os.path.islink(path) or is_junction(path)


def link_dir(dst: Path, src: Path) -> None:
    """dst 에 src 폴더를 가리키는 링크를 만든다. Windows 는 심볼릭 링크가 안 되면 정션."""
    try:
        dst.symlink_to(src, target_is_directory=True)
        return
    except OSError:
        if not WINDOWS:
            raise
    try:
        import _winapi
        _winapi.CreateJunction(str(src), str(dst))
    except (ImportError, AttributeError, OSError):
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(src)], capture_output=True,
                           **hidden_kwargs())
        if r.returncode != 0 or not is_link(dst):
            raise OSError(f"폴더 링크를 만들지 못했습니다: {dst} → {src}")


def unlink_dir(path: Path) -> None:
    """폴더 링크만 지운다 (가리키는 원본 폴더의 내용은 건드리지 않는다)."""
    if not is_link(path):
        return
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)  # Windows 폴더 심볼릭 링크·정션은 rmdir 로 지운다
