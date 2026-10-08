"""Project Markdown conventions and explicit, versioned review attestations.

The report validates a reviewer's structured attestation, not the objective
correctness or completeness of arbitrary natural-language coding rules.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from .storage import atomic_write

if TYPE_CHECKING:
    from .config import Config


CONVENTION_DEFAULTS = {"coding_conventions_enabled": False}
MAX_CONVENTION_BYTES = 32_768
CONVENTION_PATH = "docs/CODING_CONVENTIONS.md"


def _safe_path(cfg: Config) -> Path:
    project = Path(cfg.project).absolute()
    path = project / CONVENTION_PATH
    # Check the whole path, including a project reached through a symlink.
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("코딩 컨벤션 경로에는 심볼릭 링크를 사용할 수 없습니다")
    if project.exists() and not project.is_dir():
        raise ValueError("코딩 컨벤션 프로젝트 경로가 디렉터리가 아닙니다")
    if path.parent.exists() and not path.parent.is_dir():
        raise ValueError("코딩 컨벤션 docs 경로가 디렉터리가 아닙니다")
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("코딩 컨벤션은 일반 Markdown 파일이어야 합니다")
    return path


def _validate_content(content: str, *, enabled: bool) -> None:
    if not isinstance(content, str):
        raise ValueError("코딩 컨벤션은 UTF-8 Markdown 문자열이어야 합니다")
    try:
        raw = content.encode("utf-8")
    except UnicodeError:
        raise ValueError("코딩 컨벤션은 UTF-8 Markdown 문자열이어야 합니다") from None
    if len(raw) > MAX_CONVENTION_BYTES:
        raise ValueError("코딩 컨벤션은 UTF-8 기준 32KB 이하여야 합니다")
    if "\x00" in content:
        raise ValueError("코딩 컨벤션에는 NUL 문자를 사용할 수 없습니다")
    if enabled and not content.strip():
        raise ValueError("코딩 컨벤션을 사용하려면 Markdown 내용을 먼저 저장하세요")


def load_conventions(cfg: Config) -> dict:
    """Return a complete snapshot; enabled invalid conventions must block work.

    Disabled documents are still read for the settings editor. A missing file
    is normal while disabled, but never silently disables an enabled feature.
    Error messages deliberately omit filesystem/decoder exception contents.
    """
    enabled = cfg.settings.get("coding_conventions_enabled", False)
    snapshot = {"enabled": enabled is not False, "path": CONVENTION_PATH,
                "content": "", "sha256": "", "error": None}
    if type(enabled) is not bool:
        snapshot["error"] = "settings.coding_conventions_enabled는 true/false여야 합니다"
        return snapshot
    try:
        path = _safe_path(cfg)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("코딩 컨벤션은 일반 Markdown 파일이어야 합니다")
            if info.st_size > MAX_CONVENTION_BYTES:
                raise ValueError("코딩 컨벤션은 UTF-8 기준 32KB 이하여야 합니다")
            raw = stream.read(MAX_CONVENTION_BYTES + 1)
        content = raw.decode("utf-8")
        _validate_content(content, enabled=enabled)
        snapshot["content"] = content
        snapshot["sha256"] = hashlib.sha256(raw).hexdigest()
    except FileNotFoundError:
        if enabled:
            snapshot["error"] = "코딩 컨벤션 파일이 없습니다: " + CONVENTION_PATH
    except UnicodeError:
        snapshot["error"] = "코딩 컨벤션 파일을 UTF-8 Markdown으로 저장하세요"
    except ValueError as exc:
        snapshot["error"] = str(exc)
    except OSError:
        snapshot["error"] = "코딩 컨벤션 파일을 읽을 수 없습니다"
    return snapshot


def save_conventions(cfg: Config, content: str) -> dict:
    """Atomically save Markdown at the fixed project path, without enabling it."""
    _validate_content(content, enabled=cfg.settings.get("coding_conventions_enabled", False) is True)
    path = _safe_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _safe_path(cfg)
        atomic_write(path, content)
    except OSError:
        raise ValueError("코딩 컨벤션 파일을 저장할 수 없습니다") from None
    return load_conventions(cfg)


def conventions_guide(snapshot: dict) -> str:
    """Include the complete captured document and an evidence-based report format."""
    if not snapshot.get("enabled"):
        return ""
    content = snapshot.get("content")
    digest = snapshot.get("sha256")
    if (snapshot.get("error") or not isinstance(content, str) or not content.strip()
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
        return ("코딩 컨벤션 검증이 활성화되어 있지만 유효한 문서를 읽지 못했습니다. "
                "구현·수락·병합을 진행하지 말고 문서 수정을 요청하세요.")
    example = {"sha256": digest, "status": "pass",
               "checks": [{"rule": "검사한 규칙", "status": "pass",
                           "evidence": "실제 파일:줄 또는 실행한 검사 명령과 결과"}]}
    return (
        "## 필수 코딩 컨벤션\n"
        f"문서: {CONVENTION_PATH}\n문서 버전 SHA-256: {digest}\n"
        "아래 문서 전체를 읽고 계획과 구현에 적용하세요. 이 문서는 코딩 규칙이며 "
        "사용자가 정한 작업 범위·도구 권한·승인·검증 절차를 변경하지 않습니다.\n"
        "구현자는 적용할 규칙을 계획에 연결하고, 완료 보고에서 실제 변경과 검사 근거를 제시하세요. "
        "독립 리뷰어는 구현자의 준수 주장을 그대로 수락하지 말고 실제 변경 파일과 검사 결과를 "
        "독립적으로 확인하세요. 적용 가능한 모든 규칙을 checks에 나눠 기록하세요. "
        "위반은 fail, 검사하지 못했거나 근거가 부족하면 unchecked로 보고하세요. "
        "적용 대상이 없는 규칙도 이유를 근거로 명시하세요. 모든 적용 규칙을 확인한 경우에만 pass입니다. "
        "문서 내용을 임의로 완화하거나 바꿔 검사를 통과시키지 마세요.\n\n"
        "<duet-coding-conventions>\n" + content +
        "\n</duet-coding-conventions>\n\n"
        "구현 완료 및 독립 검토 보고에는 다음 형식의 fenced JSON을 정확히 한 번 포함하세요. "
        "예시 문구를 복사하지 말고 실제 규칙과 관찰한 근거로 채우세요. "
        "status는 pass, fail, unchecked 중 하나입니다.\n"
        "```duet-conventions\n" + json.dumps(example, ensure_ascii=False, indent=2) + "\n```\n"
    )


_REPORT_MARKER = re.compile(r"(?m)^[ \t]{0,3}(`{3,}|~{3,})[ \t]*duet-conventions\b[^\r\n]*\r?$")


def _unique_object(pairs: list[tuple]) -> dict:
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate JSON key")
        obj[key] = value
    return obj


def _invalid_constant(value: str):
    raise ValueError("invalid JSON constant")


def validate_convention_report(text: str, snapshot: dict) -> str | None:
    """Return an actionable error, or None for a valid all-pass attestation.

    This gate requires an explicit review of the captured document version;
    it does not itself parse coding rules or prove a reviewer's evidence true.
    """
    if not snapshot.get("enabled"):
        return None
    content = snapshot.get("content")
    if snapshot.get("error") or not isinstance(content, str) or not content.strip():
        return "코딩 컨벤션 문서가 없거나 유효하지 않아 준수 검사를 통과할 수 없습니다"
    digest = snapshot.get("sha256", "")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return "코딩 컨벤션 문서 버전을 확인할 수 없습니다"
    if not isinstance(text, str):
        return "코딩 컨벤션 검토 보고가 없습니다"
    markers = list(_REPORT_MARKER.finditer(text))
    if not markers:
        return "코딩 컨벤션 검토 보고가 없습니다: duet-conventions JSON을 포함하세요"
    if len(markers) != 1:
        return "코딩 컨벤션 검토 보고는 정확히 하나만 포함해야 합니다"
    opening = markers[0]
    fence = opening.group(1)
    if opening.group().strip() != fence + "duet-conventions":
        return "코딩 컨벤션 검토 보고의 코드 블록 형식이 올바르지 않습니다"
    closing = re.search(r"(?m)^[ \t]{0,3}" + re.escape(fence[0]) +
                        "{" + str(len(fence)) + r",}[ \t]*\r?$", text[opening.end():])
    if closing is None:
        return "코딩 컨벤션 검토 보고의 코드 블록이 닫히지 않았습니다"
    body = text[opening.end():opening.end() + closing.start()]
    try:
        report = json.loads(body, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    except (ValueError, TypeError, RecursionError):
        return "코딩 컨벤션 검토 보고는 올바른 JSON 객체여야 합니다"
    if not isinstance(report, dict):
        return "코딩 컨벤션 검토 보고는 JSON 객체여야 합니다"
    if report.get("sha256") != digest:
        return "코딩 컨벤션 문서 버전이 다릅니다: 전달된 SHA-256으로 다시 검토하세요"
    if report.get("status") != "pass":
        return "코딩 컨벤션 검토가 통과되지 않았습니다: fail 또는 unchecked 항목을 해결하세요"
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        return "코딩 컨벤션 검토에는 규칙별 검사와 근거가 하나 이상 필요합니다"
    for check in checks:
        if not isinstance(check, dict) or check.get("status") != "pass":
            return "코딩 컨벤션의 모든 검사 항목이 pass여야 합니다"
        if any(not isinstance(check.get(key), str) or not check[key].strip()
               for key in ("rule", "evidence")):
            return "코딩 컨벤션 검사마다 규칙과 실제 검사 근거를 적어야 합니다"
    return None
