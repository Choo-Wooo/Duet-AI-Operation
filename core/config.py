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
        # Windows (PowerShell·cmd)
        r"(?i)\b(Remove-Item|rd|rmdir|del|erase)\b.*\s[-/](Recurse|r|s)\b",
        r"(?i)\b(Invoke-WebRequest|iwr|Invoke-RestMethod|irm|Start-BitsTransfer|curl\.exe|wget\.exe)\b",
        r"(?i)\b(Stop-Process|taskkill|Set-ExecutionPolicy|Start-Process\b.*-Verb\s+RunAs)\b",
        r"(?i)\breg(\.exe)?\s+(add|delete|import)\b|\bformat(\.com)?\s+[a-z]:",
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
                               "integration_test": "",
                               # 병렬 작업 테스트 시간 한도(초)
                               "work_test_timeout": 900,
                               # 병렬 작업: 계획 합의 직후 구현 전 상태에서 합의 테스트를 한 번 돌려 원래부터 실패하는 테스트를 구분
                               "work_baseline_test": True,
                               # 대화 압축 시간 한도(초). 넘기면 실패로 보고 진행을 계속한다
                               "compact_timeout_sec": 300,
                               # 전권 자동 수락: 모드와 상관없이 승인·선택을 사람에게 묻지 않음 (autopilot_deny 만 막음)
                               "full_auto": False}
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


# 역할 프리셋: (보여줄 이름, 설명, 선호 CLI 순서, CLI별 모델, 권한, 병렬 최대, effort, 자동 허용 도구, MCP)
ROLE_PRESETS: dict[str, dict] = {
    "designer": dict(
        label="디자이너", clis=("claude", "codex", "agy"),
        models={"claude": DESIGNER_MODEL, "codex": DEFAULT_MODELS["codex"], "agy": "gemini-3.1-pro-high"},
        brief="사람이 보고 쓰는 모든 것의 디자인을 맡는다: 화면(UI/UX)·문서(README·설계서·보고서)·시각 에셋(아이콘·스킨·텍스처)·"
              "데이터 시각화. 디자인 시스템과 문서 서식을 정하고, 시안을 비교해 고른 뒤 만들고, 렌더링한 결과를 이미지로 확인하며 다듬는다.",
        permissions="workspace_write", max_sessions=2, context_limit=300000),
    "researcher": dict(
        label="리서처", clis=("agy", "claude", "codex"),
        models={"agy": RESEARCHER_MODEL, "claude": DEFAULT_MODELS["claude"], "codex": DEFAULT_MODELS["codex"]},
        brief="라이브러리·API·선행 사례·코드베이스를 조사해 근거와 출처가 있는 보고서를 docs/research/ 에 쓴다. "
              "코드는 고치지 않는다.",
        permissions="read_only", max_sessions=2, context_limit=300000),
    "tester": dict(
        label="테스터", clis=("codex", "claude", "agy"),
        models={"codex": DEFAULT_MODELS["codex"], "claude": DEFAULT_MODELS["claude"], "agy": RESEARCHER_MODEL},
        brief="수용 기준을 테스트로 옮기고 경계·오류·회귀 케이스를 보강한다. 실패를 재현하는 최소 테스트와 원인 분석을 보고한다.",
        permissions="workspace_write", max_sessions=2, context_limit=300000, effort="medium"),
    "reviewer": dict(
        label="코드 리뷰어", clis=("claude", "codex", "agy"),
        models={"claude": DEFAULT_MODELS["claude"], "codex": DEFAULT_MODELS["codex"], "agy": "gemini-3.1-pro-high"},
        brief="변경 사항을 읽고 버그·보안·성능·설계 일관성 문제를 찾아 근거와 수정 제안을 docs/reviews/ 에 쓴다. 코드는 고치지 않는다.",
        permissions="read_only", max_sessions=2, context_limit=300000),
    "writer": dict(
        label="문서 작성자", clis=("claude", "agy", "codex"),
        models={"claude": DEFAULT_MODELS["claude"], "agy": RESEARCHER_MODEL, "codex": DEFAULT_MODELS["codex"]},
        brief="README·사용 설명서·변경 기록·API 문서를 코드와 맞게 작성하고 갱신한다. docs/ 와 문서 파일만 쓴다.",
        permissions="read_only", max_sessions=1, context_limit=300000),
}
# 기존 프로젝트에도 자동으로 채워 넣는 기본 역할
AUTO_PRESET_ROLES = ("designer", "researcher")


