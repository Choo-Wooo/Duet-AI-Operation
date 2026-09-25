"""설정 파일(.duet/*.yaml)과 상태(.duet/state.json) 관리."""
from __future__ import annotations

import json
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .agreement import EXCLUDED_DIRS, EXCLUDED_FILES, validate_task

SUPPORTED_CLIS = ("claude", "codex")
PERMISSION_PROFILES = ("read_only", "workspace_write")

DEFAULT_MODELS = {"claude": "claude-opus-5-5", "codex": "gpt-6-astra"}


@dataclass
class Role:
    name: str
    cli: str
    model: str | None = None
    brief: str = ""
    permissions: str = "workspace_write"
    effort: str | None = None  # codex: reasoning effort, claude: effort
    context_limit: int | None = None  # 이 역할 세션의 컨텍스트 한도(토큰). 없으면 settings.context_limit_tokens

    def to_yaml(self) -> dict:
        d = {"cli": self.cli, "model": self.model, "brief": self.brief, "permissions": self.permissions}
        if self.effort:
            d["effort"] = self.effort
        if self.context_limit:
            d["context_limit"] = self.context_limit
        return d


@dataclass
class Mode:
    name: str
    max_turns: int | None  # None = 무제한
    style: str
    stall_turns: int = 6
    agreement: bool = True


DEFAULT_MODES = {
    "sprint": Mode("sprint", 20, "설계는 짧게 하고 바로 위임한다. 결과 위주로 보고받고 빠르게 다음 단계로 간다.", 6, False),
    "review": Mode("review", 60, "구현이 끝나면 꼼꼼히 검토하고, 부족하면 구체적인 재작업을 적극적으로 요청한다.", 8),
    "deliberate": Mode(
        "deliberate", None,
        "구현 전에 구현자와 대안·반론·위험을 충분히 주고받는다. 구현자에게 설계 검토를 먼저 요청해도 좋다. "
        "대화량보다 매 턴 새 정보(결정·근거·질문·발견)가 오가는 것이 중요하다.", 12),
}

DEFAULT_POLICY = {
    # 모든 역할에 대해 자동 허용되는 명령 (정규식, 각 하위 명령마다 검사)
    "auto_commands": [
        r"^(ls|pwd|cat|head|tail|wc|echo|printf|grep|rg|find|tree|which|diff|stat|file|du|sort|uniq|cut|true)\b",
        r"^sed -n\b",
        r"^git (status|diff|log|show|branch|rev-parse|ls-files|blame)\b",
        r"^(mkdir|touch)\b",
        r"^(python3?|py) -m (pytest|unittest|mypy|ruff|black|pyflakes|compileall)\b",
        r"^(pytest|mypy|ruff|black|flake8|pyright|eslint|prettier|tsc|jest|vitest)\b",
        r"^(npm|pnpm|yarn|bun) (run )?(test|lint|build|typecheck|format|check)\b",
        r"^(cargo) (test|build|check|clippy|fmt)\b",
        r"^go (test|build|vet|fmt)\b",
        r"^make( (test|build|lint|check))?$",
    ],
    # 사람 확인이 필요한 위험 명령 (정규식, 명령 전체에서 검색)
    "human_commands": [
        r"\brm\s+-[a-zA-Z]*[rf]",
        r"\bsudo\b",
        r"\bgit\s+(push|reset\s+--hard|clean\s+-[a-zA-Z]*f|checkout\s+--|rebase|filter-branch|remote)\b",
        r"\b(curl|wget|ssh|scp|rsync|ftp|nc)\b",
        r"\b(npm|pnpm|yarn|cargo|twine|poetry)\s+publish\b",
        r"\bdocker\b|\bkubectl\b|\bterraform\b",
        r"\bchmod\s+-R\b|\bchown\b",
        r"\bkill(all)?\b|\bpkill\b",
        r"\bmkfs\b|\bdd\s+if=",
        r"(^|\s)(~|/etc|/usr|/System|/Library)(/|\s|$)",
    ],
    # 보호 경로: 쓰기 시 사람 확인 (glob, 프로젝트 기준)
    "protected_paths": [".env", ".env.*", "**/.env", "**/*.pem", "**/*.key", ".git/**", "**/secrets/**", ".duet/**"],
    # read_only 역할이 쓸 수 있는 경로
    "readonly_writable": ["DIALOGUE.md", "docs/**", "docs/*"],
    # 설계자 심사 설정
    "review_timeout_sec": 120,
    "ask_architect_opinion_for_human": True,
}


