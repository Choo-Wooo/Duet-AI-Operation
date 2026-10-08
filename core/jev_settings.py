"""Jev configuration and private credentials, kept out of session snapshots."""
from __future__ import annotations

import json
import math
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from .storage import atomic_write

if TYPE_CHECKING:
    from .config import Config


JEV_DEFAULTS = {
    "jev_mode": "off",
    "jev_model": "jev-1.13.0",
    "jev_timeout_sec": 8,
    "jev_confidence_threshold": 0.8,
    "jev_choices": True,
    "jev_approvals": True,
    "jev_flow": True,
}
JEV_MODES = ("off", "shadow", "on")


def validate_settings(settings: dict) -> None:
    """Validate before writing any public configuration; never echo secret values."""
    if any(k.lower() in ("jev_api_key", "typesafe_api_key", "jev_key") for k in settings):
        raise ValueError("Jev API 키는 roles.yaml 대신 웹 설정의 API 키 입력 또는 TYPESAFE_API_KEY를 사용하세요")
    values = {**JEV_DEFAULTS, **settings}
    if values["jev_mode"] not in JEV_MODES:
        raise ValueError("settings.jev_mode는 off, shadow, on 중 하나입니다")
    model = values["jev_model"]
    if not isinstance(model, str) or not re.fullmatch(r"jev-[A-Za-z0-9._-]{1,80}", model):
        raise ValueError("settings.jev_model은 jev-로 시작하는 모델 ID여야 합니다")
    timeout = values["jev_timeout_sec"]
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 60:
        raise ValueError("settings.jev_timeout_sec는 1~60초여야 합니다")
    threshold = values["jev_confidence_threshold"]
    if type(threshold) not in (int, float) or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise ValueError("settings.jev_confidence_threshold는 0~1 사이의 숫자여야 합니다")
    for key in ("jev_choices", "jev_approvals", "jev_flow"):
        if type(values[key]) is not bool:
            raise ValueError(f"settings.{key}는 true/false여야 합니다")


def _path(cfg: Config, *, create: bool = False) -> Path:
    parent = cfg.dir / "private"
    path = parent / "jev.json"
    if cfg.dir.is_symlink() or parent.is_symlink() or path.is_symlink():
        raise ValueError("Jev 비밀 키 저장 경로에는 심볼릭 링크를 사용할 수 없습니다")
    if create:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            parent.chmod(0o700)
        ignore = parent / ".gitignore"
        if ignore.is_symlink():
            raise ValueError("Jev 비밀 키 저장 경로에는 심볼릭 링크를 사용할 수 없습니다")
        # Keep credentials untracked even in projects which do not ignore .duet.
        atomic_write(ignore, "*\n")
    return path


def _saved_key(cfg: Config) -> str | None:
    path = _path(cfg)
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 16_384:
            raise ValueError("Jev 비밀 키 파일 형식이 올바르지 않습니다")
        data = json.load(stream)
    value = data.get("api_key") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Jev 비밀 키 파일 형식이 올바르지 않습니다")
    return value.strip()


def get_api_key(cfg: Config) -> str | None:
    """Saved project credential takes precedence over TYPESAFE_API_KEY."""
    try:
        saved = _saved_key(cfg)
    except (ValueError, OSError, UnicodeError):
        return None
    return saved or os.environ.get("TYPESAFE_API_KEY", "").strip() or None


def key_status(cfg: Config) -> dict:
    """Only metadata may be serialized into UI state; no key fragments."""
    try:
        saved = _saved_key(cfg)
    except (ValueError, OSError, UnicodeError):
        return {"configured": False, "source": "unavailable"}
    if saved:
        return {"configured": True, "source": "saved"}
    if os.environ.get("TYPESAFE_API_KEY", "").strip():
        return {"configured": True, "source": "environment"}
    return {"configured": False, "source": "none"}


def set_api_key(cfg: Config, key: str) -> None:
    if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(c.isspace() for c in key.strip()):
        raise ValueError("API 키는 공백 없는 1~4096자 문자열로 입력하세요")
    path = _path(cfg, create=True)
    atomic_write(path, json.dumps({"api_key": key.strip()}, ensure_ascii=True))
    if os.name != "nt":
        path.chmod(0o600)


def clear_api_key(cfg: Config) -> None:
    """Remove only the private saved key; environment credentials are unchanged."""
    _path(cfg).unlink(missing_ok=True)
