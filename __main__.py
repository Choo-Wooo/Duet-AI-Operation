"""duet 진입점.

프로젝트 폴더 안에 이 폴더(duet/)를 넣고, 프로젝트 폴더에서 실행합니다.

    python3 duet            # 또는
    python3 -m duet
    python3 duet --doctor   # 환경 점검 (--fix: 고칠 수 있는 것 고치기)

처음 실행하면 .duet/venv 가상환경을 만들고 의존성을 설치한 뒤, 그 환경으로 다시 실행됩니다.
이 파일과 bootstrap.py 는 표준 라이브러리만 사용합니다(시스템 파이썬 3.8+에서도 동작).
"""
import sys
from pathlib import Path

DUET_DIR = Path(__file__).resolve().parent
PROJECT_DIR = DUET_DIR.parent


def _prepare_import_path():
    # `python3 duet` 로 실행하면 sys.path[0] 이 duet/ 자체가 된다.
    # 패키지(상대 import)로 불러오기 위해 부모 폴더를 쓰고, duet/ 는 경로에서 뺀다.
    here = str(DUET_DIR)
    sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != DUET_DIR]
    parent = str(PROJECT_DIR)
    if parent not in sys.path:
        sys.path.append(parent)
    return here


def _windows_console():
    """Windows: 출력이 파이프·파일로 갈 때도 한글·기호가 깨지거나 오류 나지 않게 UTF-8 로."""
    import os
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream and not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
            elif stream:
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass
    try:  # 콘솔 코드 페이지를 UTF-8 로 (자식 CLI 출력·ANSI 색 표시용)
        import atexit
        import ctypes
        k32 = ctypes.windll.kernel32
        old_out, old_in = k32.GetConsoleOutputCP(), k32.GetConsoleCP()
        if old_out and old_in:
            k32.SetConsoleOutputCP(65001)
            k32.SetConsoleCP(65001)
            atexit.register(lambda: (k32.SetConsoleOutputCP(old_out), k32.SetConsoleCP(old_in)))
        handle = k32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            k32.SetConsoleMode(handle, mode.value | 0x0004)  # ANSI 색 코드
    except Exception:
        pass


def main():
    _windows_console()
    _prepare_import_path()
    import importlib

    if "--doctor" in sys.argv[1:]:  # 가상환경을 만들기 전에 표준 라이브러리만으로 환경 점검
        doctor = importlib.import_module(DUET_DIR.name + ".doctor")
        sys.exit(doctor.main(DUET_DIR, PROJECT_DIR, sys.argv[1:]))

    bootstrap = importlib.import_module(DUET_DIR.name + ".bootstrap")
    bootstrap.ensure_environment(DUET_DIR, PROJECT_DIR, sys.argv[1:])

    app = importlib.import_module(DUET_DIR.name + ".app")
    sys.exit(app.main(DUET_DIR, PROJECT_DIR, sys.argv[1:]))


if __name__ == "__main__":
    main()
