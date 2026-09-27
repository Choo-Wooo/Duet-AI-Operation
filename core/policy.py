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
    r"(?i)-Verb\s+RunAs\b|\bformat(\.com)?\s+[a-z]:",  # Windows 관리자 권한 실행·디스크 포맷
]
# 읽기 전용 명령 (모든 역할·모든 단계에서 파일로 리다이렉트하지 않으면 자동 허용)
READ_COMMAND = re.compile(
    r"^(?:ls|pwd|cat|head|tail|wc|grep|egrep|rg|find|tree|which|diff|stat|file|du|df|sort|uniq|cut|tr|nl|column|"
    r"comm|paste|xxd|od|hexdump|jq|basename|dirname|realpath|readlink|date|whoami|uname|ps|test|true|echo|"
    r"printf|git (?:status|diff|log|show|branch|rev-parse|ls-files|blame|grep))\b"
    r"|^sed(?![^|]*\s-i)\b"            # sed (단 -i 제자리 수정 제외)
    r"|^awk(?![^|]*system\s*\()\b"    # awk (단 system() 호출 제외)
)
# find 의 파일 변경 옵션
_FIND_WRITES = re.compile(r"\s-(?:delete|exec|execdir|ok|fls|fprint\w*)\b")
# 경로 규칙(~, /etc, /usr …): 읽기 전용 명령에는 적용하지 않는다
PATH_RULE_HINT = "/etc"
TRUSTED_BIN_DIRS = {"/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin"}


def _command_name(word: str) -> str:
    # Do not resolve or normalize '..': only these literal prefixes are trusted.
    directory, _, name = word.rpartition("/")
    return name if directory in TRUSTED_BIN_DIRS else word


def _plain_command(part: str) -> bool:
    first = shlex.split(part)[0]
    return "/" not in first and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", first)


def _branch_is_read(args: list[str]) -> bool:
    listing = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg in {"-l", "--list"}:
            listing = True
        elif arg in {"-a", "-r", "-v", "-vv", "--show-current"}:
            pass
        elif arg in {"--contains", "--merged"}:
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                i += 1
        elif arg.startswith(("--contains=", "--merged=")):
            pass
        elif arg.startswith("-") or not listing:
            return False
        i += 1
    return True


def _has_output_operand(name: str, args: list[str]) -> bool:
    """xxd/uniq accept an output file after the input; unknown options need review."""
    values = {"-c", "-g", "-l", "-o", "-s"} if name == "xxd" else {"-f", "-s", "-w", "--skip-fields", "--skip-chars", "--check-chars"}
    flags = ({"-a", "-b", "-e", "-E", "-i", "-p", "-ps", "-r", "-u"} if name == "xxd" else
             {"-c", "-d", "-u", "-i", "-z", "--count", "--repeated", "--unique", "--ignore-case", "--zero-terminated"})
    operands, i, options = 0, 0, True
    while i < len(args):
        arg = args[i]
        if options and arg == "--":
            options = False
        elif options and arg in values:
            i += 1
            if i == len(args):
                return True
        elif options and arg in flags:
            pass
        elif options and any(arg.startswith(opt + "=") if opt.startswith("--") else
                             arg.startswith(opt) and len(arg) > len(opt) for opt in values):
            pass
        elif options and arg.startswith("-") and arg != "-":
            return True
        else:
            operands += 1
        i += 1
    return operands > 1


def is_read_command(part: str) -> bool:
    toks = shlex.split(part)
    if not toks:
        return False
    name = " ".join(toks[:2]) if toks[0] == "git" else toks[0]
    return bool(READ_COMMAND.fullmatch(name)) and not write_options(part) and not (toks[0] == "find" and _FIND_WRITES.search(part))


