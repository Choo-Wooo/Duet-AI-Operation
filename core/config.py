"""설정 파일(.duet/*.yaml)과 상태(.duet/state.json) 관리."""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .agreement import EXCLUDED_DIRS, EXCLUDED_FILES, validate_task
from .storage import atomic_write, load_json


def validate_role_name(name: str) -> None:
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9가-힣_-]{1,32}', name):
        raise ValueError(f'잘못된 역할 이름 {name!r}: 영문·숫자·한글·_·- 1~32자여야 합니다')

SUPPORTED_CLIS = ("claude", "codex", "agy")
PERMISSION_PROFILES = ("read_only", "workspace_write")

DEFAULT_MODELS = {"claude": "claude-opus-5-5", "codex": "gpt-6-astra", "agy": "gemini-3.8-flash-medium"}
# 디자이너가 만든 화면을 직접 열어 보고 스크린샷으로 확인하는 브라우저 도구 (Claude Code 가 npx 로 실행)
PLAYWRIGHT_MCP = {"playwright": {"type": "stdio", "command": "npx",
                                 "args": ["-y", "@playwright/mcp@latest", "--headless", "--isolated"]}}
# 역할별 기본 모델 (CLI 가 있을 때)
DESIGNER_MODEL = "claude-opus-5-5"  # Design Arena(2026-09) 웹 UI 1위
RESEARCHER_MODEL = "gemini-3.8-flash-medium"


@dataclass
class Role:
    name: str
    cli: str
    model: str | None = None
    brief: str = ""
    permissions: str = "workspace_write"
    effort: str | None = None  # codex: reasoning effort, claude: effort
    context_limit: int | None = None  # 이 역할 세션의 컨텍스트 한도(토큰). 없으면 settings.context_limit_tokens
    max_sessions: int = 1  # 병렬 작업 때 이 역할로 동시에 띄울 수 있는 세션 수 (설계자가 이 안에서 결정)
    auto_tools: list[str] = field(default_factory=list)  # 이 역할에 자동 허용할 도구 이름 패턴 (예: mcp__playwright__*)
    mcp: dict = field(default_factory=dict)  # 이 역할 세션에만 추가로 붙일 MCP 서버 (Claude Code)

    def to_yaml(self) -> dict:
        d = {"cli": self.cli, "model": self.model, "brief": self.brief, "permissions": self.permissions}
        if self.effort:
            d["effort"] = self.effort
        if self.context_limit:
            d["context_limit"] = self.context_limit
        if self.max_sessions != 1:
            d["max_sessions"] = self.max_sessions
        if self.auto_tools:
            d["auto_tools"] = list(self.auto_tools)
        if self.mcp:
            d["mcp"] = dict(self.mcp)
        return d


@dataclass
class Mode:
    name: str
    max_turns: int | None  # None = 무제한
    style: str
    stall_turns: int = 6
    agreement: bool = True
    autonomy: str = "normal"  # normal | full (전권 자동: 승인·선택을 사람에게 묻지 않음)


DEFAULT_MODES = {
    "sprint": Mode("sprint", 20, "설계는 짧게 하고 바로 위임한다. 결과 위주로 보고받고 빠르게 다음 단계로 간다.", 6, False),
    "review": Mode("review", 60, "구현이 끝나면 꼼꼼히 검토하고, 부족하면 구체적인 재작업을 적극적으로 요청한다.", 8),
    "deliberate": Mode(
        "deliberate", None,
        "구현 전에 구현자와 대안·반론·위험을 충분히 주고받는다. 구현자에게 설계 검토를 먼저 요청해도 좋다. "
        "대화량보다 매 턴 새 정보(결정·근거·질문·발견)가 오가는 것이 중요하다.", 12),
    "autopilot": Mode(
        "autopilot", None,
        "사람이 모든 권한을 위임했다. 승인·선택을 기다리지 말고 끝까지 진행한다. 판단이 필요한 것은 스스로 정하고 근거를 "
        "docs/ 에 남긴다. 작업자의 질문에는 사람 대신 답한다. ASK_HUMAN 은 요구 자체가 모호해 더 진행할 수 없을 때만 쓴다. "
        "검증은 평소보다 엄격하게 하고, 막히면 방법을 바꿔 다시 시도한다.", 10, True, "full"),
}

