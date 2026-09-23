"""이름 붙인 대화 스냅샷. CLI 목록 조회도 프로젝트 초기화 없이 사용한다."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import yaml

from .. import __version__
from .config import Config, State
from .agreement import validate_task

NAME_RE = re.compile(r"[A-Za-z0-9가-힣_.-]{1,64}")


def save_path(cfg: Config, name: str) -> Path:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name) or name in (".", ".."):
        raise ValueError("저장 이름은 한글·영문·숫자·_·-·. 1~64자여야 합니다 (. 및 .. 제외).")
    path = cfg.saves_dir / name
    if cfg.saves_dir.is_symlink() or path.is_symlink():
        raise ValueError("저장 경로에 심볼릭 링크를 사용할 수 없습니다.")
    return path


def git_head(project: Path) -> str | None:
    try:
        r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=project,
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def list_saves(cfg: Config) -> list[dict]:
    if not cfg.saves_dir.exists():
        return []
    if cfg.saves_dir.is_symlink():
        raise ValueError("저장 경로에 심볼릭 링크를 사용할 수 없습니다.")
    items = []
    for path in cfg.saves_dir.iterdir():
        try:
            save_path(cfg, path.name)
            meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
            if not isinstance(meta, dict) or not isinstance(meta.get("created_at"), str):
                continue
            items.append({**meta, "name": path.name})
        except (OSError, ValueError):
            continue
    return sorted(items, key=lambda m: (m["created_at"], m["name"]), reverse=True)


def format_saves(items: list[dict]) -> str:
    if not items:
        return "저장된 세션이 없습니다. /save [이름] 으로 저장하세요."
    return "저장된 세션 (최신순)\n" + "\n".join(
        f"  {m['name']} · {m['created_at']} · 턴 #{m.get('last_n', '?')} · "
        f"{m.get('mode', '?')}" + (f" · {m['note']}" if m.get("note") else "") for m in items)


def write_save(cfg: Config, name: str, *, note: str = "", force: bool = False,
               autosave: bool = False) -> dict:
    dest = save_path(cfg, name)
    if dest.exists() and not dest.is_dir():
        raise ValueError(f"저장 경로 '{name}'이 디렉터리가 아닙니다.")
    if dest.exists() and not force:
        raise ValueError(f"저장본 '{name}'이 이미 있습니다. /save {name} --force 로 덮어쓰세요.")
    meta = {
        "name": name, "created_at": datetime.now(timezone.utc).isoformat(), "note": note,
        "last_n": cfg.state.last_n, "mode": cfg.state.mode, "git_head": git_head(cfg.project),
        "roles": {n: {"cli": r.cli, "model": r.model} for n, r in cfg.roles.items()},
        "duet_version": __version__, "autosave": autosave,
        "archives": sorted(str(p.relative_to(cfg.project))
                           for p in (cfg.project / "DIALOGUE-archive").glob("*"))}
    cfg.saves_dir.mkdir(parents=True, exist_ok=True)
    # 공백은 유효한 저장 이름에 포함되지 않아 목록에 임시 디렉터리가 노출되지 않는다.
    stage = Path(tempfile.mkdtemp(prefix=".snapshot staging-", dir=cfg.saves_dir))
    backup = None
    try:
        (stage / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        (stage / "state.json").write_text(json.dumps(asdict(cfg.state), ensure_ascii=False, indent=2),
                                          encoding="utf-8")
        shutil.copyfile(cfg.project / "DIALOGUE.md", stage / "DIALOGUE.md")
        shutil.copyfile(cfg.roles_file, stage / "roles.yaml")
        if dest.exists():
            backup = cfg.saves_dir / (".snapshot backup-" + uuid4().hex)
            dest.rename(backup)
        try:
            stage.rename(dest)
        except OSError:
            if backup:
                backup.rename(dest)
            raise
        if backup:
            shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return meta


def read_save(cfg: Config, name: str) -> tuple[dict, State, bytes, dict]:
    path = save_path(cfg, name)
    if not path.is_dir():
        raise ValueError(f"저장본 '{name}'을 찾지 못했습니다.")
    # 모든 파일을 먼저 읽고 검사한다. 손상된 저장본이면 현재 세션을 건드리지 않는다.
    try:
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        raw = json.loads((path / "state.json").read_text(encoding="utf-8"))
        roles = yaml.safe_load((path / "roles.yaml").read_text(encoding="utf-8"))["roles"]
        dialogue = (path / "DIALOGUE.md").read_bytes()
        dialogue.decode("utf-8")
        if not isinstance(meta, dict) or not isinstance(raw, dict) or not isinstance(roles, dict):
            raise ValueError("meta/state/roles는 객체여야 합니다")
        state = State(**{k: v for k, v in raw.items() if k in State.__dataclass_fields__})
        validate_task(state.task)
        for field in ("sessions", "reviewer_sessions", "seen"):
            value = getattr(state, field)
            value_type = int if field == "seen" else str
            if not isinstance(value, dict) or any(
                    not isinstance(k, str) or type(v) is not value_type for k, v in value.items()):
                raise ValueError(f"잘못된 {field}")
        for field in ("last_n", "turns_since_checkpoint"):
            if type(getattr(state, field)) is not int or getattr(state, field) < 0:
                raise ValueError(f"잘못된 {field}")
        if not isinstance(state.mode, str) or type(state.auto) is not bool:
            raise ValueError("잘못된 mode/auto")
        if state.max_turns is not None and (type(state.max_turns) is not int or state.max_turns < -1):
            raise ValueError("잘못된 max_turns")
        if any(not isinstance(r, dict) or not isinstance(r.get("cli"), str) for r in roles.values()):
            raise ValueError("잘못된 역할 구성")
        for field in ("fork_on_resume", "reviewer_fork_on_resume"):
            value = getattr(state, field)
            if not isinstance(value, list) or any(not isinstance(n, str) for n in value):
                raise ValueError(f"잘못된 {field}")
        return meta, state, dialogue, roles
    except (OSError, ValueError, TypeError, KeyError, yaml.YAMLError) as e:
        raise ValueError(f"저장본 '{name}'을 읽을 수 없습니다: {e}") from e


def delete_save(cfg: Config, name: str) -> None:
    path = save_path(cfg, name)
    if not path.is_dir():
        raise ValueError(f"저장본 '{name}'을 찾지 못했습니다.")
    shutil.rmtree(path)