def write_options(part: str) -> bool:
    toks = shlex.split(part)
    if not toks:
        return False
    name, args = toks[0], toks[1:]
    if name == "sed":
        if any(a.startswith("--in-place") or (a.startswith("-") and not a.startswith(("--", "-e", "-f")) and "i" in a) for a in args):
            return True
        # Script files and execution/write commands require review. Conservatively
        # inspect scripts, including substitution flags, without executing sed.
        scripts = [re.sub(r"^-[nErsuz]*e", "", a) if re.match(r"^-[nErsuz]*e", a) else a.partition("=")[2]
                   if a.startswith("--expression=") else a for a in args]
        return any(a.startswith("--file") or re.match(r"^-[nErsuz]*f", a) for a in args) or any(
            re.search(r"(?:^|[;{}\n])\s*(?:[0-9,$/\\.\s!]*)(?:w|W|e)\b", a)
            or re.search(r"(?:/|!|\d)\s*[wWe](?:\s|$)", a)
            or re.search(r"s(.).*?\1.*?\1[^;\n]*[weW]", a)
            for a in scripts if not a.startswith("-"))
    if name == "awk":
        return any(">" in a or "|" in a or re.search(r"\bsystem\b", a) for a in args) or any(a.startswith("-f") for a in args)
    if name == "sort":
        return any(a.startswith(("--output", "--compress-program")) or (a.startswith("-") and not a.startswith("--") and "o" in a) for a in args)
    if name == "rg":
        return any(a == "--pre" or a.startswith("--pre=") for a in args)
    if name == "tree":
        return any(a.startswith("-") and not a.startswith("--") and "o" in a for a in args)
    if name in {"xxd", "uniq"}:
        return _has_output_operand(name, args)
    if name == "git" and args:
        sub, opts = args[0], args[1:]
        return ((sub in {"diff", "log", "show"} and any(a.startswith("--output") for a in opts))
                or (sub == "grep" and any(a.startswith("--open-files-in-pager") or a.startswith("-O") for a in opts))
                or (sub == "branch" and not _branch_is_read(opts)))
    return False


def _tokens(cmd: str) -> list[tuple[str, bool]]:
    """Keep raw quoted words separate from shell operators; shlex validates words."""
    out, buf, quote, i = [], [], None, 0
    def flush():
        if buf:
            raw = "".join(buf)
            shlex.split(raw)
            out.append((raw, False))
            buf.clear()
    while i < len(cmd):
        c = cmd[i]
        if quote:
            buf.append(c)
            if quote == '"' and c == "\\" and i + 1 < len(cmd):
                i += 1
                buf.append(cmd[i])
            elif c == quote:
                quote = None
            elif quote == '"' and (cmd.startswith("$(", i) or c == "`"):
                raise ValueError("shell substitution")
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif c == "\\" or c == "`" or cmd.startswith(("$(", "<(", ">(", "<<"), i):
            raise ValueError("ambiguous shell syntax")
        elif c in ";&|<>\n":
            flush()
            op = next((op for op in ("&&", "||", ">&", "<&", "&>", ">>") if cmd.startswith(op, i)), c)
            if op == "&":
                raise ValueError("background command")
            out.append((op, True))
            i += len(op) - 1
        elif c.isspace():
            flush()
        else:
            buf.append(c)
        i += 1
    if quote:
        raise ValueError("unclosed quote")
    flush()
    return out


# Windows 의 Codex·agy 는 명령을 PowerShell 로 실행한다. 읽기만 하는 cmdlet (별칭 포함)
PS_READ_CMDLETS = {
    "get-content", "gc", "type", "cat", "get-childitem", "gci", "ls", "dir", "select-string", "sls",
    "get-location", "gl", "pwd", "test-path", "get-item", "gi", "get-itemproperty", "gp", "resolve-path",
    "rvpa", "split-path", "join-path", "get-filehash", "get-date", "measure-object", "measure",
    "select-object", "select", "sort-object", "sort", "format-table", "ft", "format-list", "fl", "out-string",
    "write-output", "echo", "write-host", "get-command", "gcm", "rg", "findstr", "tree",
}
PS_EXES = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
PS_FLAGS = {"-noprofile", "-nop", "-nologo", "-noninteractive", "-noni"}


def _powershell_script(cmd: str) -> str | None:
    """`powershell.exe -NoProfile -Command '<스크립트>'` 형태면 스크립트를, 아니면 None."""
    try:
        words = shlex.split(cmd)
    except ValueError:
        return None
    if not words or re.split(r"[\\/]", words[0])[-1].lower() not in PS_EXES:
        return None
    i = 1
    while i < len(words) and words[i].lower() in PS_FLAGS:
        i += 1
    if i + 2 == len(words) and words[i].lower() in ("-command", "-c"):
        return words[i + 1]
    return None