def preset_tools(key: str, cli: str) -> tuple[list[str], dict]:
    """역할 × CLI 에 맞는 자동 허용 도구와 역할 전용 MCP (브라우저 도구가 필요하면 Claude 에 Playwright MCP)."""
    from .role_guides import PRESET_TOOLS
    tools = list(PRESET_TOOLS.get((key, cli), []))
    mcp = dict(PLAYWRIGHT_MCP) if cli == "claude" and any(t.startswith("mcp__playwright__") for t in tools) else {}
    return tools, mcp


def preset_role(key: str, clis, name: str | None = None, cli: str | None = None,
                model: str | None = None) -> Role | None:
    """프리셋으로 역할을 만든다. cli 를 주지 않으면 설치된 CLI 중 선호 순서대로 고른다."""
    p = ROLE_PRESETS.get(key)
    if not p:
        return None
    cli = cli if cli in SUPPORTED_CLIS else next((c for c in p["clis"] if c in clis), None)
    if not cli:
        return None
    tools, mcp = preset_tools(key, cli)
    return Role(name or key, cli, model or p["models"].get(cli) or DEFAULT_MODELS[cli], p["brief"], p["permissions"],
                p.get("effort") if cli == "codex" else None, context_limit=p.get("context_limit"),
                max_sessions=p.get("max_sessions", 1), auto_tools=tools, mcp=mcp)


def presets_info(clis) -> list[dict]:
    out = []
    for key, p in ROLE_PRESETS.items():
        r = preset_role(key, clis)
        out.append({"key": key, "label": p["label"], "available": r is not None,
                    "cli": r.cli if r else None, "model": r.model if r else None,
                    "brief": p["brief"], "permissions": p["permissions"], "max_sessions": p.get("max_sessions", 1),
                    "effort": r.effort if r else None, "tools": bool(r and (r.auto_tools or r.mcp))})
    return out


ROLE_TOOLS_VERSION = 1


def ensure_preset_roles(cfg: "Config", clis) -> list[str]:
    """기존 프로젝트 보강 (한 번씩만):
    - 기본 역할(디자이너·리서처)이 없으면 추가. 사람이 지운 역할은 다시 넣지 않는다 (settings.preset_roles_added).
    - 프리셋 이름의 역할에 그 CLI 에 맞는 도구(자동 허용·Playwright MCP)를 더한다. 설명·모델은 건드리지 않는다
      (settings.role_tools_version)."""
    msgs: list[str] = []
    done = list(cfg.settings.get("preset_roles_added") or [])
    changed = False
    for key in AUTO_PRESET_ROLES:
        if key in done:
            continue
        done.append(key)
        changed = True
        if key not in cfg.roles:
            r = preset_role(key, clis)
            if r:
                cfg.roles[key] = r
                msgs.append(f"{ROLE_PRESETS[key]['label']} 역할을 추가했습니다: {key}={r.cli}/{r.model} (역할·모델 화면에서 변경·삭제)")
    if changed:
        cfg.settings["preset_roles_added"] = done
    if int(cfg.settings.get("role_tools_version") or 0) < ROLE_TOOLS_VERSION:
        for r in cfg.roles.values():
            if r.name not in ROLE_PRESETS:
                continue
            tools, mcp = preset_tools(r.name, r.cli)
            add = [t for t in tools if t not in r.auto_tools]
            new_mcp = {k: v for k, v in mcp.items() if k not in r.mcp}
            if add or new_mcp:
                r.auto_tools = list(r.auto_tools) + add
                r.mcp = {**r.mcp, **new_mcp}
                msgs.append(f"{r.name} 역할에 역할 도구를 붙였습니다: {', '.join(add + list(new_mcp))}")
        cfg.settings["role_tools_version"] = ROLE_TOOLS_VERSION
        changed = True
    if changed:
        cfg.save_roles()
    return msgs


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
    for key in AUTO_PRESET_ROLES:
        r = preset_role(key, clis)
        if r:
            roles[key] = r
    return "architect", roles