DEFAULT_POLICY = {
    "human_approval_timeout_sec": 3600,
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
    "protected_paths": [".env", ".env.*", "**/.env", "**/.env.*", "**/*.pem", "**/*.key",
                        ".git/**", "**/secrets/**", ".duet/**", ".claude/**", ".agents/**"],
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
        self.load_warnings: list[str] = []
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
                               "ask_compact_tokens": 200000,
                               # 병렬 작업: 설계자 외 동시에 도는 작업 세션 수 상한
                               "max_parallel": 4,
                               # 병렬 작업: ACCEPT + 통합 테스트 통과 시 자동 병합 (false 면 사람 승인 후 병합)
                               "auto_merge": True,
                               # 병렬 작업 통합 테스트 명령 (없으면 각 작업의 test_command 를 모두 실행)
                               "integration_test": ""}
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
                    "ask_compact_tokens", "max_parallel"):
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
                context_limit=r.get("context_limit"), max_sessions=r.get("max_sessions", 1),
                auto_tools=list(r.get("auto_tools") or []), mcp=dict(r.get("mcp") or {}),
            )
            ms = self.roles[name].max_sessions
            if type(ms) is not int or ms < 1:
                raise ValueError(f"roles.{name}.max_sessions 는 1 이상의 정수여야 합니다")
            if self.roles[name].cli not in SUPPORTED_CLIS:
                raise ValueError(f"roles.{name}.cli 는 {', '.join(SUPPORTED_CLIS)} 중 하나여야 합니다")
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
                    autonomy="full" if str(m.get("autonomy", "normal")) == "full" else "normal",
                )
        if self.policy_file.exists():
            self.policy.update(yaml.safe_load(self.policy_file.read_text(encoding="utf-8")) or {})
        for name in self.roles:
            validate_role_name(name)
        raw = load_json(self.state_file, self.load_warnings.append)
        self.state = State(**{k: v for k, v in raw.items() if k in State.__dataclass_fields__}) if raw is not None else State()
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
            "# cli: claude | codex | agy, permissions: read_only | workspace_write\n"
            "# max_sessions: 병렬 작업 때 이 역할로 동시에 띄울 수 있는 세션 수",
        )

    def save_modes(self) -> None:
        _dump_yaml(
            self.modes_file,
            {"modes": {n: {"max_turns": m.max_turns if m.max_turns is not None else "inf",
                           "stall_turns": m.stall_turns, "style": m.style, "agreement": m.agreement,
                           **({"autonomy": m.autonomy} if m.autonomy != "normal" else {})}
                       for n, m in self.modes.items()}},
            "# 대화 모드 프리셋. max_turns: 숫자 또는 inf(무제한). stall_turns: 코드 변경 없이 이 턴 수가 지나면 정체로 판단(0=끔).\n"
            "# autonomy: full 이면 전권 자동 (승인·선택을 사람에게 묻지 않고 진행, policy.yaml 의 autopilot_deny 만 막음)",
        )

    def save_policy(self) -> None:
        _dump_yaml(self.policy_file, self.policy,
                   "# 권한 정책. auto_commands=자동 허용, human_commands=사람 확인, 그 외는 설계자 판단.")

    def save_state(self) -> None:
        atomic_write(self.state_file, json.dumps(asdict(self.state), ensure_ascii=False, indent=2))

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
    arch_cli = "claude" if "claude" in clis else ("codex" if "codex" in clis else "agy")
    impl_cli = "codex" if "codex" in clis else ("claude" if "claude" in clis else "agy")
    roles = {
        "architect": Role(
            "architect", arch_cli, DEFAULT_MODELS[arch_cli],
            "사람의 요구를 설계로 바꾸고 작업을 나눠 위임한다. 병렬로 나눌 수 있으면 역할별 세션 수를 정해 duet-work 로 "
            "동시에 맡긴다. 구현 결과를 검토하고 다른 역할의 권한 요청을 심사한다. "
            "코드는 직접 고치지 않고 docs/ 와 DIALOGUE.md 만 쓴다.",
            "read_only", context_limit=500000,
        ),
        "implementer": Role(
            "implementer", impl_cli, DEFAULT_MODELS[impl_cli],
            "설계와 위임받은 작업에 따라 코드를 작성하고 테스트를 통과시킨 뒤 결과를 보고한다.",
            "workspace_write", "medium" if impl_cli == "codex" else None, context_limit=300000, max_sessions=3,
        ),
    }
    if "claude" in clis:
        roles["designer"] = Role(
            "designer", "claude", DESIGNER_MODEL,
            "화면·UX·API 형태·데이터 모델을 설계하고 시안(docs/design/, 목업 코드, 스타일)을 만든다. "
            "구현자와 인터페이스를 맞추고, 결정 근거를 문서로 남긴다.",
            "workspace_write", context_limit=300000, max_sessions=2,
            auto_tools=["mcp__playwright__*"], mcp=dict(PLAYWRIGHT_MCP),
        )
    researcher_cli = "agy" if "agy" in clis else ("claude" if "claude" in clis else None)
    if researcher_cli:
        roles["researcher"] = Role(
            "researcher", researcher_cli,
            RESEARCHER_MODEL if researcher_cli == "agy" else DEFAULT_MODELS[researcher_cli],
            "라이브러리·API·선행 사례·코드베이스를 조사해 근거와 출처가 있는 보고서를 docs/research/ 에 쓴다. "
            "코드는 고치지 않는다.",
            "read_only", context_limit=300000, max_sessions=2,
        )
    return "architect", roles
