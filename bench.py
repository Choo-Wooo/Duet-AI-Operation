"""무인 벤치마크 실행 (`python3 duet --bench`).

과제 하나를 사람 없이 끝까지 진행하고, 채점용 커밋과 지표(JSON)를 남긴 뒤 종료한다.
DeepSWE·Terminal-Bench 같은 벤치마크 실행기(Pier/Harbor)가 컨테이너 안에서 부르는 용도다.

- 전권 자동(full_auto)으로 켜고 autopilot 모드(턴 무제한)로 진행한다.
- 설계자가 STATUS done 으로 끝냈고 남은 병렬 작업이 없으면 끝난 것으로 본다.
  사람에게 넘긴 채 멈추면 "사람은 없다"는 안내를 몇 번 보내 스스로 마무리하게 한다.
- 끝나면 duet 이 만든 파일(DIALOGUE.md, .duet/, duet 폴더, .gitignore 의 duet 줄, docs/ 의 duet 기록)을
  채점 대상 변경에서 빼고 마지막 커밋을 만든다. 채점기는 기준 커밋과 HEAD 의 차이만 본다.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .core.config import Config
from .core.events import Event, EventBus
from .core.orchestrator import Orchestrator
from .core.policy import ApprovalRequest, Decision

PREAMBLE = """[무인 벤치마크 실행] 사람은 자리에 없고 이 실행이 끝날 때까지 응답하지 않습니다. 아래 과제를 끝까지 완성하세요.
- 사람에게 묻지 말고 과제 설명과 코드베이스에 근거해 스스로 결정하세요 (ASK_HUMAN 을 쓰지 마세요).
- 과제가 요구하는 동작을 실제로 실행해 검증하고, 기존 테스트가 깨지지 않게 하세요. 숨겨진 테스트로 채점됩니다.
- {parallel}
- 모든 변경은 이 저장소의 커밋으로 남아야 채점됩니다 (duet 이 턴마다 스냅샷 커밋합니다). 원격 push 는 하지 마세요.
- 모두 끝나면 한 일을 요약하고 STATUS done 으로 마치세요.