def _dump_yaml(path: Path, data: Any, header: str = "") -> None:
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)
    path.write_text((header + "\n" if header else "") + text, encoding="utf-8")


@dataclass
class State:
    sessions: dict[str, str] = field(default_factory=dict)  # role -> session/thread id
    reviewer_sessions: dict[str, str] = field(default_factory=dict)
    seen: dict[str, int] = field(default_factory=dict)  # role -> 마지막으로 본 턴 번호
    last_n: int = 0
    mode: str = "review"
    max_turns: int | None = -1  # -1 = 모드 기본값 사용, None = 무제한
    auto: bool = True
    turns_since_checkpoint: int = 0
    fork_on_resume: list[str] = field(default_factory=list)
    reviewer_fork_on_resume: list[str] = field(default_factory=list)
    task: dict | None = None


class Config:
    """프로젝트별 설정 묶음."""

    def __init__(self, project: Path):
        self.project = project
        self.dir = project / ".duet"
        self.roles_file = self.dir / "roles.yaml"
        self.modes_file = self.dir / "modes.yaml"
        self.policy_file = self.dir / "policy.yaml"
        self.state_file = self.dir / "state.json"
        self.logs_dir = self.dir / "logs"
        self.saves_dir = self.dir / "saves"
        self.main: str = "architect"
        self.roles: dict[str, Role] = {}
        self.modes: dict[str, Mode] = dict(DEFAULT_MODES)
        self.policy: dict = dict(DEFAULT_POLICY)
        self.state = State()
        self.settings: dict = {"checkpoint_every": 50, "git_snapshots": True,
                               "plan_rounds": 3, "plan_approval": "architect",
                               "fingerprint_exclude": sorted(EXCLUDED_DIRS | EXCLUDED_FILES),
                               # 세션 컨텍스트가 이 토큰 수를 넘으면 다음 턴 전에 압축(/compact), 실패하면 새 세션
                               "context_limit_tokens": 100000,
                               # 한 턴 프롬프트의 최대 글자 수. 넘는 부분은 파일로 빼고 경로만 전달
                               "prompt_max_chars": 20000,
                               # DIALOGUE.md 가 이 크기(KB)를 넘으면 체크포인트 요약 후 이전 대화를 보관 폴더로 옮김
                               "dialogue_max_kb": 120,
                               # 작업이 끝났을 때 이 크기 이상이면 다음 턴 전에 미리 압축
                               "compact_floor_tokens": 40000,
                               # 질문 콘솔(/ask) 세션이 이 크기를 넘으면 압축
                               "ask_compact_tokens": 200000}
        self.runtime: dict = {}  # 이번 실행에만 쓰는 값 (예산 등, 저장 안 함)

    # ---------- 로드 ----------
    def load(self) -> None:
        data = yaml.safe_load(self.roles_file.read_text(encoding="utf-8")) or {}
        self.main = data.get("main", "architect")
        self.settings.update(data.get("settings") or {})
        if type(self.settings["plan_rounds"]) is not int or self.settings["plan_rounds"] < 1:
            raise ValueError("settings.plan_rounds는 양의 정수여야 합니다")
        if self.settings["plan_approval"] not in ("architect", "human"):
            raise ValueError("settings.plan_approval은 architect 또는 human입니다")
        for key in ("context_limit_tokens", "prompt_max_chars", "dialogue_max_kb", "compact_floor_tokens",
                    "ask_compact_tokens"):
            if type(self.settings[key]) is not int or self.settings[key] < 0:
                raise ValueError(f"settings.{key}는 0 이상의 정수여야 합니다 (0 = 끔)")
        excluded = self.settings["fingerprint_exclude"]
        if not isinstance(excluded, list) or any(
                not isinstance(name, str) or not name.strip() or name in (".", "..")
                or "/" in name or "\\" in name for name in excluded):
            raise ValueError("settings.fingerprint_exclude는 경로가 아닌 파일/디렉터리 이름 목록이어야 합니다")
        self.roles = {}
        for name, r in (data.get("roles") or {}).items():
            self.roles[name] = Role(
                name=name, cli=r.get("cli", "claude"), model=r.get("model"), brief=r.get("brief", ""),
                permissions=r.get("permissions", "workspace_write"), effort=r.get("effort"),
                context_limit=r.get("context_limit"),
            )
            cl = self.roles[name].context_limit
            if cl is not None and (type(cl) is not int or cl < 0):
                raise ValueError(f"roles.{name}.context_limit 는 0 이상의 정수여야 합니다")
        if self.main not in self.roles:
            raise ValueError(f"roles.yaml 의 main('{self.main}') 역할이 roles 에 없습니다.")
        if self.modes_file.exists():
            md = yaml.safe_load(self.modes_file.read_text(encoding="utf-8")) or {}
            for name, m in (md.get("modes") or {}).items():
                mt = m.get("max_turns")
                agreement = m.get("agreement", name != "sprint")
                if type(agreement) is not bool:
                    raise ValueError(f"{name}.agreement는 true/false여야 합니다")
                self.modes[name] = Mode(
                    name=name,
                    max_turns=None if mt in (None, "inf", "unlimited", 0) else int(mt),
                    style=m.get("style", ""),
                    stall_turns=int(m.get("stall_turns", 6)),
                    agreement=agreement,
                )
        if self.policy_file.exists():
            self.policy.update(yaml.safe_load(self.policy_file.read_text(encoding="utf-8")) or {})
        if self.state_file.exists():
            try:
                raw = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.state = State(**{k: v for k, v in raw.items() if k in State.__dataclass_fields__})
            except Exception:
                self.state = State()
        validate_task(self.state.task)
        if self.state.mode not in self.modes:
            self.state.mode = "review"

    # ---------- 저장 ----------
    def save_roles(self) -> None:
        _dump_yaml(
            self.roles_file,
            {"main": self.main, "settings": self.settings,
             "roles": {n: r.to_yaml() for n, r in self.roles.items()}},
            "# duet 역할 설정. main 역할이 사람과 대화하고 다른 역할에게 위임합니다.\n"
            "# cli: claude | codex, permissions: read_only | workspace_write",
        )

    def save_modes(self) -> None:
        _dump_yaml(
            self.modes_file,
            {"modes": {n: {"max_turns": m.max_turns if m.max_turns is not None else "inf",
                           "stall_turns": m.stall_turns, "style": m.style, "agreement": m.agreement}
                       for n, m in self.modes.items()}},
            "# 대화 모드 프리셋. max_turns: 숫자 또는 inf(무제한). stall_turns: 코드 변경 없이 이 턴 수가 지나면 정체로 판단(0=끔).",
        )

    def save_policy(self) -> None:
        _dump_yaml(self.policy_file, self.policy,
                   "# 권한 정책. auto_commands=자동 허용, human_commands=사람 확인, 그 외는 설계자 판단.")

    def save_state(self) -> None:
        self.state_file.write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=2), encoding="utf-8")

    # ---------- 편의 ----------
    @property
    def mode(self) -> Mode:
        return self.modes[self.state.mode]

    @property
    def max_turns(self) -> int | None:
        if self.state.max_turns == -1:
            return self.mode.max_turns
        return self.state.max_turns

    def main_role(self) -> Role:
        return self.roles[self.main]


def detect_clis() -> dict[str, str]:
    from .clis import which
    found = {}
    for c in SUPPORTED_CLIS:
        p = which(c)
        if p:
            found[c] = p
    return found


def default_roles(clis: dict[str, str]) -> tuple[str, dict[str, Role]]:
    arch_cli = "claude" if "claude" in clis else "codex"
    impl_cli = "codex" if "codex" in clis else "claude"
    roles = {
        "architect": Role(
            "architect", arch_cli, DEFAULT_MODELS[arch_cli],
            "사람의 요구를 설계로 바꾸고 작업을 나눠 위임한다. 구현 결과를 검토하고 구현자의 권한 요청을 심사한다. "
            "코드는 직접 고치지 않고 docs/ 와 DIALOGUE.md 만 쓴다.",
            "read_only", context_limit=500000,
        ),
        "implementer": Role(
            "implementer", impl_cli, DEFAULT_MODELS[impl_cli],
            "설계와 위임받은 작업에 따라 코드를 작성하고 테스트를 통과시킨 뒤 결과를 보고한다.",
            "workspace_write", "high" if impl_cli == "codex" else None,
        ),
    }
    return "architect", roles