def _powershell_segments(script: str) -> list[str] | None:
    """PowerShell 파이프라인을 ; | 로 나눈다. 변수·하위식·스크립트 블록·리다이렉트 등 해석이 필요한 구문이면 None."""
    parts, buf, quote, i = [], [], None, 0
    while i < len(script):
        c = script[i]
        if quote == "'":
            buf.append(c)
            if c == "'":
                if script.startswith("''", i):
                    buf.append("'")
                    i += 1
                else:
                    quote = None
        elif quote == '"':
            if c in "`$":
                return None  # 이스케이프·변수 확장
            buf.append(c)
            if c == '"':
                quote = None
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif c in ";|\n":
            if not "".join(buf).strip():
                return None
            parts.append("".join(buf).strip())
            buf = []
        elif c in "`$@&<>(){}[]#,":
            return None
        else:
            buf.append(c)
        i += 1
    if quote or not "".join(buf).strip():
        return None
    parts.append("".join(buf).strip())
    return parts


def powershell_read_only(cmd: str) -> bool:
    """PowerShell 로 실행되는 읽기 전용 명령인지 (powershell -Command 로 감싼 것 또는 Get-Content 같은 cmdlet)."""
    script = _powershell_script(cmd)
    if script is None:
        first = (cmd or "").strip().split(None, 1)[0].lower() if (cmd or "").strip() else ""
        if "-" not in first or first not in PS_READ_CMDLETS:
            return False  # 감싸지 않은 명령은 Get-Content 처럼 동사-명사 cmdlet 으로 시작할 때만
        script = cmd
    segments = _powershell_segments(script)
    if not segments:
        return False
    for seg in segments:
        name = seg.split(None, 1)[0].lower()
        if name == "git":
            try:
                if not (is_read_command(seg) and not write_options(seg)):
                    return False
            except ValueError:
                return False
        elif name not in PS_READ_CMDLETS:
            return False
        elif name == "rg" and re.search(r"(^|\s)--pre\b", seg):
            return False
    return True


def read_only_command(cmd: str) -> bool:
    """파일을 바꾸지 않는 읽기 명령(ls, cat, grep, git log …)만으로 이뤄졌는지. 해석이 모호하면 False."""
    if powershell_read_only(cmd):
        return True
    try:
        parts = split_commands(cmd or "")
        return bool(parts) and not writes_output(cmd) and all(
            (is_read_command(p) and not write_options(p)) or p.startswith("cd ") for p in parts)
    except (ValueError, RecursionError):
        return False


def read_only_decision(req: "ApprovalRequest", what: str = "이 세션") -> "Decision":
    """읽기 전용 세션(질문 콘솔·협의 답변)용 승인: 읽기 도구·읽기 명령만 허용."""
    if (req.kind == "tool" and req.tool in PLAN_TOOLS) or (req.kind == "command" and read_only_command(req.command or "")):
        return Decision(True, "읽기 전용 허용", by="policy")
    return Decision(False, f"{what}은 읽기 전용입니다. 파일 수정·실행은 본 작업에서 요청하세요.", by="policy")


def tool_matches(tool: str | None, patterns: list[str] | None) -> bool:
    return bool(tool) and any(fnmatch.fnmatchcase(tool, p) for p in (patterns or []))


def writes_output(cmd: str) -> bool:
    """파일로 리다이렉트하는지 (2>&1, >/dev/null 은 제외, 따옴표 안의 > 는 무시)."""
    try:
        tokens = _tokens(cmd)
        for i, (token, operator) in enumerate(tokens):
            if operator and ">" in token:
                target = shlex.split(tokens[i + 1][0])[0] if i + 1 < len(tokens) and not tokens[i + 1][1] else ""
                if token == ">&" and target.isdigit():
                    continue
                if target != "/dev/null":
                    return True
        return any(shlex.split(p)[0] == "tee" or (p != cmd and writes_output(p)) for p in split_commands(cmd))
    except (ValueError, IndexError, RecursionError):
        return True


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
            return f"{self.role}|cmd|{' '.join(toks)}"
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
    parts, buf = [], []
    for token, operator in _tokens(cmd):
        if operator and token in ("&&", "||", ";", "|", "\n"):
            if not buf:
                raise ValueError("empty command")
            parts.append(" ".join(buf))
            buf = []
        else:
            buf.append(token)
    if buf:
        parts.append(" ".join(buf))
    else:
        raise ValueError("empty command")
    return parts


