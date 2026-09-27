"""병렬 작업 보드: 설계자가 나눈 작업을 역할별 세션 여러 개가 동시에, 각자의 git 워크트리에서 진행한다.

흐름 (작업 하나당):
  queued → plan(작업자, 읽기 전용) ⇄ plan_review(설계자 분신) → implement(작업자, 워크트리 쓰기)
  → verify(duet 이 합의 테스트 실행 + 설계자 분신 검토) → merging(기준 브랜치 최신화·통합 테스트·squash 병합)
  → merged.  실패·보류는 waiting / failed / cancelled.

- 설계자(main)는 턴 끝에 ```duet-work``` YAML 블록으로 작업 목록·역할·의존 관계를 낸다. 역할별 동시 세션 수는
  roles.<역할>.max_sessions, 전체는 settings.max_parallel 안에서 설계자가 정한다.
- 작업마다 설계자 세션을 복제(fork)한 분신이 계획 검토·검증을 맡아, 설계자가 여러 세션과 동시에 협의한다.
- 작업 세션끼리는 CONSULT 로 서로(또는 설계자 분신에게) 물어볼 수 있다. 답은 대상 세션의 읽기 전용 복제본이 한다.
- 작업 기록: docs/work/<id>.md (메인 작업 트리). 상태: .duet/work.json
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .agreement import parse_plan
from .dialogue import extract_directives
from .policy import ApprovalRequest, Decision, Policy, AUTO, read_only_decision
from .prompts import BACKGROUND_WAIT
from .worktrees import GitError, Worktrees, valid_id

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

WORK_BLOCK = re.compile(r"```duet-work[ \t]*\n(?P<body>.*?)\n```", re.S)
ACTIVE = ("queued", "plan", "plan_review", "implement", "verify", "merging")
DONE = ("merged", "failed", "cancelled")
REPORT_TRIGGER = "\x00duet-work-report"
MAX_CONSULTS = 6
MAX_REWORK = 4

WORK_SYSTEM = BACKGROUND_WAIT + """
# duet 병렬 작업 규칙
당신은 duet 오케스트레이터 안에서 '{role}' 역할로 병렬 작업 `{id}` 하나를 맡은 세션입니다.
역할: {brief}
작업 폴더는 이 작업 전용 git 워크트리입니다 (브랜치 {branch}, 기준 브랜치 {base}).
- 이 폴더 안의 파일만 고치세요. git commit/merge/push 는 하지 마세요 (duet 이 커밋·병합합니다).
- DIALOGUE.md 는 쓰지 마세요. 당신의 채팅 응답을 duet 이 docs/work/{id}.md 에 기록합니다.
- 같은 시간에 다른 세션들이 다른 작업을 하고 있습니다. 합의한 파일 범위 밖을 고치지 마세요.
- 사람에게 직접 묻는 도구는 쓰지 말고 ASK_HUMAN 지시문을 쓰세요.

## 지시문 (응답 끝에 HTML 주석으로)
- `<!-- duet: PLAN ready -->` : (plan 단계) 계획 전문을 응답한 뒤
- `<!-- duet: REPORT done -->` / `<!-- duet: REPORT blocked <이유> -->` : (implement 단계) 결과 보고
- `<!-- duet: CONSULT <작업id|architect> <질문> -->` : 다른 작업 세션이나 설계자에게 묻기 (인터페이스·결정 확인용)
- `<!-- duet: ASK_HUMAN <질문> -->` : 사람에게 묻고 이 작업을 멈춤

## 계획 형식 (plan 단계)
이해한 요구, 설계와 다른 점과 이유, ```files 코드블록(한 줄에 파일 하나, 이 워크트리 기준 상대 경로), AC(수용 기준),
테스트↔AC 매핑(모의/실제 구분), `test_command: <한 줄>` (이 워크트리에서 실행), 다른 작업과의 인터페이스, 열린 질문.
"""

ARCH_WORK_SYSTEM = BACKGROUND_WAIT + """
# duet 병렬 작업 — 설계자 분신
당신은 설계자 세션을 복제한 분신으로, 병렬 작업 `{id}` 하나의 계획 검토·검증·질문 응답을 맡습니다.
작업 워크트리 경로: {worktree} (메인 프로젝트 기준). 파일은 이 경로 아래를 읽으세요. 코드는 고치지 않습니다.
DIALOGUE.md 는 쓰지 마세요. 응답은 duet 이 docs/work/{id}.md 에 기록합니다.
- plan_review: `<!-- duet: AGREE -->` 또는 `<!-- duet: REVISE <수정 요청> -->`
- verify: `<!-- duet: ACCEPT -->` 또는 `<!-- duet: REWORK <재작업 사유> -->`
- 질문 응답: 지시문 없이 답만
사람 판단이 필요하면 `<!-- duet: ASK_HUMAN <질문> -->`.
"""

PARALLEL_SYSTEM = """
## 병렬 작업 (duet-work)
서로 독립적으로 진행할 수 있는 작업이 여럿이면, 턴 끝에 아래 블록으로 여러 세션에 동시에 맡기세요.
역할별 세션 수는 당신이 정합니다 (역할별 max_sessions, 전체 동시 {max_parallel}개 이내. 넘치면 차례로 대기).
각 작업은 별도 git 워크트리·로컬 브랜치에서 진행되고, 당신의 분신이 계획 검토와 검증을 맡으며,
검증과 통합 테스트를 통과하면 기준 브랜치에 병합됩니다. 모든 작업이 끝나면 결과가 당신에게 보고됩니다.
```duet-work
- id: api-login          # 소문자·숫자·-
  role: implementer      # 위임할 역할 (메인 제외)
  task: 로그인 API 와 테스트 작성. 인터페이스는 docs/design/auth.md 를 따른다.
  files: [src/api/login.py, tests/test_login.py]   # 예상 범위 (선택)
