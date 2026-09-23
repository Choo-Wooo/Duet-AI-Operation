"""duet 진입점.

프로젝트 폴더 안에 이 폴더(duet/)를 넣고, 프로젝트 폴더에서 실행합니다.

    python3 duet            # 또는
    python3 -m duet

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


def main():
    _prepare_import_path()
    import importlib

    bootstrap = importlib.import_module(DUET_DIR.name + ".bootstrap")
    bootstrap.ensure_environment(DUET_DIR, PROJECT_DIR, sys.argv[1:])

    app = importlib.import_module(DUET_DIR.name + ".app")
    sys.exit(app.main(DUET_DIR, PROJECT_DIR, sys.argv[1:]))


if __name__ == "__main__":
    main()
