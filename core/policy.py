"""권한 요청 3단계 정책: auto(자동 허용) / architect(설계자 판단) / human(사람 확인) / deny."""
from __future__ import annotations

import fnmatch
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .config import Role

AUTO, ARCHITECT, HUMAN, DENY = "auto", "architect", "human", "deny"

# Claude Code 도구 중 부작용이 없거나 작은 것
SAFE_TOOLS = {"Read", "Grep", "Glob", "LS", "TodoWrite", "Task", "Agent", "NotebookRead", "BashOutput",
              "KillShell", "ListMcpResourcesTool", "ReadMcpResourceTool", "Skill", "ToolSearch"}
NETWORK_TOOLS = {"WebFetch", "WebSearch"}
PLAN_READ_TOOLS = {"Read", "Grep", "Glob"}
READ_COMMAND = re.compile(r"^(?:ls|pwd|cat|head|tail|wc|grep|rg|find|tree|which|diff|stat|file|du|sort|uniq|cut|"
                          r"sed -n|git (?:status|diff|log|show|branch|rev-parse|ls-files|blame))\b")


@dataclass
class ApprovalRequest:
    role: str
    kind: str  # command | file | tool | permissions
    summary: str
    command: str | None = None
    paths: list[str] = field(default_factory=list)
    tool: str | None = None
    detail: dict = field(default_factory=dict)

    def cache_key(self) -> str:
        if self.kind == "command" and self.command:
            try:
                toks = shlex.split(self.command)
            except ValueError:
                toks = self.command.split()
            return f"{self.role}|cmd|{' '.join(toks[:2])}"
        if self.kind == "file":
            return f"{self.role}|file|{'|'.join(sorted(self.paths))}"
        return f"{self.role}|{self.kind}|{self.tool or self.summary}"


@dataclass
class Decision:
    allow: bool
    reason: str = ""
    scope: str = "once"  # once | session
    by: str = ""  # auto | architect | human | policy
    interrupt: bool = False


_SPLIT_RE = re.compile(r"\s*(?:&&|\|\||;|\||\n)\s*")


def split_commands(cmd: str) -> list[str]:
    cmd = cmd.strip()
    # bash -lc "..." 형태 벗기기 (codex 가 자주 이렇게 보냄)
    m = re.match(r"^(?:/bin/)?(?:ba|z)?sh\s+-l?c\s+(['\"])(.*)\1$", cmd, re.S)
    if m:
        cmd = m.group(2)
    parts = [p.strip() for p in _SPLIT_RE.split(cmd) if p.strip()]
    # 환경변수 접두어(FOO=bar cmd) 제거, cd 는 무해 처리
    out = []
    for p in parts:
        p = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)+", "", p)
        # 첫 토큰의 경로 접두어 제거: /root/.local/bin/pytest → pytest, .venv/bin/python → python
        p = re.sub(r"^(?:\S*/)(?=[^/\s]+(\s|$))", "", p)
        out.append(p)
    return out