--- 과제 ---
"""
PARALLEL_ON = ("서로 독립적인 부분으로 나눌 수 있으면 duet-work 블록으로 병렬 작업을 맡기세요 (동시에 최대 {n}개). "
               "나누기 어려운 작은 과제는 한 번에 위임해도 됩니다.")
PARALLEL_OFF = "이번 실행은 병렬 작업을 쓰지 않습니다. 작업은 한 번에 하나씩 위임하세요."
NUDGE = ("[무인 벤치마크] 사람은 응답할 수 없습니다. 남은 일(대기·차단된 병렬 작업 포함)을 스스로 판단해 처리하세요. "
         "과제를 모두 구현하고 검증했으면 한 일을 요약하고 STATUS done 으로 마치세요.")

# duet 이 프로젝트에 남기는 기록. 채점 대상 변경에서 뺀다.
DUET_PATHS = ("DIALOGUE.md", "DIALOGUE-archive", ".duet")
DUET_DOC_PREFIXES = ("docs/plans/", "docs/work/", "docs/design/", "docs/research/", "docs/reviews/")
DUET_DOC_FILES = ("docs/design.md",)


class BenchUI:
    """전권 자동이라 거의 불리지 않는다. 불리면 사람 없이 진행 쪽으로 답한다."""

    async def ask_approval(self, req: ApprovalRequest, reason: str, opinion: str | None) -> Decision:
        return Decision(False, "무인 벤치마크: 사람 확인이 필요한 요청은 거부", by="bench")

    async def ask_choice(self, title: str, body: str, options: list[tuple[str, str]]) -> str:
        return options[0][0]


class Metrics:
    """버스 이벤트에서 지표를 모은다."""

    def __init__(self) -> None:
        self.turns: dict[str, int] = {}
        self.cost_usd = 0.0
        self.tokens = 0
        self.tokens_by_role: dict[str, int] = {}
        self.approvals: dict[str, int] = {}
        self.denied = 0
        self.errors: list[str] = []
        self.asks = 0
        self.warnings = 0
        self.last_main_directives: list[tuple[str, str]] = []
        self.main = ""

    def on_event(self, ev: Event) -> None:
        d = ev.data or {}
        role = (ev.role or "").split("#", 1)[0]
        if ev.kind == "turn_end":
            self.turns[role] = self.turns.get(role, 0) + 1
            if ev.role == self.main:
                self.last_main_directives = [tuple(x) for x in d.get("directives") or []]
        elif ev.kind == "usage":
            self.cost_usd += float(d.get("cost_usd") or 0)
            used = int(d.get("tokens") or 0)
            self.tokens += used
            self.tokens_by_role[role] = self.tokens_by_role.get(role, 0) + used
        elif ev.kind == "approval":
            key = f"{d.get('tier')}:{d.get('by')}"
            self.approvals[key] = self.approvals.get(key, 0) + 1
            if not d.get("allow"):
                self.denied += 1
        elif ev.kind == "error":
            if len(self.errors) < 50:
                self.errors.append(f"{ev.role or 'duet'}: {str(d.get('text'))[:300]}")
        elif ev.kind == "ask":
            self.asks += 1
        elif ev.kind == "notice" and d.get("level") == "warn":
            self.warnings += 1

    def main_done(self) -> bool:
        return any(k == "STATUS" and (v or "").strip().lower().startswith("done") for k, v in self.last_main_directives)


def _git(project: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=project, capture_output=True, text=True, check=check)


def head_commit(project: Path) -> str | None:
    r = _git(project, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def _in_commit(project: Path, commit: str | None, path: str) -> bool:
    return bool(commit) and _git(project, "cat-file", "-e", f"{commit}:{path}").returncode == 0


def _strip_gitignore(project: Path, base: str | None, duet_dirname: str) -> None:
    """.gitignore 에서 duet 이 덧붙인 줄만 뺀다 (과제가 고친 줄은 둔다)."""
    gi = project / ".gitignore"
    if not gi.exists():
        return
    base_lines = set()
    if _in_commit(project, base, ".gitignore"):
        base_lines = set(_git(project, "show", f"{base}:.gitignore").stdout.splitlines())
    ours = {"# duet", f"/{duet_dirname}/", ".duet/venv/", ".duet/logs/", ".duet/state.json", ".duet/saves/",
            ".duet/worktrees/", ".duet/work.json", ".duet/models.json", ".duet/web.json", ".duet/memory/",
            ".duet/asks/", ".duet/work/"}
    lines = gi.read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in lines if ln in base_lines or ln not in ours]
    while kept and not kept[-1].strip():
        kept.pop()
    if _in_commit(project, base, ".gitignore"):
        base_text = _git(project, "show", f"{base}:.gitignore").stdout
        if [ln for ln in kept if ln.strip()] == [ln for ln in base_text.splitlines() if ln.strip()]:
            gi.write_text(base_text, encoding="utf-8")
            return
    if not any(ln.strip() for ln in kept) and not _in_commit(project, base, ".gitignore"):
        gi.unlink()
        return
    gi.write_text("\n".join(kept) + "\n", encoding="utf-8")


def finalize(project: Path, base: str | None, duet_dirname: str, message: str = "duet bench: 최종 결과") -> dict:
    """작업 트리의 결과를 커밋하고 duet 기록을 채점 대상에서 뺀다. 최종 커밋과 변경 통계를 돌려준다."""
    out: dict[str, Any] = {"base": base}
    if _git(project, "rev-parse", "--is-inside-work-tree").returncode != 0:
        out["error"] = "git 저장소가 아닙니다"
        return out
    ident = ["-c", "user.name=duet", "-c", "user.email=duet@localhost"]
    _git(project, "add", "-A")                       # 커밋하지 않은 결과도 담는다 (duet 폴더는 .gitignore 로 제외)
    _strip_gitignore(project, base, duet_dirname)
    if (project / ".gitignore").exists():
        _git(project, "add", "--", ".gitignore")
    else:
        _git(project, "rm", "-q", "--cached", "--ignore-unmatch", "--", ".gitignore")
    staged = [p for p in _git(project, "ls-files", "-z").stdout.split("\0") if p]
    targets = set()
    for p in staged:
        top = p.split("/", 1)[0]
        if top in DUET_PATHS or top == duet_dirname or p in DUET_DOC_FILES or p.startswith(DUET_DOC_PREFIXES):
            targets.add(p)
    for p in sorted(targets):
        if _in_commit(project, base, p):
            _git(project, "restore", "--staged", f"--source={base}", "--", p)
        else:
            _git(project, "rm", "-q", "--cached", "--", p)
    if _git(project, "diff", "--cached", "--quiet").returncode != 0:
        r = _git(project, *ident, "commit", "-q", "--no-verify", "-m", message)
        if r.returncode != 0:
            out["error"] = (r.stderr or r.stdout).strip()[:500]
    out["head"] = head_commit(project)
    if base:
        out["diff_stat"] = _git(project, "diff", "--shortstat", base, "HEAD").stdout.strip()
        out["files"] = [p for p in _git(project, "diff", "--name-only", base, "HEAD").stdout.splitlines() if p]
    return out


def _quiet(orch: Orchestrator) -> bool:
    return (not orch.running and orch.inbox.empty() and not orch.work.busy() and not orch._pending_report
            and not orch._input_pending and not orch.pending_approvals and not orch.running_role)


def apply_role_models(cfg: Config, specs: list[str]) -> list[str]:
    """`역할=cli/모델` 지정으로 역할의 CLI·모델을 바꾼다."""
    done = []
    for spec in specs or []:
        name, _, value = spec.partition("=")
        name, value = name.strip(), value.strip()
        if name not in cfg.roles or not value:
            raise ValueError(f"--role-model 형식: 역할=cli/모델 (알 수 없는 역할: {name or spec})")
        cli, _, model = value.partition("/")
        role = cfg.roles[name]
        if cli:
            role.cli = cli
        role.model = model or None
        done.append(f"{name}={role.cli}/{role.model or '(기본)'}")
    if done:
        cfg.save_roles()
    return done


async def run_bench(cfg: Config, bus: EventBus, msgs: list[str], fake: bool, task: str, *,
                    out: Path | None = None, parallel: int | None = None, timeout: float = 10200.0,
                    nudges: int = 3, poll: float = 1.0, settle: float = 3.0, duet_dirname: str = "duet",
                    log=print) -> dict:
    project = cfg.project
    started = time.time()
    base = head_commit(project)
    if parallel is not None:
        cfg.settings["max_parallel"] = max(1, int(parallel))
    if os.environ.get("DUET_BENCH_KEEP_MCP") != "1":
        for role in cfg.roles.values():  # 과제 컨테이너는 인터넷이 막혀 npx 로 받는 MCP(브라우저 도구 등)를 띄울 수 없다
            role.mcp = {}
    cfg.settings["full_auto"] = True
    cfg.settings["auto_merge"] = True
    if "autopilot" in cfg.modes:
        cfg.state.mode = "autopilot"
    cfg.state.max_turns = None
    cfg.state.auto = True
    cfg.save_roles()
    cfg.save_state()
    n_parallel = int(cfg.settings.get("max_parallel") or 1)
    prompt = PREAMBLE.format(parallel=PARALLEL_ON.format(n=n_parallel) if n_parallel > 1 else PARALLEL_OFF) + task

    metrics = Metrics()
    metrics.main = cfg.main
    bus.subscribe(metrics.on_event)
    orch = Orchestrator(cfg, bus, BenchUI(), fake=fake)
    for m in msgs:
        log("· " + m)
    log(f"[bench] 시작: 병렬 {n_parallel}, 제한 {int(timeout)}초, 기준 {base or '(커밋 없음)'}")
    server = asyncio.create_task(orch.serve())
    orch.submit(prompt)
    reason, used, quiet_for = "done", 0, 0.0
    await asyncio.sleep(poll)
    try:
        while True:
            await asyncio.sleep(poll)
            if server.done():
                reason = "error"
                break
            if time.time() - started > timeout:
                reason = "timeout"
                break
            if not _quiet(orch):
                quiet_for = 0.0
                continue
            quiet_for += poll
            if quiet_for < settle:
                continue
            if (metrics.main_done() and not orch.work.active()) or used >= nudges:
                reason = "done" if metrics.main_done() and not orch.work.active() else "stalled"
                break
            used += 1
            log(f"[bench] 사람 차례로 멈춰 스스로 마무리하도록 안내합니다 ({used}/{nudges}).")
            metrics.last_main_directives = []
            orch.submit(NUDGE)
            quiet_for = 0.0
    finally:
        try:
            await asyncio.wait_for(orch.stop(), 30)
        except Exception:  # noqa: BLE001
            pass
        server.cancel()
        try:
            await asyncio.wait_for(orch.work.close(), 60)
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(orch.close(), 60)
        except Exception:  # noqa: BLE001
            pass

    work = orch.work.snapshot()
    git = finalize(project, base, duet_dirname)
    result = {
        "finished": reason,
        "elapsed_sec": round(time.time() - started, 1),
        "max_parallel": n_parallel,
        "nudges": used,
        "roles": {n: f"{r.cli}/{r.model or ''}" for n, r in cfg.roles.items()},
        "turns": metrics.turns,
        "turns_total": sum(metrics.turns.values()),
        "cost_usd": round(metrics.cost_usd, 4),
        "tokens": metrics.tokens,
        "tokens_by_role": metrics.tokens_by_role,
        "approvals": metrics.approvals,
        "denied": metrics.denied,
        "asks": metrics.asks,
        "warnings": metrics.warnings,
        "errors": metrics.errors,
        "work_items": [{k: w.get(k) for k in ("id", "role", "status", "rounds", "reworks", "merged_commit")} for w in work],
        "work_merged": sum(1 for w in work if w.get("merged_commit")),
        "git": git,
    }
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"[bench] 끝: {reason}, {result['elapsed_sec']}초, 턴 {result['turns_total']}, 병렬 작업 {len(work)}개"
        f" (병합 {result['work_merged']}), 변경 {git.get('diff_stat') or '없음'}")
    return result
