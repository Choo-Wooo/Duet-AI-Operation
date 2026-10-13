"""Project-scoped, revocable browser credentials for the web UI.

The URL token enrolls a browser. Only a hash of its independent random cookie
credential is persisted; addresses and user agents never grant access.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit

from ..core.fsutil import is_link
from ..core.storage import atomic_write

TRUST_DAYS = 180
TRUST_SECONDS = TRUST_DAYS * 24 * 60 * 60
MAX_DEVICES = 128
MAX_STORAGE_BYTES = 256 * 1024


class AuthStorageError(Exception):
    """The credential registry is unavailable; never silently replace it."""


def normalize_origin(value: str) -> str | None:
    """Compare actual HTTP origins, including scheme and effective port."""
    try:
        if not value or any(c.isspace() for c in value):
            return None
        parts = urlsplit(value)
        if (parts.scheme not in ("http", "https") or not parts.hostname or
                parts.username is not None or parts.password is not None or
                parts.path or parts.query or parts.fragment):
            return None
        host = parts.hostname.lower()
        port = parts.port if parts.port is not None else (443 if parts.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            return None
        if ":" in host:
            host = f"[{host}]"
        return f"{parts.scheme}://{host}:{port}"
    except ValueError:
        return None


def browser_label(user_agent: str) -> str:
    """A short display hint only, never an authentication factor."""
    ua = user_agent[:2048].lower()
    device = ("iPad" if "ipad" in ua else "iPhone" if "iphone" in ua else
              "Android" if "android" in ua else "Mac" if "macintosh" in ua else
              "Windows" if "windows" in ua else "Linux" if "linux" in ua else "브라우저")
    browser = ("Edge" if "edg" in ua else "Firefox" if "firefox" in ua or "fxios" in ua else
               "Chrome" if "chrome" in ua or "crios" in ua else "Safari" if "safari" in ua else "")
    return f"{device} · {browser}" if browser else device


class DeviceRegistry:
    def __init__(self, project: Path, config_dir: Path):
        self.project = str(project.resolve())
        self.config_dir = config_dir
        self.folder = config_dir / "private"
        self.path = self.folder / "web-devices.json"

    def cookie_name(self, origin: str) -> str:
        # Cookies ignore ports. Include both project and origin so separate Duet
        # projects on the same Tailscale host do not overwrite each other's login.
        namespace = hashlib.sha256(f"{self.project}\n{origin}".encode()).hexdigest()[:24]
        return "duet_device_" + namespace

    def _check_paths(self) -> None:
        if any(is_link(p) for p in (self.config_dir, self.folder, self.path,
                                    self.folder / ".gitignore", self.folder / "web-devices.lock")):
            raise AuthStorageError("브라우저 등록 경로에는 심볼릭 링크를 사용할 수 없습니다.")

    def _prepare(self) -> None:
        self._check_paths()
        self.folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.folder.chmod(0o700)
        atomic_write(self.folder / ".gitignore", "*\n")

    @contextmanager
    def _locked(self):
        """Keep concurrent Duet servers for one project from losing revocations."""
        fd = None
        locked = False
        try:
            self._prepare()
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            fd = os.open(self.folder / "web-devices.lock", flags, 0o600)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise AuthStorageError("브라우저 등록 잠금 파일이 올바르지 않습니다.")
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX)
            locked = True
            yield
        except (OSError, ValueError, UnicodeError) as exc:
            raise AuthStorageError("브라우저 등록 파일을 읽거나 저장할 수 없습니다.") from exc
        finally:
            if fd is not None:
                try:
                    if locked:
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def _load(self) -> list[dict]:
        try:
            self._check_paths()
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            try:
                fd = os.open(self.path, flags)
            except FileNotFoundError:
                return []
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STORAGE_BYTES:
                    raise ValueError("invalid registry file")
                data = json.load(stream)
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("devices"), list):
                raise ValueError("invalid registry schema")
            devices = data["devices"]
            if len(devices) > MAX_DEVICES:
                raise ValueError("too many devices")
            seen = set()
            for row in devices:
                if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or
                        not re.fullmatch(r"[0-9a-f]{24}", row["id"]) or row["id"] in seen or
                        not isinstance(row.get("credential_hash"), str) or
                        not re.fullmatch(r"[0-9a-f]{64}", row["credential_hash"]) or
                        not isinstance(row.get("origin"), str) or normalize_origin(row["origin"]) != row["origin"] or
                        not isinstance(row.get("label"), str) or len(row["label"]) > 100):
                    raise ValueError("invalid device")
                for field in ("created_at", "last_seen", "expires_at"):
                    value = row.get(field)
                    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                        raise ValueError("invalid timestamp")
                if row["expires_at"] <= row["created_at"] or row["last_seen"] < row["created_at"]:
                    raise ValueError("invalid timestamp order")
                seen.add(row["id"])
            return devices
        except (OSError, ValueError, UnicodeError) as exc:
            raise AuthStorageError("브라우저 등록 파일이 손상되었거나 읽을 수 없습니다.") from exc

    def _write(self, devices: list[dict]) -> None:
        self._check_paths()
        atomic_write(self.path, json.dumps({"version": 1, "devices": devices}, ensure_ascii=False))
        if os.name != "nt":
            self.path.chmod(0o600)

    @staticmethod
    def public(row: dict, current_id: str | None = None) -> dict:
        return {**{key: row[key] for key in ("id", "label", "created_at", "last_seen", "expires_at")},
                "current": row["id"] == current_id}

    def authenticate(self, credential: str, origin: str) -> dict | None:
        # Always read the registry, including for malformed/missing credentials,
        # so corruption cannot fall back to a new empty whitelist.
        devices = self._load()
        if not isinstance(credential, str) or not re.fullmatch(r"[0-9a-f]{24}\.[A-Za-z0-9_-]{43}", credential):
            return None
        device_id = credential.split(".", 1)[0]
        digest = hashlib.sha256(credential.encode()).hexdigest()
        now = time.time()
        return next((row for row in devices if row["id"] == device_id and row["origin"] == origin and
                     row["expires_at"] > now and secrets.compare_digest(row["credential_hash"], digest)), None)

    def touch(self, device_id: str) -> None:
        now = time.time()
        with self._locked():
            rows = self._load()
            row = next((item for item in rows if item["id"] == device_id), None)
            if row and row["expires_at"] > now and now - row["last_seen"] >= 3600:
                row["last_seen"] = now
                self._write(rows)

    def enroll(self, origin: str, user_agent: str) -> tuple[str, dict]:
        now = time.time()
        with self._locked():
            rows = [row for row in self._load() if row["expires_at"] > now]
            if len(rows) >= MAX_DEVICES:
                raise AuthStorageError("등록된 브라우저가 너무 많습니다. 설정에서 사용하지 않는 브라우저를 해제하세요.")
            device_id = secrets.token_hex(12)
            credential = device_id + "." + secrets.token_urlsafe(32)
            row = {"id": device_id, "credential_hash": hashlib.sha256(credential.encode()).hexdigest(),
                   "origin": origin, "label": browser_label(user_agent), "created_at": now,
                   "last_seen": now, "expires_at": now + TRUST_SECONDS}
            rows.append(row)
            self._write(rows)
        return credential, row

    def list(self, current_id: str | None) -> list[dict]:
        return [self.public(row, current_id) for row in sorted(self._load(), key=lambda row: row["last_seen"], reverse=True)
                if row["expires_at"] > time.time()]

    def revoke(self, device_id: str) -> bool:
        with self._locked():
            rows = self._load()
            remaining = [row for row in rows if row["id"] != device_id]
            if len(rows) == len(remaining):
                return False
            self._write(remaining)
            return True