class Policy:
    def __init__(self, project: Path, conf: dict, main_role: str):
        self.project = project.resolve()
        self.conf = conf
        self.main_role = main_role
        self.auto_res = [re.compile(p) for p in conf.get("auto_commands", [])]
        self.human_res = [re.compile(p) for p in conf.get("human_commands", [])]
        self.session_allow: set[str] = set()
        self.task: dict | None = None

    def verification_command(self, req: ApprovalRequest) -> bool:
        return bool(self.task and self.task["phase"] == "verify" and req.role == self.main_role
                    and req.kind == "command" and self.task["test_command"]
                    and (req.command or "").strip() == self.task["test_command"])

    # ---- 경로 ----
    def _rel(self, p: str) -> str | None:
        try:
            ap = (self.project / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
            return ap.relative_to(self.project).as_posix()
        except (ValueError, OSError):
            return None  # 프로젝트 밖

    @staticmethod
    def _match(rel: str, globs: list[str]) -> bool:
        return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch("/" + rel, "/" + g) for g in globs)

    # ---- 분류 ----
    def classify(self, req: ApprovalRequest, role: Role) -> tuple[str, str]:
        plan = bool(self.task and role.name != self.main_role
                    and (self.task["phase"] in ("plan", "plan_review") or self.task["waiting"]))
        if plan:
            if req.kind in ("file", "permissions"):
                return DENY, "합의 전 계획/대기 단계에서는 파일 쓰기와 권한 확장을 허용하지 않습니다."
            if req.kind == "command" and role.cli == "claude":
                return DENY, "plan 단계: Read/Grep/Glob 만 사용하세요. Bash는 금지됩니다."
            if req.kind == "tool" and req.tool not in PLAN_READ_TOOLS | NETWORK_TOOLS:
                return DENY, "plan 단계에서는 읽기 도구 외의 도구를 허용하지 않습니다."
        if self.task and self.task["phase"] == "verify" and role.name == self.main_role and req.kind == "command":
            if self.verification_command(req):
                if any(r.search(req.command or "") for r in self.human_res):
                    return HUMAN, "합의 명령이지만 기존 위험 명령 정책에 해당합니다."
                return AUTO, "합의된 검증 명령 (1회)"
            parts = split_commands(req.command or "")
            if not parts or not all(READ_COMMAND.search(p) for p in parts):
                return DENY, "verify에서는 합의된 test_command만 정확히 실행할 수 있습니다."
        if not plan and req.cache_key() in self.session_allow:
            return AUTO, "이번 세션에서 이미 허용한 요청"
        read_only = role.permissions == "read_only"

        if req.kind == "file":
            rels = [self._rel(p) for p in req.paths] or [None]
            if any(r is None for r in rels):
                return HUMAN, "프로젝트 밖 경로에 쓰기"
            if any(self._match(r, self.conf.get("protected_paths", [])) for r in rels):
                return HUMAN, "보호된 경로(.env, 키, .git 등)에 쓰기"
            if read_only:
                if all(self._match(r, self.conf.get("readonly_writable", [])) for r in rels):
                    return AUTO, "읽기 전용 역할의 허용 경로"
                return DENY, f"'{role.name}' 역할은 코드를 직접 수정하지 않습니다. 구현 역할에게 위임하세요."
            return AUTO, "프로젝트 안 파일 수정"

        if req.kind == "command":
            cmd = req.command or ""
            if any(r.search(cmd) for r in self.human_res):
                return HUMAN, "위험 명령(삭제·push·네트워크·권한 등)"
            parts = split_commands(cmd)
            if parts and all(any(r.search(p) for r in self.auto_res) or p.startswith("cd ") for p in parts):
                return AUTO, "안전한 명령"
            if read_only:
                return DENY, f"'{role.name}' 역할은 이 명령을 실행할 수 없습니다. 필요하면 구현 역할에게 위임하세요."
            return self._architect_or_human(role, "목록에 없는 명령")

        if req.kind == "tool":
            t = req.tool or ""
            if t in SAFE_TOOLS:
                return AUTO, "안전한 도구"
            if t == "AskUserQuestion":
                return DENY, "사람에게 물을 때는 턴 끝에 <!-- duet: ASK_HUMAN 질문 --> 지시문을 쓰세요."
            if t in NETWORK_TOOLS:
                return self._architect_or_human(role, "외부 네트워크 조회")
            return self._architect_or_human(role, f"도구 '{t}' 사용")

        if req.kind == "permissions":
            return HUMAN, "샌드박스 권한 확장 요청"
        return HUMAN, "알 수 없는 요청"

    def _architect_or_human(self, role: Role, reason: str) -> tuple[str, str]:
        if role.name == self.main_role:
            return HUMAN, reason + " (설계자 본인 요청)"
        return ARCHITECT, reason

    def remember(self, req: ApprovalRequest) -> None:
        if self.verification_command(req) or (self.task and self.task["phase"] in ("plan", "plan_review")):
            return
        self.session_allow.add(req.cache_key())
