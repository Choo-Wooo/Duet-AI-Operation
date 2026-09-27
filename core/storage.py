"""Atomic runtime file writes and explicit recovery of malformed JSON."""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from uuid import uuid4


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         prefix=path.name + '.', suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_json(path: Path, warn):
    try:
        text = path.read_text(encoding='utf-8')
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError('JSON root must be an object')
        return data
    except FileNotFoundError:
        return None
    except (UnicodeDecodeError, ValueError):
        backup = path.with_name(f'{path.name}.corrupt-{time.time_ns()}-{uuid4().hex[:8]}')
        os.replace(path, backup)
        warn(f'JSON 손상: {path.name}을 {backup.name}에 백업했습니다.')
        return None