- id: login-ui
  role: designer
  task: 로그인 화면 시안과 컴포넌트
  depends_on: [api-login]   # 먼저 병합돼야 하는 작업 (선택)
```
- 작업 사이 인터페이스(함수 이름·API 형태·파일 경계)는 블록을 내기 전에 docs/ 설계 문서로 고정하세요.
- 같은 파일을 두 작업이 동시에 고치지 않게 나누세요. 겹치면 depends_on 으로 순서를 주세요.
- 진행 중 작업 취소: `<!-- duet: CANCEL_WORK <id> <사유> -->`, 대기 작업 재개: `<!-- duet: RESUME_WORK <id> [작업자에게 줄 답·지시] -->`
- 병렬 작업이 도는 동안 DELEGATE 는 쓰지 마세요 (메인 작업 트리 충돌 방지).
"""


@dataclass
class WorkItem:
    id: str
    role: str
    task: str
    depends_on: list[str] = field(default_factory=list)
    files_hint: list[str] = field(default_factory=list)
    status: str = "queued"
    wait_reason: str = ""
    batch: int = 0
    from_turn: int = 0
    plan: str = ""
    plan_version: int = 0
    agreed_files: list[str] = field(default_factory=list)
    test_command: str = ""
    rounds: int = 0
    negotiations: int = 0
    negotiation_limit: int = 6
    reworks: int = 0
    consults: int = 0
    session_id: str | None = None
    arch_session_id: str | None = None
    merged_commit: str | None = None
    result: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    inbox: list[str] = field(default_factory=list)  # 사람이 이 작업에 보낸 메시지 (다음 턴에 전달)
    resume_at: str = ""  # 재개 시 바로 들어갈 단계 (verify / merging)

    @property
    def thread(self) -> str:
        return f"docs/work/{self.id}.md"


def parse_work_block(text: str | None) -> list[dict] | None:
    """응답에서 마지막 duet-work 블록을 찾아 목록으로. 블록이 없으면 None."""
    found = list(WORK_BLOCK.finditer(text or ""))
    if not found:
        return None
    data = yaml.safe_load(found[-1].group("body")) or []
    if isinstance(data, dict):
        data = data.get("work") or data.get("items") or [data]
    if not isinstance(data, list):
        raise ValueError("duet-work 블록은 작업 목록(YAML 리스트)이어야 합니다")
    return data


class WorkBoard:
    def __init__(self, orch: "Orchestrator"):
        self.orch = orch
        self.cfg = orch.cfg
        self.project = orch.project
        self.path = self.cfg.dir / "work.json"
        self.items: dict[str, WorkItem] = {}
        self.base: str | None = None
        self.batch = 0
        self.tasks: dict[str, asyncio.Task] = {}
        self.adapters: dict[str, Any] = {}  # key -> adapter (worker "id", architect "id@arch")
        self.merge_lock = asyncio.Lock()
        self.git_lock = orch.git.merge_lock
        self.closing = False
        self._started = False
        self.wt = Worktrees(self.project) if orch.git.enabled else None
        self.changed = asyncio.Event()
        self._report_pending = False
        self.load_warnings: list[str] = []
        self._load()

    def start(self) -> None:
        """Called once at the first async entry, after synchronous construction."""
        if not self._started and not self.closing:
            self._started = True
            self.schedule()

    # ---------------- 저장 ----------------
    def _load(self) -> None:
        from .storage import load_json
        def warn(text):
            self.load_warnings.append(text)
            self.orch.bus.emit('notice', None, text=text, level='warn')
        raw = load_json(self.path, warn)
        if raw is None:
            return
        self.base = raw.get("base")
        self.batch = int(raw.get("batch") or 0)
        for d in raw.get("items") or []:
            try:
                it = WorkItem(**{k: v for k, v in d.items() if k in WorkItem.__dataclass_fields__})
            except TypeError:
                continue
            if it.status in ACTIVE and it.status != "queued":
                it.wait_reason = f"duet 재시작으로 {it.status} 단계에서 멈춤 — RESUME_WORK {it.id} 로 재개"
                it.status = "waiting:" + it.status
            self.items[it.id] = it

    def save(self) -> None:
        from .storage import atomic_write
        data = {"base": self.base, "batch": self.batch, "items": [asdict(i) for i in self.items.values()]}
        try:
            atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2))
        except OSError as e:
            self.orch.bus.emit('notice', None, text=f'work.json 저장 실패: {e}', level='warn')

    def _touch(self, it: WorkItem, status: str | None = None, reason: str = "") -> None:
        if status:
            old = it.status
            it.status = status
            it.wait_reason = reason
            if old != status:
                it.__dict__.pop("_reported", None)
                self.orch.bus.emit("work", it.role + "#" + it.id, id=it.id, status=status, reason=reason)
        it.updated = time.time()
        self.save()
        self.changed.set()
        self.orch.emit_status()

    # ---------------- 조회 ----------------
    def active(self) -> bool:
        return any(i.status in ACTIVE or i.status == "blocked" or i.status.startswith("waiting")
                   for i in self.items.values())

    def busy(self) -> bool:
        return bool(self.tasks) or any(i.status in ACTIVE for i in self.items.values())

    def running(self) -> list[WorkItem]:
        return [i for i in self.items.values() if i.status in ACTIVE and i.status != "queued"]

    def snapshot(self) -> list[dict]:
        out = []
        for i in sorted(self.items.values(), key=lambda x: x.created):
            out.append({"id": i.id, "role": i.role, "task": i.task, "status": i.status, "wait_reason": i.wait_reason,
                        "depends_on": i.depends_on, "batch": i.batch, "agreed_files": i.agreed_files,
                        "test_command": i.test_command, "thread": i.thread, "merged_commit": i.merged_commit,
                        "rounds": i.rounds, "reworks": i.reworks, "updated": i.updated,
                        "branch": f"duet/work/{i.id}", "result": i.result,
                        "context": getattr(self.adapters.get(i.id), "context_tokens", 0)})
        return out

    def summary(self, batch: int | None = None) -> str:
        items = [i for i in self.items.values() if batch is None or i.batch == batch]
        lines = []
        for i in items:
            extra = f" ({i.wait_reason})" if i.wait_reason else ""
            commit = f" · 커밋 {i.merged_commit[:9]}" if i.merged_commit else ""
            lines.append(f"- {i.id} [{i.role}] {i.status}{commit}{extra} — 기록 {i.thread}"
                         + (f"\n  결과: {i.result}" if i.result else ""))
        return "\n".join(lines) or "(작업 없음)"

    # ---------------- 설계자 지시 처리 ----------------
    def submit(self, specs: list[dict], from_turn: int) -> list[str]:
        """duet-work 목록을 검증해 보드에 올린다. 오류 문구 목록을 돌려준다 (비어 있으면 성공)."""
        errors: list[str] = []
        if self.wt is None:
            return ["git 스냅샷이 꺼져 있거나 git 이 없어 병렬 작업을 쓸 수 없습니다. DELEGATE 로 순차 진행하세요."]
        why = self.wt.check_ready()
        if why:
            return [f"병렬 작업을 시작할 수 없습니다: {why}"]
        if self.cfg.state.task:
            return ["진행 중인 합의 작업(DELEGATE)이 있습니다. 끝내거나 CANCEL 한 뒤 병렬 작업을 내세요."]
        base = self.wt.current_branch()
        if self.base and self.base != base and self.active():
            return [f"진행 중인 병렬 작업의 기준 브랜치({self.base})와 현재 브랜치({base})가 다릅니다."]
        new: list[WorkItem] = []
        ids = set(self.items)
        for n, s in enumerate(specs, 1):
            if not isinstance(s, dict):
                errors.append(f"{n}번째 항목이 매핑이 아닙니다")
                continue
            wid, role, task = str(s.get("id") or "").strip(), str(s.get("role") or "").strip(), str(s.get("task") or "").strip()
            deps = s.get("depends_on") or []
            files = s.get("files") or []
            if not valid_id(wid):
                errors.append(f"id '{wid}': 소문자·숫자·- 로 40자 이내여야 합니다")
            elif wid in ids and self.items.get(wid) and self.items[wid].status not in DONE:
                errors.append(f"id '{wid}' 가 이미 진행 중입니다")
            elif any(x.id == wid for x in new):
                errors.append(f"id '{wid}' 가 중복됩니다")
            if role not in self.cfg.roles or role == self.cfg.main:
                errors.append(f"{wid}: 역할 '{role}' 에 맡길 수 없습니다 (가능: "
                              + ", ".join(r for r in self.cfg.roles if r != self.cfg.main) + ")")
            if not task:
                errors.append(f"{wid}: task 가 비었습니다")
            if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
                errors.append(f"{wid}: depends_on 은 id 목록이어야 합니다")
                deps = []
            if not isinstance(files, list):
                files = []
            new.append(WorkItem(id=wid, role=role, task=task, depends_on=list(deps),
                                files_hint=[str(f) for f in files], from_turn=from_turn))
        known = {i.id for i in new} | set(self.items)
        for it in new:
            for d in it.depends_on:
                if d not in known:
                    errors.append(f"{it.id}: depends_on '{d}' 작업이 없습니다")
        if not errors and self._has_cycle(new):
            errors.append("depends_on 에 순환이 있습니다")
        if errors:
            return errors
        self.base = base
        self.batch += 1
        msg = self.wt.install_pre_push()
        if msg:
            self.orch.notice(msg, "warn")
        # 메인 작업 트리 변경을 먼저 커밋해 워크트리가 최신 내용에서 출발하게 한다
        self.orch.git.snapshot(f"duet: 병렬 작업 {self.batch}차 시작 전 스냅샷")
        for it in new:
            if it.id in self.items and self.items[it.id].status in DONE:
                self.items.pop(it.id)
            it.batch = self.batch
            self.items[it.id] = it
            self._thread(it, "작업 등록", f"역할: {it.role}\n의존: {', '.join(it.depends_on) or '없음'}\n\n{it.task}")
        self.save()
        self.orch.notice(f"병렬 작업 {len(new)}개를 받았습니다 (기준 브랜치 {base}): " + ", ".join(i.id for i in new))
        self.schedule()
        return []

    def _has_cycle(self, new: list[WorkItem]) -> bool:
        graph = {i.id: i.depends_on for i in list(self.items.values()) + new}
        state: dict[str, int] = {}

        def visit(n: str) -> bool:
            if state.get(n) == 1:
                return True
            if state.get(n) == 2:
                return False
            state[n] = 1
            if any(visit(d) for d in graph.get(n, [])):
                return True
            state[n] = 2
            return False
        return any(visit(n) for n in graph)

    def handle_directives(self, directives: list[tuple[str, str]]) -> list[str]:
        out = []
        for k, v in directives:
            if k == "CANCEL_WORK":
                wid, _, why = v.partition(" ")
                out.append(self.cancel(wid, why or "설계자 취소"))
            elif k == "RESUME_WORK":
                wid, _, text = v.partition(" ")
                if text.strip() and wid in self.items:  # 대기 사유(질문)에 대한 답을 함께 전달
                    self.items[wid].inbox.append(f"설계자: {text.strip()}")
                    self._thread(self.items[wid], "설계자", text.strip())
                out.append(self.resume(wid))
        return out

    def cancel(self, wid: str, reason: str) -> str:
        it = self.items.get(wid)
        if not it or it.status in DONE:
            return f"취소할 작업 '{wid}' 가 없습니다."
        t = self.tasks.pop(wid, None)
        if t:
            t.cancel()
        self._touch(it, "cancelled", reason)
        self._thread(it, "취소", reason)
        if self.wt:
            self.wt.remove(wid)
        self.schedule()
        return f"{wid} 취소"

    def resume(self, wid: str) -> str:
        it = self.items.get(wid)
        if not it or not it.status.startswith("waiting"):
            return f"재개할 대기 작업 '{wid}' 가 없습니다."
        phase = it.status.split(":", 1)[1] if ":" in it.status else "plan"
        if it.negotiations >= it.negotiation_limit:
            it.negotiation_limit += 6
        it.resume_at = phase if phase in ("verify", "merging") and it.agreed_files else ""
        self._touch(it, "queued")
        self.schedule()
        return f"{wid} 재개"

    def message(self, wid: str, text: str) -> str:
        it = self.items.get(wid)
        if not it or it.status in DONE:
            return f"작업 '{wid}' 가 없습니다."
        it.inbox.append(text)
        self._thread(it, "사람", text)
        self.save()
        if it.status.startswith("waiting"):
            return self.resume(wid) + " — 메시지는 다음 턴에 전달합니다."
        return f"{wid} 에 전달 예약 (다음 턴)"

    # ---------------- 스케줄 ----------------
    def schedule(self) -> None:
        if self.closing:
            return
        # Compute dependency blockage independent of insertion order, including chains.
        blocked = {i.id for i in self.items.values()
                   if i.status in ("failed", "cancelled") or i.status.startswith("waiting")}
        while True:
            more = {i.id for i in self.items.values() if i.status in ("queued", "blocked")
                    and any(d not in self.items or d in blocked for d in i.depends_on)}
            if more <= blocked:
                break
            blocked.update(more)
        for it in self.items.values():
            if it.status not in ("queued", "blocked"):
                continue
            if it.id in blocked:
                reason = "선행 작업 차단: " + ", ".join(
                    f"{d} ({'missing' if d not in self.items else 'blocked' if self.items[d].status == 'queued' else self.items[d].status})"
                    for d in it.depends_on if d not in self.items or d in blocked)
                if it.status != "blocked" or it.wait_reason != reason:
                    self._touch(it, "blocked", reason)
            elif it.status == "blocked":
                self._touch(it, "queued")
        cap = int(self.cfg.settings.get("max_parallel") or 4)
        running = [i for i in self.items.values() if i.id in self.tasks]
        for it in sorted(self.items.values(), key=lambda x: x.created):
            if it.status != "queued" or it.id in self.tasks:
                continue
            deps = [self.items.get(d) for d in it.depends_on]
            if any(d.status != "merged" for d in deps if d):
                continue
            role = self.cfg.roles.get(it.role)
            if role is None:
                self._touch(it, "waiting:queued", f"역할 {it.role} 이 없습니다")
                continue
            same = sum(1 for r in running if r.role == it.role)
            if len(running) >= cap or same >= max(1, role.max_sessions):
                continue
            running.append(it)
            self.tasks[it.id] = asyncio.create_task(self._run(it))
        self._maybe_report()

    def _maybe_report(self) -> None:
        """이번 차수 작업이 모두 끝났거나 사람·설계자 판단이 필요한 작업만 남았으면 설계자에게 보고."""
        if self._report_pending or not self.items:
            return
        live = [i for i in self.items.values() if i.id in self.tasks or i.status == "queued"]
        if live:
            return
        unreported = [i for i in self.items.values() if not getattr(i, "_reported", False)
                      and (i.status in DONE or i.status == "blocked" or i.status.startswith("waiting"))]
        if not unreported:
            return
        for i in unreported:
            setattr(i, "_reported", True)
        self._report_pending = True
        self.orch.inbox.put_nowait((None, REPORT_TRIGGER))

    def report_prompt(self) -> str:
        self._report_pending = False
        return ("병렬 작업 보고입니다. 각 작업의 기록(docs/work/<id>.md)을 필요한 만큼 읽고, 결과를 설계와 대조해 다음 단계를 정하세요. "
                "대기 작업은 RESUME_WORK/CANCEL_WORK 로 처리하고, 추가 작업이 있으면 새 duet-work 블록을 내세요. "
                "모두 끝났으면 사람에게 요약하고 STATUS done.\n\n기준 브랜치: " + str(self.base) + "\n" + self.summary())

    # ---------------- 기록 ----------------
    def _thread(self, it: WorkItem, who: str, text: str) -> None:
        path = self.project / it.thread
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            head = "" if path.exists() else f"# 병렬 작업 {it.id}\n\n역할 {it.role} · 브랜치 duet/work/{it.id} · 기준 {self.base}\n\n"
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{head}## [{who}] {time.strftime('%H:%M:%S')}\n\n{text.strip()}\n\n")
        except OSError:
            pass

    # ---------------- 세션 ----------------
    def _worker_role(self, it: WorkItem):
        return replace(self.cfg.roles[it.role], name=f"{it.role}#{it.id}")

    async def _worker(self, it: WorkItem, path: Path):
        ad = self.adapters.get(it.id)
        if ad:
            return ad
        from ..adapters import make_adapter
        role = self._worker_role(it)
        from .prompts import role_guide
        system = WORK_SYSTEM.format(role=it.role, id=it.id, brief=role.brief, branch=f"duet/work/{it.id}",
                                    base=self.base) + role_guide(it.role)
        pol = Policy(path, self.cfg.policy, self.cfg.main)
        ad = make_adapter(role, path, self.orch.bus, lambda req: self._approve(it, pol, req), it.session_id,
                          system, fake=self.orch.fake)
        ad.context_limit = self.orch.context_limit(it.role)
        ad.approval_timeout = float(self.cfg.policy.get('human_approval_timeout_sec', 3600))
        await self._start_activity(ad)
        self.adapters[it.id] = ad
        return ad

    async def _architect(self, it: WorkItem, path: Path):
        key = it.id + "@arch"
        ad = self.adapters.get(key)
        if ad:
            return ad
        from ..adapters import make_adapter
        main = self.cfg.main_role()
        role = replace(main, name=f"{main.name}#{it.id}")
        base_session = it.arch_session_id or self.cfg.state.sessions.get(main.name)
        system = ARCH_WORK_SYSTEM.format(id=it.id, worktree=path.relative_to(self.project).as_posix())
        ad = make_adapter(role, self.project, self.orch.bus, self.orch.handle_approval, base_session, system,
                          fake=self.orch.fake, fork_session=bool(base_session) and not it.arch_session_id)
        await self._start_activity(ad)
        self.adapters[key] = ad
        return ad

    async def _start_activity(self, ad):
        if '#' in ad.label:
            base, ident = ad.label.split('#', 1)
            self.orch.work_sessions[ad.label] = dict(id=ident.split('(', 1)[0], role=base,
                session=ad.label, phase='starting', started_at=time.time())
        self.orch.set_activity(ad.label, 'starting', start=True)
        try:
            await ad.start()
        except BaseException as e:
            self.orch.work_sessions.pop(ad.label, None)
            self.orch.set_activity(ad.label, 'error', str(e))
            raise

    async def _approve(self, it: WorkItem, pol: Policy, req: ApprovalRequest) -> Decision:
        phase = it.status if it.status in ("plan", "plan_review") else "implement"
        pol.task = {"phase": phase, "waiting": False, "test_command": it.test_command}
        return await self.orch.handle_approval(req, policy=pol)

    async def _close(self, it: WorkItem) -> None:
        for key in (it.id, it.id + "@arch"):
            ad = self.adapters.pop(key, None)
            if ad:
                try:
                    await ad.close()
                except Exception:
                    pass
                self.orch.work_sessions.pop(ad.label, None)
                if self.orch.activity.get(ad.label, {}).get('state') not in ('done', 'error', 'idle'):
                    self.orch.set_activity(ad.label, 'idle')

    async def _turn(self, ad, it: WorkItem, kind: str, prompt: str, who: str):
        await self.orch.not_paused.wait()
        ad.turn_kind = kind
        if it.inbox and who != "설계자":
            prompt += "\n\n이 작업에 온 메시지 (사람·설계자):\n" + "\n".join(f"- {m}" for m in it.inbox)
            it.inbox.clear()
        self.orch.bus.emit("turn_start", ad.label, n=0, kind=kind, info=it.id)
        try:
            tr = await ad.run_turn(prompt)
        except BaseException as e:
            self.orch.bus.emit('turn_end', ad.label, ok=False, error=str(e),
                               interrupted=isinstance(e, asyncio.CancelledError))
            raise
        self.orch.bus.emit("turn_end", ad.label, n=0, summary=(tr.text or "")[:200], directives=[], ok=tr.ok,
                           error=tr.error, interrupted=tr.interrupted)
        full = tr.full_text if tr.full_text is not None else tr.text
        self._thread(it, f"{who} · {kind}", full or f"(오류) {tr.error}")
        if not tr.ok:
            raise RuntimeError(tr.error or "턴 실패")
        return tr, full or "", extract_directives(full or "")

    # ---------------- 한 작업의 진행 ----------------
    async def _run(self, it: WorkItem) -> None:
        try:
            assert self.wt and self.base
            path = await asyncio.to_thread(self.wt.create, it.id, self.base)
            if not it.agreed_files:
                await self._plan(it, path)
            await self._implement_loop(it, path)
        except asyncio.CancelledError:
            raise
        except _Wait as w:
            self._touch(it, "waiting:" + w.phase, w.reason)
            self.orch.bus.emit("ask", it.role + "#" + it.id, text=f"[{it.id}] {w.reason}")
        except Exception as e:
            self._touch(it, "failed", f"{type(e).__name__}: {e}")
            self._thread(it, "duet", f"작업 실패: {e}")
        finally:
            if it.status in DONE or it.status.startswith("waiting"):
                self._save_sessions(it)
                await self._close(it)
            self.tasks.pop(it.id, None)
            self.schedule()

    def _save_sessions(self, it: WorkItem) -> None:
        w, a = self.adapters.get(it.id), self.adapters.get(it.id + "@arch")
        if w and w.session_id:
            it.session_id = w.session_id
        if a and a.session_id:
            it.arch_session_id = a.session_id
        self.save()

    def _others(self, it: WorkItem) -> str:
        rows = [f"- {o.id} [{o.role}] {o.status}: {o.task[:120]}" for o in self.items.values()
                if o.id != it.id and o.status not in DONE]
        return "\n".join(rows) or "(없음)"

    async def _plan(self, it: WorkItem, path: Path) -> None:
        limit = int(self.cfg.settings.get("plan_rounds") or 3)
        feedback = ""
        while True:
            # One submission (valid or invalid) plus its review is one negotiation.
            if it.negotiations >= it.negotiation_limit:
                self.orch.bus.emit('ask', it.role, text=f'{it.id}: 협상 6회 한도 — 사람 응답/RESUME_WORK 필요')
                raise _Wait('plan', '협상 6회 한도 — 사람 응답/RESUME_WORK 필요')
            it.negotiations += 1
            self._touch(it, "plan")
            worker = await self._worker(it, path)
            worker.plan_read_only = True
            prompt = (f"[duet] 병렬 작업 {it.id} · plan 단계 (읽기 전용)\n작업: {it.task}\n"
                      + (f"예상 범위: {', '.join(it.files_hint)}\n" if it.files_hint else "")
                      + f"동시에 진행 중인 다른 작업:\n{self._others(it)}\n"
                      + (f"\n설계자 수정 요청: {feedback}\n" if feedback else "")
                      + "\n코드를 읽고 계획 전문을 응답한 뒤 <!-- duet: PLAN ready --> 를 쓰세요. 파일은 쓰지 마세요.")
            _, full, dirs = await self._consultable(worker, it, "work_plan", prompt, path)
            self._check_ask(dirs, "plan")
            if ("PLAN", "ready") not in dirs:
                feedback = "계획 전문과 PLAN ready 지시문이 없습니다. 형식을 지켜 다시 제출하세요."
                it.rounds += 1
                if it.rounds > limit + 1:
                    raise _Wait("plan", "계획 제출 형식 오류가 반복됨")
                continue
            try:
                files, command = parse_plan(full)
            except ValueError as e:
                feedback = f"계획 형식 오류: {e}"
                it.rounds += 1
                if it.rounds > limit + 1:
                    raise _Wait("plan", f"계획 형식 오류 반복: {e}")
                continue
            it.plan, it.plan_version = full, it.plan_version + 1
            it.rounds += 1
            self._touch(it, "plan_review")
            arch = await self._architect(it, path)
            _, rfull, rdirs = await self._turn(
                arch, it, "work_review",
                f"[duet] 병렬 작업 {it.id} ({it.role}) 계획 v{it.plan_version} 검토\n원 작업: {it.task}\n"
                f"다른 작업:\n{self._others(it)}\n\n--- 계획 ---\n{full}\n--- 끝 ---\n"
                "요구 해석, 설계와의 차이, 파일 범위(다른 작업과 겹치지 않는지), AC, 테스트가 AC 를 검증하는지 보세요. "
                "AGREE 또는 REVISE <수정 요청>.", "설계자")
            self._check_ask(rdirs, "plan_review")
            rev = next((v for k, v in rdirs if k == "REVISE"), None)
            if any(k == "AGREE" for k, _ in rdirs) and rev is None:
                it.agreed_files, it.test_command = files, command
                self._thread(it, "duet", f"계획 v{it.plan_version} 합의 · 파일 {', '.join(files)} · test_command: {command}")
                return
            if it.rounds >= limit:
                choice = await self.orch.ui.ask_choice(
                    f"[{it.id}] 계획 합의 라운드 한도", f"{it.role} 계획 v{it.plan_version}\n\n설계자: {rev or rfull[-800:]}",
                    [("more", "한 라운드 더"), ("agree", "이 계획으로 진행"), ("wait", "보류")])
                if choice == "agree":
                    it.agreed_files, it.test_command = files, command
                    return
                if choice != "more":
                    raise _Wait("plan_review", "계획 합의 라운드 한도 — 사람 보류")
                limit += 1
            feedback = rev or "설계자가 AGREE 하지 않았습니다. 응답을 읽고 계획을 고치세요."

    async def _implement_loop(self, it: WorkItem, path: Path) -> None:
        note = "계획이 합의되었습니다. 구현하세요."
        if it.resume_at:
            note = "사람이 작업을 재개했습니다. 이어서 진행하세요."
        skip, it.resume_at = it.resume_at, ""
        while True:
            if skip:
                full = "(재개 — 이전 보고는 docs/work 기록 참고)"
                skip = ""
                verdict = await self._verify(it, path, full)
                if verdict is None:
                    merged = await self._merge(it, path)
                    if merged is True:
                        return
                    note = merged
                else:
                    note = "재작업 요청: " + verdict
                continue
            self._touch(it, "implement")
            worker = await self._worker(it, path)
            worker.plan_read_only = False
            prompt = (f"[duet] 병렬 작업 {it.id} · implement 단계\n작업: {it.task}\n{note}\n"
                      f"합의 파일: {', '.join(it.agreed_files)}\ntest_command: {it.test_command}\n"
                      f"다른 작업:\n{self._others(it)}\n"
                      "끝나면 AC별 충족 여부, 바꾼 파일, 테스트 명령과 결과를 보고하고 <!-- duet: REPORT done --> "
                      "(막히면 REPORT blocked <이유>). git 커밋은 하지 마세요.")
            _, full, dirs = await self._consultable(worker, it, "work_implement", prompt, path)
            self._check_ask(dirs, "implement")
            report = next((v for k, v in dirs if k == "REPORT"), "")
            if report.startswith("blocked"):
                raise _Wait("implement", "작업자 보고: " + report)
            if not report.startswith("done"):
                note = "REPORT done 지시문이 없습니다. 작업을 마쳤으면 보고와 함께 REPORT done 을 쓰세요."
                it.reworks += 1
                if it.reworks > MAX_REWORK:
                    raise _Wait("implement", "보고 형식 오류 반복")
                continue
            if self.wt.in_merge(path):
                try:
                    await asyncio.to_thread(self.wt.finish_merge, path)
                except GitError as e:
                    note = f"충돌 해결이 끝나지 않았습니다: {e}"
                    continue
            verdict = await self._verify(it, path, full)
            if verdict is None:
                merged = await self._merge(it, path)
                if merged is True:
                    return
                note = merged  # 병합 과정 문제 → 재작업 안내
            else:
                note = "재작업 요청: " + verdict
            it.reworks += 1
            if it.reworks > MAX_REWORK:
                choice = await self.orch.ui.ask_choice(f"[{it.id}] 재작업 한도", note[-1500:],
                                                       [("more", "계속"), ("wait", "보류")])
                if choice != "more":
                    raise _Wait("implement", "재작업 한도 — 사람 보류")
                it.reworks = 0

    async def _run_test(self, it: WorkItem, path: Path, command: str) -> tuple[int, str]:
        label = f'duet#{it.id}'
        self.orch.bus.emit('turn_start', label, kind='work_test', info=it.id)
        try:
            result = await self._run_test_active(it, path, command)
        except BaseException as e:
            self.orch.bus.emit('turn_end', label, ok=False, error=str(e), interrupted=isinstance(e, asyncio.CancelledError))
            raise
        self.orch.bus.emit('turn_end', label, ok=result[0] == 0, error='' if result[0] == 0 else result[1][:80])
        return result

    async def _run_test_active(self, it: WorkItem, path: Path, command: str) -> tuple[int, str]:
        req = ApprovalRequest(self.cfg.main, "command", "$ " + command, command=command)
        pol = Policy(path, self.cfg.policy, self.cfg.main)
        tier, reason = pol.classify(req, self.cfg.main_role())
        if tier != AUTO:
            d = await self.orch._ask_human(replace(req, role=f'duet#{it.id}'), f"[{it.id}] 합의 테스트 실행: {reason}", None)
            if not d.allow:
                return 126, f"사람이 테스트 실행을 거부: {d.reason}"
        timeout = float(self.cfg.settings.get("work_test_timeout") or 900)
        self.orch.bus.emit("tool", f"duet#{it.id}", name="test", detail="$ " + command)
        proc = await asyncio.create_subprocess_shell(command, cwd=str(path), stdout=asyncio.subprocess.PIPE,
                                                     stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            await self._kill_test_group(proc)
            return 124, f"시간 초과 ({timeout:.0f}초)"
        except asyncio.CancelledError:
            await self._kill_test_group(proc)
            raise
        text = out.decode(errors="replace")
        self.orch.bus.emit("tool_output", f"duet#{it.id}", text=text[-800:], ok=proc.returncode == 0)
        return proc.returncode or 0, text[-4000:]

    @staticmethod
    async def _kill_test_group(proc) -> None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await proc.wait()

    async def _verify(self, it: WorkItem, path: Path, report: str) -> str | None:
        """합의 테스트를 실행하고 설계자 분신이 검토. 재작업 사유(통과면 None)."""
        self._touch(it, "verify")
        code, out = await self._run_test(it, path, it.test_command)
        self._thread(it, "duet · 테스트", f"$ {it.test_command}\nexit {code}\n```\n{out[-2000:]}\n```")
        changed = await asyncio.to_thread(self.wt.changed_files, path, self.base)
        outside = [p for p in changed if p not in it.agreed_files]
        stat = await asyncio.to_thread(self.wt.diff_stat, path, self.base)
        arch = await self._architect(it, path)
        _, full, dirs = await self._turn(
            arch, it, "work_verify",
            f"[duet] 병렬 작업 {it.id} 검증\n작업자 보고:\n{report[-3000:]}\n\n"
            f"변경 요약 (기준 {self.base} 대비):\n{stat}\n계획 밖 변경: {', '.join(outside) or '없음'}\n\n"
            f"duet 이 합의 테스트를 실행했습니다: `{it.test_command}` → exit {code}\n```\n{out[-2500:]}\n```\n"
            f"워크트리 {path.relative_to(self.project).as_posix()} 에서 코드를 직접 읽고 AC 충족·테스트의 타당성을 검토하세요. "
            "ACCEPT 또는 REWORK <사유>.", "설계자")
        self._check_ask(dirs, "verify")
        rework = next((v for k, v in dirs if k == "REWORK"), None)
        if rework is not None:
            return rework or "설계자가 재작업을 요청했습니다."
        if any(k == "ACCEPT" for k, _ in dirs):
            if code != 0:
                return f"합의 테스트가 실패했습니다 (exit {code}). 설계자 ACCEPT 에도 테스트 통과가 필요합니다.\n{out[-1500:]}"
            it.result = full.strip().splitlines()[0][:200] if full.strip() else ""
            return None
        return "설계자가 ACCEPT/REWORK 를 내지 않았습니다. 보고를 보완하세요."

    async def _merge(self, it: WorkItem, path: Path) -> bool | str:
        """True = 병합 완료. 문자열 = 작업자에게 돌려줄 재작업 안내."""
        async with self.merge_lock:
            self._touch(it, "merging")
            await asyncio.to_thread(self.wt.commit_all, path, f"duet work {it.id}: {it.task.splitlines()[0][:60]}")
            conflicts = await asyncio.to_thread(self.wt.merge_base_into, path, self.base)
            if conflicts:
                self._thread(it, "duet", "기준 브랜치 병합 충돌: " + ", ".join(conflicts))
                return ("기준 브랜치의 최신 내용(다른 작업 병합분)과 충돌했습니다. 충돌 파일: " + ", ".join(conflicts)
                        + "\n충돌 표시를 해결해 파일을 고치고(커밋은 하지 말 것) 테스트를 다시 돌린 뒤 REPORT done.")
            integ = (self.cfg.settings.get("integration_test") or "").strip() or it.test_command
            code, out = await self._run_test(it, path, integ)
            self._thread(it, "duet · 통합 테스트", f"$ {integ}\nexit {code}\n```\n{out[-2000:]}\n```")
            if code != 0:
                return f"기준 브랜치와 합친 뒤 통합 테스트가 실패했습니다 (`{integ}`, exit {code}):\n{out[-1500:]}"
            if not self.cfg.settings.get("auto_merge", True):
                stat = await asyncio.to_thread(self.wt.diff_stat, path, self.base)
                c = await self.orch.ui.ask_choice(f"[{it.id}] 기준 브랜치 {self.base} 에 병합할까요?", stat,
                                                  [("merge", "병합"), ("hold", "보류")])
                if c != "merge":
                    raise _Wait("merging", "사람이 병합을 보류함")
            try:
                sha = await asyncio.to_thread(self._snapshot_and_squash, it)
            except GitError as e:
                raise _Wait("merging", f"기준 브랜치 병합 실패: {e}")
            it.merged_commit = sha
            await self._close(it)
            await asyncio.to_thread(self.wt.remove, it.id)
            self._touch(it, "merged")
            self._thread(it, "duet", f"{self.base} 에 병합 완료" + (f" ({sha[:9]})" if sha else " (변경 없음)")
                         + " · 작업 브랜치·워크트리 삭제")
            self.orch.dialogue.append_note(f"[work] {it.id} ({it.role}) → {self.base} 병합"
                                           + (f" {sha[:9]}" if sha else ""))
            return True

    def _snapshot_and_squash(self, it: WorkItem) -> str | None:
        with self.git_lock:
            self.orch.git._snapshot_locked(f"duet: {it.id} 병합 전 스냅샷")
            return self.wt.squash_into_base(it.id, self.base,
                                           f"{it.task.splitlines()[0][:72]}\n\nduet work {it.id} ({it.role})")

    # ---------------- 세션 간 협의 ----------------
    async def _consultable(self, ad, it: WorkItem, kind: str, prompt: str, path: Path):
        """작업자 턴 실행. CONSULT 가 있으면 답을 받아 같은 단계 턴을 이어간다."""
        who = it.role
        tr, full, dirs = await self._turn(ad, it, kind, prompt, who)
        while True:
            consult = next((v for k, v in dirs if k == "CONSULT"), None)
            if consult is None:
                return tr, full, dirs
            it.consults += 1
            if it.consults > MAX_CONSULTS:
                answer = f"협의 한도({MAX_CONSULTS}회)를 넘었습니다. 지금까지의 정보로 진행하거나 REPORT blocked 하세요."
            else:
                target, _, question = consult.partition(" ")
                answer = await self._answer(it, target.strip(), question.strip(), path)
            tr, full, dirs = await self._turn(ad, it, kind, f"[duet] CONSULT 답변\n{answer}\n\n{prompt}", who)

    async def _answer(self, it: WorkItem, target: str, question: str, path: Path) -> str:
        from ..adapters import make_adapter
        if not question:
            return "질문이 비었습니다."
        self.orch.notice(f"[{it.id}] → {target} 협의: {question[:120]}")
        if target in ("architect", self.cfg.main):
            arch = await self._architect(it, path)
            tr, full, _ = await self._turn(arch, it, "work_consult",
                                           f"[duet] 작업 {it.id} 의 {it.role} 가 묻습니다:\n{question}\n"
                                           "설계 결정에 근거해 짧고 구체적으로 답하세요.", "설계자")
            return f"설계자 답변:\n{full}"
        other = self.items.get(target)
        if not other or other.status in DONE and other.status != "merged":
            return f"'{target}' 작업이 없습니다. 가능: architect, " + ", ".join(
                o.id for o in self.items.values() if o.id != it.id and o.status not in DONE)
        src = self.adapters.get(other.id)
        session = (src.session_id if src else None) or other.session_id
        role = replace(self.cfg.roles[other.role], name=f"{other.role}#{other.id}(답변:{it.id})")
        opath = self.wt.path_for(other.id)
        if not opath.exists():
            opath = self.project
        ad = make_adapter(role, opath, self.orch.bus, self._read_only_answer, session,
                          WORK_SYSTEM.format(role=other.role, id=other.id, brief=role.brief,
                                             branch=f"duet/work/{other.id}", base=self.base),
                          fake=self.orch.fake, fork_session=bool(session))
        try:
            await self._start_activity(ad)
            ad.plan_read_only = True
            ad.turn_kind = "work_consult"
            self.orch.bus.emit('turn_start', ad.label, kind='work_consult', info=other.id)
            tr = await ad.run_turn(f"[duet] 병렬 작업 {it.id} ({it.role}) 가 당신의 작업 {other.id} 에 대해 묻습니다 "
                                   f"(읽기 전용 복제본이 답합니다. 파일을 고치지 마세요):\n{question}")
            self.orch.bus.emit('turn_end', ad.label, ok=tr.ok, error=tr.error, interrupted=tr.interrupted)
        except BaseException as e:
            self.orch.bus.emit('turn_end', ad.label, ok=False, error=str(e), interrupted=isinstance(e, asyncio.CancelledError))
            raise
        finally:
            await ad.close()
        text = tr.full_text or tr.text or tr.error or "(응답 없음)"
        self._thread(other, f"{it.id} 의 질문에 답변", f"Q: {question}\n\nA: {text}")
        return f"{other.id} ({other.role}) 답변:\n{text}"

    async def _read_only_answer(self, req: ApprovalRequest) -> Decision:
        return read_only_decision(req, "협의 답변 세션")

    @staticmethod
    def _check_ask(dirs: list[tuple[str, str]], phase: str) -> None:
        q = next((v for k, v in dirs if k == "ASK_HUMAN"), None)
        if q is not None:
            raise _Wait(phase, "사람에게 질문: " + (q or "(내용 없음)"))

    # ---------------- 종료 ----------------
    async def close(self) -> None:
        self.closing = True
        for t in list(self.tasks.values()):
            t.cancel()
        for t in list(self.tasks.values()):
            try:
                await t
            except BaseException:
                pass
        self.tasks.clear()
        for it in self.items.values():
            if it.status in ACTIVE and it.status != "queued":
                self._save_sessions(it)
                it.wait_reason = f"duet 종료로 {it.status} 단계에서 멈춤 — RESUME_WORK {it.id} 로 재개"
                it.status = "waiting:" + it.status
        for key in list(self.adapters):
            ad = self.adapters.pop(key)
            try:
                await ad.close()
            except Exception:
                pass
        self.save()


class _Wait(Exception):
    def __init__(self, phase: str, reason: str):
        super().__init__(reason)
        self.phase, self.reason = phase, reason
