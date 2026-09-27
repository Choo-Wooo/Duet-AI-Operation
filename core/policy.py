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
# 읽기 전용 단계(plan·질문 콘솔)에서도 쓰는 도구: 읽기·검색·도구 불러오기·할 일 목록·보조 에이전트·스킬
# (보조 에이전트의 도구 호출도 같은 훅을 거치므로 읽기 전용이 그대로 적용된다)
PLAN_TOOLS = PLAN_READ_TOOLS | {"LS", "ToolSearch", "TodoWrite", "Agent", "Task", "NotebookRead", "Skill",
                                "BashOutput", "ListMcpResourcesTool", "ReadMcpResourceTool"}
PLAN_DENY_TEXT = ("읽기 전용 단계: 읽기·검색 도구(Read/Grep/Glob/LS/ToolSearch), 보조 에이전트, 읽기 명령(ls, cat, grep, "
                  "git log 등)만 쓸 수 있습니다. 파일 쓰기·설치·빌드·실행은 합의 후에 하세요.")
# 전권 자동(autopilot) 모드에서도 막는 명령: push 는 사람만, 시스템 파괴·관리자 권한 금지
AUTOPILOT_DENY = [
    r"\bgit\s+push\b",
    r"\bsudo\b",
    r"\brm\s+-[a-zA-Z]*[rf][a-zA-Z]*\s+(/|~|\$HOME)(\s|$)",
    r"\bmkfs\b|\bdd\s+if=",
    r"\b(npm|pnpm|yarn|cargo|twine|poetry)\s+publish\b",
]
# 읽기 전용 명령 (모든 역할·모든 단계에서 파일로 리다이렉트하지 않으면 자동 허용)
READ_COMMAND = re.compile(
    r"^(?:ls|pwd|cat|head|tail|wc|grep|egrep|rg|find|tree|which|diff|stat|file|du|df|sort|uniq|cut|tr|nl|column|"
    r"comm|paste|xxd|od|hexdump|jq|less|more|basename|dirname|realpath|readlink|date|whoami|uname|ps|test|true|echo|"
    r"printf|git (?:status|diff|log|show|branch|rev-parse|ls-files|blame|grep))\b"
    r"|^sed(?![^|]*\s-i)\b"            # sed (단 -i 제자리 수정 제외)
    r"|^awk(?![^|]*system\s*\()\b"    # awk (단 system() 호출 제외)
)
# find 의 파일 변경 옵션
_FIND_WRITES = re.compile(r"\s-(?:delete|exec|execdir|ok|fprint\w*)\b")
# 경로 규칙(~, /etc, /usr …): 읽기 전용 명령에는 적용하지 않는다
PATH_RULE_HINT = "/etc"


def is_read_command(part: str) -> bool:
    return bool(READ_COMMAND.search(part)) and not (part.startswith("find") and _FIND_WRITES.search(part))


def read_only_command(cmd: str) -> bool:
    """파일을 바꾸지 않는 읽기 명령(ls, cat, grep, git log …)만으로 이뤄졌는지."""
    parts = split_commands(cmd or "")
    return bool(parts) and not writes_output(cmd) and all(is_read_command(p) or p.startswith("cd ") for p in parts)


def read_only_decision(req: "ApprovalRequest", what: str = "이 세션") -> "Decision":
    """읽기 전용 세션(질문 콘솔·협의 답변)용 승인: 읽기 도구·읽기 명령만 허용."""
    if (req.kind == "tool" and req.tool in PLAN_TOOLS) or (req.kind == "command" and read_only_command(req.command or "")):
        return Decision(True, "읽기 전용 허용", by="policy")
    return Decision(False, f"{what}은 읽기 전용입니다. 파일 수정·실행은 본 작업에서 요청하세요.", by="policy")


def tool_matches(tool: str | None, patterns: list[str] | None) -> bool:
    return bool(tool) and any(fnmatch.fnmatchcase(tool, p) for p in (patterns or []))


def writes_output(cmd: str) -> bool:
    """파일로 리다이렉트하는지 (2>&1, >/dev/null 은 제외, 따옴표 안의 > 는 무시)."""
    unquoted = re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", "", cmd)
    stripped = re.sub(r"\d?>&\d|\d?>>?\s*/dev/null", "", unquoted)
    return ">" in stripped or re.search(r"\btee\b", unquoted) is not None


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