def split_commands(cmd: str, _depth: int = 0) -> list[str]:
    if _depth > 32:
        raise ValueError("shell nesting limit")
    words = shlex.split(cmd)
    if words and _command_name(words[0]) in ("bash", "sh", "zsh") and len(words) != 3:
        raise ValueError("ambiguous shell wrapper")
    out = []
    for part in _split_unquoted(cmd.strip()):
        toks = shlex.split(part)
        if not toks:
            raise ValueError("empty command")
        name = _command_name(toks[0])
        if name in ("bash", "sh", "zsh"):
            if len(toks) != 3 or toks[1] not in ("-c", "-lc"):
                raise ValueError("ambiguous shell wrapper")
            out.extend(split_commands(toks[2], _depth + 1))
        else:
            if name != toks[0]:
                raw_name = _tokens(part)[0][0]
                part = name + part[len(raw_name):]
            out.append(part)
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
        if cmd == test:
            return True
        if not cmd.startswith(test):
            return False
        if cmd[len(test):] and not (cmd[len(test)].isspace() or cmd[len(test)] == "|"):
            return False
        rest = cmd[len(test):].strip()
        rest = re.sub(r"^2>&1(?=\s|\|)\s*", "", rest)
        if not rest.startswith("|") or rest.startswith("||"):
            return False
        try:
            tokens = _tokens(rest[1:])
            if any(op and token != "|" for token, op in tokens):
                return False
            tail = _split_unquoted(rest[1:])
            return bool(tail) and all(
                (shlex.split(p)[0] in {"head", "tail", "grep", "rg", "wc", "cat"}
                 or shlex.split(p)[:2] == ["sed", "-n"])
                and is_read_command(p) and not writes_output(p) for p in tail)
        except (ValueError, RecursionError):
            return False

    # ---- 경로 ----
    def _rel(self, p: str) -> str | None:
        try:
            ap = (self.project / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
            return Path(str(ap).casefold()).relative_to(Path(str(self.project).casefold())).as_posix()
        except (ValueError, OSError, RuntimeError):
            return None  # 프로젝트 밖

    @staticmethod
    def _match(rel: str, globs: list[str]) -> bool:
        def variants(pattern):
            yield pattern
            start = pattern.find("**/")
            if start >= 0:
                for suffix in variants(pattern[start + 3:]):
                    yield pattern[:start] + suffix
                    yield pattern[:start + 3] + suffix
        return any(fnmatch.fnmatchcase(rel.casefold(), pattern)
                   for g in globs for pattern in variants(g.casefold()))

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
        if req.kind == "command" and powershell_read_only(req.command or ""):
            hits = [r for r in self.human_res if r.search(req.command or "") and PATH_RULE_HINT not in r.pattern]
            if hits:
                return HUMAN, "위험 명령(삭제·push·네트워크·권한·프로젝트 밖 경로 등)"
            return AUTO, "읽기 전용 명령 (PowerShell)"
        if req.kind == "command":
            try:
                parsed_parts = split_commands(req.command or "")
            except (ValueError, RecursionError):
                return HUMAN, "해석이 모호한 셸 구문 (사람 확인)"
            if any(write_options(p) for p in parsed_parts):
                return HUMAN, "읽기 명령의 쓰기·실행 옵션 (사람 확인)"
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
        read_only = role.permissions == "read_only"
        cached = not plan and req.cache_key() in self.session_allow
        if cached and req.kind not in ("file", "command"):
            return AUTO, "이번 세션에서 이미 허용한 요청"

        if req.kind == "file":
            rels = [self._rel(p) for p in req.paths] or [None]
            if any(r is None for r in rels):
                return HUMAN, "프로젝트 밖 경로에 쓰기"
            if any(self._match(r, self.conf.get("protected_paths", [])) for r in rels):
                return HUMAN, "보호된 경로(.env, 키, .git 등)에 쓰기"
            if cached:
                return AUTO, "이번 세션에서 이미 허용한 요청"
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
            if not plan and req.cache_key() in self.session_allow:
                return AUTO, "이번 세션에서 이미 허용한 요청"
            if read_only_cmd:
                return AUTO, "읽기 전용 명령"
            if not redirect and parts and all(
                    (_plain_command(p) and any(r.search(p) for r in self.auto_res) and not _FIND_WRITES.search(p)
                     and not (read_only and re.match(r"^(?:mkdir|touch|cp|mv)\b", p)))
                    or is_read_command(p) or p.startswith("cd ")
                    for p in parts):
                return AUTO, "안전한 명령"
            if read_only:
                return HUMAN, f"'{role.name}' 역할의 목록 밖 명령 (사람 확인 — 승인 창에서 세션 허용 가능)"
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