def _split_unquoted(cmd: str) -> list[str]:
    """따옴표 밖의 &&, ||, ;, |, 줄바꿈 에서만 나눈다 (grep -E "a|b" 의 | 는 나누지 않음)."""
    parts, buf, q, i = [], [], None, 0
    while i < len(cmd):
        c = cmd[i]
        if q:
            buf.append(c)
            if c == "\\" and q == '"' and i + 1 < len(cmd):
                buf.append(cmd[i + 1])
                i += 1
            elif c == q:
                q = None
        elif c in "'\"":
            q = c
            buf.append(c)
        elif c == "\\" and i + 1 < len(cmd):
            buf.append(c + cmd[i + 1])
            i += 1
        elif cmd.startswith(("&&", "||"), i):
            parts.append("".join(buf))
            buf = []
            i += 1
        elif c in ";|\n":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def split_commands(cmd: str) -> list[str]:
    cmd = cmd.strip()
    # bash -lc "..." 형태 벗기기 (codex 가 자주 이렇게 보냄)
    m = re.match(r"^(?:/bin/)?(?:ba|z)?sh\s+-l?c\s+(['\"])(.*)\1$", cmd, re.S)
    if m:
        cmd = m.group(2)
    out = []
    for p in _split_unquoted(cmd):
        # 환경변수 접두어(FOO=bar cmd) 제거
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
        """합의 test_command 그대로, 또는 그 출력을 head/tail/grep 으로 줄인 형태."""
        if not (self.task and self.task["phase"] == "verify" and req.role == self.main_role
                and req.kind == "command" and self.task["test_command"]):
            return False
        cmd, test = (req.command or "").strip(), self.task["test_command"].strip()
        m = re.match(r"^(?:/bin/)?(?:ba|z)?sh\s+-l?c\s+(['\"])(.*)\1$", cmd, re.S)
        if m:
            cmd = m.group(2).strip()
        cmd = re.sub(r"^cd\s+\S+\s*&&\s*", "", cmd)
        if cmd == test:
            return True
        if not cmd.startswith(test):
            return False
        rest = cmd[len(test):].strip()
        rest = re.sub(r"^2>&1\s*", "", rest)
        if not rest.startswith("|"):
            return False
        tail = _split_unquoted(rest[1:])
        return bool(tail) and all(re.match(r"^(?:head|tail|grep|egrep|rg|wc|sed -n|cat)\b", p) for p in tail) \
            and not writes_output(rest)

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
            if req.kind == "command":
                if read_only_command(req.command or ""):
                    return AUTO, "읽기 전용 단계의 읽기 명령"
                return DENY, PLAN_DENY_TEXT
            if req.kind == "tool" and not (req.tool in PLAN_TOOLS | NETWORK_TOOLS
                                           or tool_matches(req.tool, role.auto_tools)):
                return DENY, PLAN_DENY_TEXT
        if self.task and self.task["phase"] == "verify" and role.name == self.main_role and req.kind == "command":
            if self.verification_command(req):
                # 실행 파일 절대경로(/usr/bin/python3 등)는 경로 규칙에서 뺀다
                body = re.sub(r"^\s*/\S+", "", req.command or "", count=1)
                if any(r.search(body) for r in self.human_res):
                    return HUMAN, "합의 명령이지만 기존 위험 명령 정책에 해당합니다."
                return AUTO, "합의된 검증 명령 (1회)"
            parts = split_commands(req.command or "")
            if not (parts and all(is_read_command(p) or p.startswith("cd ") for p in parts)
                    and not writes_output(req.command or "")):
                return HUMAN, "verify 단계의 합의 명령·읽기 외 명령 (사람 확인)"
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
            parts = split_commands(cmd)
            redirect = writes_output(cmd)
            read_only_cmd = bool(parts) and not redirect and all(
                is_read_command(p) or p.startswith("cd ") for p in parts)
            hits = [r for r in self.human_res if r.search(cmd)]
            if read_only_cmd:  # 읽기만 하는 명령은 프로젝트 밖 경로 규칙을 적용하지 않는다
                hits = [r for r in hits if PATH_RULE_HINT not in r.pattern]
            if hits:
                return HUMAN, "위험 명령(삭제·push·네트워크·권한·프로젝트 밖 경로 등)"
            if read_only_cmd:
                return AUTO, "읽기 전용 명령"
            if not redirect and parts and all(
                    (any(r.search(p) for r in self.auto_res) and not _FIND_WRITES.search(p)
                     and not (read_only and re.match(r"^(?:mkdir|touch|cp|mv)\b", p)))
                    or is_read_command(p) or p.startswith("cd ")
                    for p in parts):
                return AUTO, "안전한 명령"
            if read_only:
                return HUMAN, f"'{role.name}' 역할의 목록 밖 명령 (사람 확인 — A 로 세션 동안 허용 가능)"
            return self._architect_or_human(role, "목록에 없는 명령")

        if req.kind == "tool":
            t = req.tool or ""
            if t in SAFE_TOOLS:
                return AUTO, "안전한 도구"
            if tool_matches(t, role.auto_tools):
                return AUTO, f"'{role.name}' 역할에 자동 허용된 도구"
            if t == "AskUserQuestion":
                return DENY, "사람에게 물을 때는 턴 끝에 <!-- duet: ASK_HUMAN 질문 --> 지시문을 쓰세요."
            if t in NETWORK_TOOLS:
                return self._architect_or_human(role, "외부 네트워크 조회")
            return self._architect_or_human(role, f"도구 '{t}' 사용")

        if req.kind == "permissions":
            return HUMAN, "샌드박스 권한 확장 요청"
        return HUMAN, "알 수 없는 요청"

    def autopilot(self, req: ApprovalRequest) -> tuple[str, str]:
        """전권 자동 모드: 사람·설계자 확인 대상도 자동 허용하되 금지 목록만 막는다."""
        pats = self.conf.get("autopilot_deny", AUTOPILOT_DENY)
        if req.kind == "command" and any(re.search(p, req.command or "") for p in pats):
            return DENY, "전권 자동 모드에서도 막는 명령입니다 (push·sudo·시스템 삭제·배포는 사람이 직접)."
        return AUTO, "전권 자동 모드 (사람이 모든 권한을 위임)"

    def _architect_or_human(self, role: Role, reason: str) -> tuple[str, str]:
        if role.name == self.main_role:
            return HUMAN, reason + " (설계자 본인 요청)"
        return ARCHITECT, reason

    def remember(self, req: ApprovalRequest) -> None:
        if self.verification_command(req) or (self.task and self.task["phase"] in ("plan", "plan_review")):
            return
        self.session_allow.add(req.cache_key())
