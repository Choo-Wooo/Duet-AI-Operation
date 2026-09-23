"""오케스트레이터 코어: 턴 진행, 위임, 권한 라우팅, 턴 한도·정체 감지, 스냅샷."""
from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import datetime
from typing import Any

from ..adapters import AgentAdapter, TurnResult, make_adapter
from .config import SUPPORTED_CLIS, Config, Role
from .dialogue import Dialogue, Turn, extract_directives
from .agreement import (changed_files, fingerprint, new_task, parse_plan, read_plan, record_plan,
                        task_status, text_hash)
from .events import Event, EventBus
from .gitops import Git
from .policy import ARCHITECT, AUTO, DENY, HUMAN, ApprovalRequest, Decision, Policy
from .prompts import opinion_prompt, review_prompt, system_append, turn_prompt
from .ui import HumanUI
from .saves import delete_save, git_head, list_saves, read_save, write_save

Next = tuple[str, str, str]  # (role, kind, info)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


class Orchestrator:
    def __init__(self, cfg: Config, bus: EventBus, ui: HumanUI, fake: bool = False):
        self.cfg = cfg
        self.bus = bus
        self.ui = ui
        self.fake = fake
        self.project = cfg.project
        self.dialogue = Dialogue(cfg.project)
        self.git = Git(cfg.project, bool(cfg.settings.get("git_snapshots", True)))
        self.policy = Policy(cfg.project, cfg.policy, cfg.main)
        self.adapters: dict[str, AgentAdapter] = {}
        self.reviewer: AgentAdapter | None = None
        self.reviewer_lock = asyncio.Lock()
        self.inbox: asyncio.Queue[tuple[str | None, str]] = asyncio.Queue()
        self.running_role: str | None = None
        self.running = False
        self.loading = False
        self._turn_active = False
        self._input_pending = False
        self._session_ready = asyncio.Event()
        self._session_ready.set()
        self._stale_adapters: set[str] = set()
        self._stale_reviewer = False
        self.current_task: dict[str, str] = {}
        self.not_paused = asyncio.Event()
        self.not_paused.set()
        self.stop_requested = False
        self.run_turns = 0
        self.extra_turns = 0
        self.pending_approvals = 0
        self.cost_usd = 0.0
        self.tokens = 0
        self.started = time.time()
        self.hashes: list[str | None] = []
        self.task_history: list[str] = []
        self.notes: list[str] = []
        self.budget_usd: float | None = cfg.runtime.get("budget_usd")
        self.max_hours: float | None = cfg.runtime.get("max_hours")
        if cfg.state.task:
            cfg.state.task["waiting"] = True
            cfg.state.task["wait_reason"] = cfg.state.task["wait_reason"] or "재시작 후 메인의 재개 판단 대기"
        self.policy.task = cfg.state.task
        bus.subscribe(self._on_event)

    # ================= 상태 =================
    def _on_event(self, ev: Event) -> None:
        if ev.kind == "usage":
            self.cost_usd += float(ev.data.get("cost_usd") or 0)
            self.tokens += int(ev.data.get("tokens") or 0)
            self.emit_status()

    def status(self) -> dict[str, Any]:
        mt = self.cfg.max_turns
        return {
            "running": self.running_role,
            "busy": self.running,
            "run_turns": self.run_turns,
            "max_turns": None if mt is None else mt + self.extra_turns,
            "mode": self.cfg.state.mode,
            "auto": self.cfg.state.auto,
            "paused": not self.not_paused.is_set(),
            "pending_approvals": self.pending_approvals,
            "cost_usd": self.cost_usd,
            "tokens": self.tokens,
            "elapsed": time.time() - self.started,
            "roles": list(self.cfg.roles.keys()),
            "main": self.cfg.main,
            "task": ({k: self.cfg.state.task[k] for k in ("id", "phase", "plan_path", "submitted_version",
                       "agreed_version", "rounds", "waiting")} if self.cfg.state.task else None),
            "task_summary": task_status(self.cfg.state.task),
        }

    def emit_status(self) -> None:
        self.bus.emit("status", None, **self.status())

    def notice(self, text: str, level: str = "info", role: str | None = None) -> None:
        self.bus.emit("notice", role, text=text, level=level)

    # ================= 어댑터 =================
    async def adapter(self, name: str) -> AgentAdapter:
        if name in self._stale_adapters:
            old = self.adapters.pop(name, None)
            if old:
                await old.close()
            self._stale_adapters.discard(name)
        if name in self.adapters:
            return self.adapters[name]
        role = self.cfg.roles[name]
        ad = make_adapter(role, self.project, self.bus, self.handle_approval, self.cfg.state.sessions.get(name),
                          system_append(self.cfg, role), fake=self.fake,
                          fork_session=name in self.cfg.state.fork_on_resume)
        self.notice(f"{name} 세션 시작 ({role.cli}/{role.model or '기본'})", role=name)
        try:
            await ad.start()
        except Exception:
            await ad.close()
            raise
        self.adapters[name] = ad
        self._remember_session(name, ad)
        return ad

    def _remember_session(self, name: str, ad: AgentAdapter, reviewer: bool = False) -> None:
        sessions = self.cfg.state.reviewer_sessions if reviewer else self.cfg.state.sessions
        forks = self.cfg.state.reviewer_fork_on_resume if reviewer else self.cfg.state.fork_on_resume
        if ad.session_id:
            sessions[name] = ad.session_id
        else:
            sessions.pop(name, None)
        # Claude는 첫 응답 때 새 id를 준다. 그 전 재시작에도 분기 예약을 유지한다.
        if not ad.fork_session and name in forks:
            forks.remove(name)
        self.cfg.save_state()

    async def get_reviewer(self) -> AgentAdapter:
        if self._stale_reviewer:
            if self.reviewer:
                await self.reviewer.close()
            self.reviewer = None
            self._stale_reviewer = False
        if self.reviewer is None:
            role = self.cfg.main_role()
            ad = make_adapter(role, self.project, self.bus, None, self.cfg.state.reviewer_sessions.get(role.name),
                              "", reviewer=True, fake=self.fake,
                              fork_session=role.name in self.cfg.state.reviewer_fork_on_resume)
            try:
                await ad.start()
            except Exception:
                await ad.close()
                raise
            self.reviewer = ad
            self._remember_session(role.name, ad, reviewer=True)
        return self.reviewer

    async def close(self) -> None:
        for ad in list(self.adapters.values()) + ([self.reviewer] if self.reviewer else []):
            try:
                await ad.close()
            except Exception:
                pass
        self.adapters.clear()
        self.reviewer = None

    # ================= 권한 =================
    async def handle_approval(self, req: ApprovalRequest) -> Decision:
        role = self.cfg.roles.get(req.role) or Role(req.role, "claude")
        self.policy.task = self.cfg.state.task
        tier, reason = self.policy.classify(req, role)
        opinion = None
        if tier == AUTO:
            d = Decision(True, reason, by="auto")
        elif tier == DENY:
            d = Decision(False, reason, by="policy")
        elif tier == ARCHITECT:
            d, opinion = await self._ask_architect(req, reason)
            if d is None:
                d = await self._ask_human(req, reason + " — 설계자가 사람에게 넘김", opinion)
        else:
            if self.cfg.policy.get("ask_architect_opinion_for_human", True) and req.role != self.cfg.main:
                _, opinion = await self._ask_architect(req, reason, opinion_only=True)
            d = await self._ask_human(req, reason, opinion)
        if self.policy.verification_command(req):
            d.scope = "once"
        if d.allow and d.scope == "session":
            self.policy.remember(req)
        self.bus.emit("approval", req.role, summary=req.summary, tier=tier, allow=d.allow, by=d.by,
                      reason=d.reason)
        if tier != AUTO:
            verdict = "허용" if d.allow else "거부"
            one_line = " ".join(req.summary.split())
            one_line = one_line if len(one_line) <= 120 else one_line[:119] + "…"
            self.notes.append(f"[approval] {req.role}: `{one_line}` → {verdict} ({d.by}: {' '.join(d.reason.split())})")
        return d

    async def _ask_architect(self, req: ApprovalRequest, reason: str,
                             opinion_only: bool = False) -> tuple[Decision | None, str | None]:
        timeout = float(self.cfg.policy.get("review_timeout_sec", 120))
        task = self.current_task.get(req.role, "")
        if self.cfg.state.task:
            task = f"{self.cfg.state.task['instruction']}\n현재 합의 단계: {self.cfg.state.task['phase']}"
        prompt = (opinion_prompt if opinion_only else review_prompt)(req.summary, req.role, reason, task)
        try:
            rev = await self.get_reviewer()
            async with self.reviewer_lock:
                self.notice(f"설계자 심사 중: {req.summary[:100]}", role=self.cfg.main)
                try:
                    tr = await asyncio.wait_for(rev.run_turn(prompt), timeout)
                    self._remember_session(self.cfg.main, rev, reviewer=True)
                except asyncio.TimeoutError:
                    await rev.interrupt()
                    return None, "(설계자 심사 시간 초과)"
        except Exception as e:
            return None, f"(설계자 심사 실패: {e})"
        data = _parse_json(tr.text)
        if not data:
            return None, (tr.text or "")[:300] or "(설계자 응답을 해석하지 못함)"
        dec = str(data.get("decision", "")).lower()
        why = str(data.get("reason", ""))
        opinion = f"{dec}: {why}"
        if opinion_only or dec == "escalate":
            return None, opinion
        if dec == "allow":
            return Decision(True, why, scope="session" if data.get("scope") == "session" else "once",
                            by="architect"), opinion
        if dec == "deny":
            return Decision(False, why, by="architect"), opinion
        return None, opinion

    async def _ask_human(self, req: ApprovalRequest, reason: str, opinion: str | None) -> Decision:
        self.pending_approvals += 1
        self.emit_status()
        try:
            d = await self.ui.ask_approval(req, reason, opinion)
            d.by = d.by or "human"
            return d
        finally:
            self.pending_approvals -= 1
            self.emit_status()

    # ================= 사람 입력 =================
    def submit(self, text: str, to: str | None = None) -> None:
        self.inbox.put_nowait((to, text))
        if self.running:
            self.notice("메시지를 받았습니다. 현재 턴이 끝나면 전달합니다.")

    def _append_human(self, to: str | None, text: str) -> int:
        n = self._next_number()
        body = (f"@{to} " if to else "") + text
        self.dialogue.append_turn("human", n, body)
        self.cfg.state.last_n = n
        self.cfg.save_state()
        self.bus.emit("human", "human", text=text, n=n, to=to)
        return n

    def _drain_inbox(self, nxt: Next | None) -> Next | None:
        while not self.inbox.empty():
            to, text = self.inbox.get_nowait()
            n = self._append_human(to, text)
            target = to if to in self.cfg.roles else self.cfg.main
            if self.cfg.state.task:
                if not self.cfg.state.task["waiting"]:
                    self._wait_task("사람 메시지에 대한 메인의 판단 대기")
                target = self.cfg.main
            nxt = (target, "human", str(n))
        return nxt

    async def serve(self) -> None:
        """사람 메시지를 기다렸다가 요청 단위로 진행한다 (TUI 워커로 실행)."""
        self.emit_status()
        while True:
            to, text = await self.inbox.get()
            self._input_pending = True
            await self._session_ready.wait()
            self._input_pending = False
            n = self._append_human(to, text)
            target = to if to in self.cfg.roles else self.cfg.main
            if self.cfg.state.task:
                if not self.cfg.state.task["waiting"]:
                    self._wait_task("사람 메시지에 대한 메인의 판단 대기")
                target = self.cfg.main
            try:
                await self.run((target, "human", str(n)))
            except Exception as e:  # 예기치 못한 오류도 루프는 유지
                self.bus.emit("error", None, text=f"진행 중 오류: {type(e).__name__}: {e}")
            finally:
                self.running = False
                self.running_role = None
                self.emit_status()
                self.bus.emit("idle", None, text="사람 차례입니다.")

    # ================= 진행 루프 =================
    async def run(self, nxt: Next | None) -> None:
        try:
            await self._run_loop(nxt)
        finally:
            if self.cfg.state.task and not self.cfg.state.task["waiting"]:
                self._wait_task("진행 종료/중단 후 메인의 판단 대기")

    async def _run_loop(self, nxt: Next | None) -> None:
        self.running = True
        self.stop_requested = False
        self.run_turns = 0
        self.extra_turns = 0
        self.hashes = [self.git.code_hash()]
        self.task_history = []
        final = False
        while nxt:
            await self.not_paused.wait()
            nxt = self._drain_inbox(nxt)
            if self.stop_requested or not nxt:
                break

            # 턴 한도
            limit = self.cfg.max_turns
            if not final and limit is not None and self.run_turns >= limit + self.extra_turns:
                c = await self.ui.ask_choice(
                    "턴 한도에 도달했습니다",
                    f"이번 요청에서 {self.run_turns}턴이 진행되었습니다 (모드 {self.cfg.state.mode}).",
                    [("+10", "10턴 더"), ("+30", "30턴 더"), ("inf", "무제한으로 전환"),
                     ("wrap", "설계자가 정리하고 끝내기"), ("stop", "바로 멈추기")])
                if c.startswith("+"):
                    self.extra_turns += int(c[1:])
                elif c == "inf":
                    self.set_max_turns(None)
                elif c == "wrap":
                    final = True
                    nxt = (self.cfg.main, "system",
                           "턴 한도에 도달해 사람이 정리를 요청했습니다. 지금까지의 진행 상황과 남은 일을 정리하고 STATUS done 으로 마치세요.")
                else:
                    break

            # 예산/시간 (선택)
            if not final and not await self._check_budget():
                break

            # 체크포인트
            every = int(self.cfg.settings.get("checkpoint_every") or 0)
            if every and nxt[0] == self.cfg.main and self.cfg.state.turns_since_checkpoint >= every:
                res = await self._turn(self.cfg.main, "checkpoint", "")
                if res and res[0].ok:
                    dest = self.dialogue.archive_before(res[1].n)
                    self.cfg.state.turns_since_checkpoint = 0
                    self.cfg.save_state()
                    if dest:
                        self.notice(f"체크포인트: 이전 대화를 {dest.relative_to(self.project)} 로 옮겼습니다.")
                    self.git.snapshot(f"duet #{res[1].n} [checkpoint] archive")

            role, kind, info = nxt
            if kind == "delegate" and not self.cfg.state.auto:
                c = await self.ui.ask_choice("위임 확인", f"{self.cfg.main} → {role}\n\n{info}",
                                             [("yes", "진행"), ("no", "멈추기")])
                if c != "yes":
                    break

            res = await self._turn(role, kind, info)
            if res is None:
                break
            tr, turn = res
            self.run_turns += 1
            self.emit_status()
            if final:
                break

            negotiating = self.cfg.state.task and self.cfg.state.task["phase"] in ("plan", "plan_review")
            problem = None if negotiating else self._check_flow(role)
            if problem and not self.stop_requested:
                c = await self.ui.ask_choice("흐름이 막힌 것 같습니다", problem,
                                             [("continue", "계속 진행"), ("summarize", "설계자에게 정리 요청"),
                                              ("stop", "멈추기")])
                self.hashes = self.hashes[-1:]
                self.task_history = []
                if c == "stop":
                    break
                if c == "summarize":
                    nxt = (self.cfg.main, "system",
                           f"오케스트레이터가 정체를 감지했습니다: {problem}\n막힌 지점과 원인, 다음에 시도할 방법을 정리하고 사람에게 선택지를 제시하세요 (ASK_HUMAN).")
                    continue
            nxt = await self._decide(role, kind, tr, turn)

    async def _check_budget(self) -> bool:
        over = []
        if self.budget_usd is not None and self.cost_usd >= self.budget_usd:
            over.append(f"비용 ${self.cost_usd:.2f} ≥ 예산 ${self.budget_usd:.2f}")
        if self.max_hours is not None and (time.time() - self.started) / 3600 >= self.max_hours:
            over.append(f"경과 시간이 {self.max_hours}시간을 넘었습니다")
        if not over:
            return True
        c = await self.ui.ask_choice("예산 한도", "\n".join(over), [("continue", "한도 해제하고 계속"), ("stop", "멈추기")])
        if c == "continue":
            self.budget_usd = None
            self.max_hours = None
            return True
        return False

    def _next_number(self) -> int:
        return max(self.cfg.state.last_n, self.dialogue.max_number()) + 1

    async def _turn(self, role_name: str, kind: str, info: str) -> tuple[TurnResult, Turn] | None:
        self._turn_active = True
        try:
            return await self._run_turn(role_name, kind, info)
        finally:
            self._turn_active = False

    async def _run_turn(self, role_name: str, kind: str, info: str) -> tuple[TurnResult, Turn] | None:
        try:
            ad = await self.adapter(role_name)
        except Exception as e:
            self.bus.emit("error", role_name, text=f"{role_name} 세션을 시작하지 못했습니다: {e}")
            return None
        role = self.cfg.roles[role_name]
        task = self.cfg.state.task
        ad.turn_kind = kind
        ad.agreement_phase = task["phase"] if task else None
        ad.plan_read_only = bool(task and role_name != self.cfg.main
                                 and (task["phase"] in ("plan", "plan_review") or task["waiting"]))
        ad.verification_command = task["test_command"] if task and task["phase"] == "verify" and role_name == self.cfg.main else None
        self.policy.task = task
        n = self._next_number()
        since = self.cfg.state.seen.get(role_name, 0) + 1
        prompt = turn_prompt(self.cfg, role, n, since, kind, info)
        if task:
            self._warn_plan_hash()
            if task["phase"] == "verify":
                current = self._fingerprint()
                changes = changed_files(task["agree_fingerprint"], current)
                outside = [p for p in changes if p not in task["agreed_files"]]
                prompt += "\nAGREE 시점 이후 변경: " + (", ".join(changes) or "없음")
                prompt += "\n계획 외 변경 경고: " + (", ".join(outside) or "없음")
        if kind == "delegate":
            self.current_task[role_name] = info
        self.running_role = role_name
        self.bus.emit("turn_start", role_name, n=n, kind=kind, info=info)
        self.emit_status()

        tr = await ad.run_turn(prompt)

        self.running_role = None
        self._remember_session(role_name, ad)
        # plan 작업자는 파일을 쓰지 않는다. 전문은 계획서에, 요약만 대화에 기록한다.
        full = tr.full_text if tr.full_text is not None else tr.text
        directives = extract_directives(full)
        deviation = any(k == "REPORT" and v.split(maxsplit=1)[0:1] == ["deviation"] for k, v in directives)
        plan_submission = bool(task and task["phase"] == "plan" and ("PLAN", "ready") in directives)
        if task and role_name == task["role"] and tr.ok and not tr.interrupted and (plan_submission or deviation):
            try:
                if deviation:
                    parse_plan(full)  # 전문 없는 deviation은 다음 plan 턴에서 작성시킨다.
                version = record_plan(self.project, task, full, deviation=deviation)
                controls = "\n".join(f"<!-- duet: {k} {v} -->" for k, v in directives)
                tr.text = f"계획 v{version} 제출: {task['plan_path']}\n\n{controls}"
                self.cfg.save_state()
            except (ValueError, OSError) as e:
                if plan_submission:
                    tr.ok, tr.error = False, f"계획 제출 실패: {e}"
                self.notice(f"계획 전문을 기록하지 못했습니다: {e}", "warn")
        if task:
            current = self._fingerprint()
            if task["phase"] in ("plan", "plan_review"):
                task["plan_changes"] = changed_files(task["delegate_fingerprint"], current)
                if task["plan_changes"]:
                    self.notice("plan 단계 파일 변경 경고: " + ", ".join(task["plan_changes"]), "warn")
            else:
                outside = [p for p in changed_files(task["agree_fingerprint"], current)
                           if p not in task["agreed_files"]]
                if outside:
                    self.notice("계획 외 변경 경고: " + ", ".join(outside), "warn")
        turn = self.dialogue.find(role_name, n)
        if turn is None:  # 에이전트가 문서에 쓰지 않았으면 대신 기록
            body = tr.text.strip() or ("(응답 없음)" if tr.ok else f"(오류) {tr.error}")
            if tr.interrupted:
                body += "\n\n(사람이 이 턴을 중단했습니다)"
            self.dialogue.append_turn(role_name, n, body, note="duet 대리 기록")
            turn = self.dialogue.find(role_name, n)
        for line in self.notes:
            self.dialogue.append_note(line)
        self.notes.clear()
        assert turn is not None
        last = max(self.dialogue.max_number(), turn.n)
        self.cfg.state.last_n = last
        self.cfg.state.seen[role_name] = last
        self.cfg.state.turns_since_checkpoint += 1
        self.cfg.save_state()
        first = re.sub(r"[*#`>_]+", "", turn.summary(1, 60) or kind).replace("\n", " ").strip() or kind
        self.git.snapshot(f"duet #{turn.n} [{role_name}] {first}")
        self.bus.emit("turn_end", role_name, n=turn.n, summary=turn.summary(), directives=turn.directives,
                      ok=tr.ok, error=tr.error, interrupted=tr.interrupted)
        if not tr.ok:
            self.bus.emit("error", role_name, text=f"{role_name} 턴 오류: {tr.error}")
        return tr, turn

    def _check_flow(self, role: str) -> str | None:
        h = self.git.code_hash()
        self.hashes.append(h)
        hs = self.hashes
        if h is not None and len(hs) >= 3 and hs[-1] == hs[-3] and hs[-2] != hs[-1]:
            return "직전 두 턴의 변경이 서로를 되돌렸습니다 (핑퐁)."
        k = self.cfg.mode.stall_turns
        if h is not None and k and len(hs) >= k + 1 and all(x == h for x in hs[-(k + 1):]):
            return f"최근 {k}턴 동안 코드·문서 변경이 없습니다."
        if self.task_history:
            last = self.task_history[-1]
            if self.task_history.count(last) >= 3:
                return "같은 작업이 3번 반복해서 위임되었습니다."
        return None

    async def _decide(self, role: str, kind: str, tr: TurnResult, turn: Turn) -> Next | None:
        if not tr.ok or tr.interrupted or self.stop_requested:
            self._wait_task(tr.error or "턴 중단")
            return None
        if self.cfg.state.task:
            return await self._decide_task(role, kind, turn)
        flow_commands = {"PLAN", "AGREE", "REVISE", "ACCEPT", "REWORK", "RESUME", "CANCEL"}
        if any(k in flow_commands for k, _ in turn.directives):
            return (role, "system", "활성 합의 task가 없습니다. 메인의 DELEGATE로 작업을 시작하세요.")
        for name, arg in turn.directives:
            if name == "PROPOSE_ROLE":
                await self._propose_role(arg)
        q = turn.directive("ASK_HUMAN")
        if q is not None:
            self.bus.emit("ask", role, text=q or "(질문 내용 없음)")
            return None
        main = self.cfg.main
        if role == main:
            d = turn.directive("DELEGATE")
            if d is None:
                return None
            target = d.split()[0] if d.split() else ""
            if target not in self.cfg.roles or target == main:
                others = ", ".join(r for r in self.cfg.roles if r != main) or "(없음)"
                return (main, "system", f"'{target}' 역할은 위임할 수 없습니다. 가능한 역할: {others}. 다시 위임하거나 STATUS done 으로 마치세요.")
            task = turn.directive("TASK") or f"DIALOGUE.md #{turn.n} 의 지시를 따르세요."
            self.task_history.append(_norm(task))
            if self.cfg.mode.agreement:
                if not self.cfg.state.auto:
                    answer = await self.ui.ask_choice("위임 확인", f"{self.cfg.main} → {target}\n\n{task}",
                                                      [("yes", "계획부터 진행"), ("no", "취소")])
                    if answer != "yes":
                        return None
                self.cfg.state.task = new_task(target, task, git_head(self.project), self._fingerprint())
                self.current_task[target] = task
                self._phase("plan", previous=None)
                return (target, "plan", task)
            return (target, "delegate", task)
        if kind == "human":  # 사람이 /to 로 직접 말을 건 경우 → 답하면 사람에게
            return None
        return (main, "report", str(turn.n))

    # ================= 합의 기반 위임 =================
    def _fingerprint(self) -> dict:
        values, errors = fingerprint(self.project, self.cfg.settings["fingerprint_exclude"])
        for error in errors:
            self.notice("파일 지문 검증 불완전: " + error, "warn")
        return values

    def _phase(self, phase: str, previous: str | None = "current") -> None:
        task = self.cfg.state.task
        assert task is not None
        old = task["phase"] if previous == "current" else previous
        task["phase"], task["waiting"], task["wait_reason"] = phase, False, ""
        self.policy.task = task
        self.cfg.save_state()
        if old != phase:
            self.bus.emit("phase", None, **{"task_id": task["id"], "from": old, "to": phase})
        self.emit_status()

    def _wait_task(self, reason: str) -> None:
        task = self.cfg.state.task
        if task:
            task["waiting"], task["wait_reason"] = True, reason
            self.cfg.save_state()
            self.emit_status()

    def _finish_task(self, reason: str) -> None:
        task = self.cfg.state.task
        if task:
            self.cfg.state.task = None
            self.policy.task = None
            self.current_task.pop(task["role"], None)
            self.cfg.save_state()
            self.bus.emit("phase", None, **{"task_id": task["id"], "from": task["phase"], "to": reason})
            self.emit_status()

    def _warn_plan_hash(self) -> None:
        task = self.cfg.state.task
        if task and task["agreed_version"]:
            try:
                text = read_plan(self.project, task, task["agreed_version"])
                if text_hash(text) != task["agreed_text_sha256"]:
                    raise ValueError("해시 불일치")
            except (ValueError, OSError) as e:
                self.notice(f"합의 계획 문서 경고: {e}. 저장된 agreed_files/test_command를 유지합니다.", "warn")

    async def _plan_review(self, turn: Turn) -> Next | None:
        task = self.cfg.state.task
        assert task
        task["rounds"] += 1
        self._phase("plan_review")
        limit = self.cfg.settings["plan_rounds"] + task["rounds_extra"]
        if task["rounds"] > limit:
            prior = self.dialogue.get(task["review_n"])
            proposal = read_plan(self.project, task, task["submitted_version"])
            body = (f"구현자 #{turn.n}, {task['plan_path']} v{task['submitted_version']}:\n{proposal}\n\n"
                    f"설계자 #{task['review_n']}:\n{prior.body if prior else '(첫 검토 전)'}")
            choice = await self.ui.ask_choice("계획 합의 라운드 한도", body,
                                              [("more", "한 라운드 더"), ("wait", "메인과 논의하고 대기"),
                                               ("cancel", "작업 취소")])
            if choice == "cancel":
                self._finish_task("cancelled")
                return None
            if choice != "more":
                self._wait_task("계획 합의 라운드 한도: 사람과 메인의 논의 대기")
                return None
            task["rounds_extra"] += 1
            self.cfg.save_state()
        return (self.cfg.main, "plan_review", f"계획 제출 턴 #{turn.n}")

    async def _decide_task(self, role: str, kind: str, turn: Turn) -> Next | None:
        task = self.cfg.state.task
        assert task
        main, worker, phase = self.cfg.main, task["role"], task["phase"]
        actions = [(k, v) for k, v in turn.directives if k not in ("TASK", "STATUS", "PROPOSE_ROLE")]
        def invalid(message=""):
            options = {"plan": "작업자 PLAN ready, 메인 CANCEL/대기 시 RESUME",
                       "plan_review": "메인 AGREE vN/REVISE/CANCEL",
                       "implement": "작업자 REPORT done/deviation/blocked, 메인 CANCEL/대기 시 RESUME",
                       "verify": "메인 ACCEPT/REWORK/CANCEL"}
            return (role, "system", f"현재 {phase}: {options[phase]}. {message}")
        # ACCEPT와 같은 턴의 다음 DELEGATE는 완료 후 기존 진입 경로로 처리한다.
        next_delegate = [(k, v) for k, v in actions if k == "DELEGATE"]
        if role == main and phase == "verify" and ("ACCEPT", "") in actions:
            actions = [(k, v) for k, v in actions if k != "DELEGATE"]
        elif next_delegate:
            return invalid("활성 task를 새 DELEGATE로 덮어쓸 수 없습니다. CANCEL로 먼저 종료하세요.")
        if len(actions) != 1:
            return invalid("단계에 맞는 제어 지시문 하나를 제출하세요.")
        action, arg = actions[0]
        if action == "ASK_HUMAN":
            self._wait_task(arg or "사람 응답 대기")
            self.bus.emit("ask", role, text=arg or "사람 응답 대기")
            return None
        if action == "CANCEL" and role == main and arg:
            self.notice("합의 작업 취소: " + arg)
            self._finish_task("cancelled")
            return None
        if action == "RESUME" and role == main and not arg:
            if not task["waiting"] or phase not in ("plan", "implement"):
                return invalid("RESUME는 대기 중 plan/implement에서만 가능합니다.")
            if worker not in self.cfg.roles:
                return invalid(f"{worker} 역할이 없습니다. 역할을 복구하거나 CANCEL하세요.")
            reason = task["wait_reason"]
            self._phase(phase)
            return (worker, phase, f"메인 #{turn.n} RESUME. 대기 사유: {reason}")
        if role == worker and action == "REPORT" and arg.split(maxsplit=1)[0:1] == ["blocked"]:
            self._wait_task(f"작업자 #{turn.n}: {arg}")
            self.bus.emit("ask", worker, text=arg)
            return None
        if phase == "plan" and role == worker and action == "PLAN" and arg == "ready":
            if not task["submitted_version"]:
                return invalid("계획 전문을 제출하세요.")
            return await self._plan_review(turn)
        if phase == "plan_review" and role == main:
            task["review_n"] = turn.n
            if action == "REVISE" and arg:
                self._phase("plan")
                return (worker, "plan", f"메인 #{turn.n} 수정 요청/반론 가능: {arg}")
            if action == "AGREE":
                version = task["submitted_version"]
                if not version or arg != f"v{version}":
                    return invalid(f"현재 제출 버전은 v{version}입니다.")
                try:
                    text = read_plan(self.project, task, version)
                    files, command = parse_plan(text)
                except (OSError, ValueError) as e:
                    self.notice(f"AGREE 거부: {e}", "warn")
                    return invalid(str(e))
                if worker not in self.cfg.roles:
                    return invalid("작업자 역할이 없습니다. 복구하거나 CANCEL하세요.")
                if self.cfg.settings["plan_approval"] == "human" and task["human_approved_version"] != version:
                    choice = await self.ui.ask_choice("계획 최종 승인", f"{task['plan_path']} v{version}\n{text}",
                                                      [("yes", "승인"), ("no", "거절하고 재작성")])
                    if choice != "yes":
                        self._phase("plan")
                        return (worker, "plan", "사람이 계획 최종 승인을 거절했습니다. 대화 맥락을 확인해 수정하세요.")
                    task["human_approved_version"] = version
                task.update(agreed_version=version, agreed_text_sha256=text_hash(text), agreed_files=files,
                            test_command=command, agree_fingerprint=self._fingerprint())
                self._phase("implement")
                return (worker, "implement", f"메인 #{turn.n}이 v{version}에 합의했습니다.")
        if phase == "implement" and role == worker and action == "REPORT":
            report = arg.split(maxsplit=1)[0:1]
            if report == ["done"]:
                self._phase("verify")
                return (main, "verify", f"구현 완료 보고 #{turn.n}")
            if report == ["deviation"]:
                if task["submitted_version"] <= (task["agreed_version"] or 0):
                    self._phase("plan")
                    return (worker, "plan", f"변경 계획 전문을 응답으로 제출하세요. 사유: {arg}")
                return await self._plan_review(turn)
        if phase == "verify" and role == main:
            if action == "REWORK" and arg:
                self._phase("implement")
                return (worker, "implement", f"메인 #{turn.n} 재작업 요청: {arg}. 범위 변경은 deviation으로 보고하세요.")
            if action == "ACCEPT" and not arg:
                self._finish_task("accepted")
                if next_delegate:
                    body = "\n".join(f"<!-- duet: {k} {v} -->" for k, v in turn.directives if k != "ACCEPT")
                    return await self._decide(role, kind, TurnResult(""),
                                              Turn(role, turn.n, turn.rest, turn.start, turn.end, body))
                return None
        return invalid()

    async def _propose_role(self, arg: str) -> None:
        parts = arg.split(None, 3)
        if len(parts) < 2 or parts[1] not in SUPPORTED_CLIS:
            self.notice(f"역할 제안 형식을 이해하지 못했습니다: {arg}", "warn")
            return
        name, cli = parts[0], parts[1]
        model = parts[2] if len(parts) > 2 else None
        brief = parts[3] if len(parts) > 3 else ""
        c = await self.ui.ask_choice(f"새 역할 제안: {name}", f"CLI: {cli}\n모델: {model}\n설명: {brief}",
                                     [("add", "추가"), ("skip", "무시")])
        if c == "add":
            self.add_role(Role(name, cli, model, brief, "workspace_write"))

    # ================= 사람 명령 =================
    def save(self, name: str | None = None, *, note: str = "", force: bool = False) -> dict:
        if (self.loading or self._turn_active or self.running_role or self.pending_approvals
                or self.reviewer_lock.locked()
                or any(ad.busy for ad in self.adapters.values())
                or (self.reviewer and self.reviewer.busy)
                or (self.running and self.not_paused.is_set())):
            raise ValueError("턴 진행 중에는 저장할 수 없습니다. /pause 후 현재 턴이 끝나면 다시 시도하세요.")
        if self._input_pending or not self.inbox.empty():
            raise ValueError("아직 전달하지 않은 메시지가 있습니다. 메시지 처리가 끝난 뒤 저장하세요.")
        name = datetime.now().strftime("%Y%m%d-%H%M%S") if name is None else name
        meta = write_save(self.cfg, name, note=note, force=force)
        # 저장된 원본 세션이 다음 턴에 자라지 않도록, 현재 가지도 첫 재개 때 분기한다.
        self.cfg.state.fork_on_resume = list(self.cfg.state.sessions)
        self.cfg.state.reviewer_fork_on_resume = list(self.cfg.state.reviewer_sessions)
        self._stale_adapters.update(self.adapters)
        self._stale_reviewer = self.reviewer is not None
        self.cfg.save_state()
        self.bus.emit("save", None, name=name, last_n=meta["last_n"])
        return meta

    def list_saves(self) -> list[dict]:
        return list_saves(self.cfg)

    def delete_save(self, name: str) -> None:
        if self.loading:
            raise ValueError("세션을 불러오는 중입니다.")
        delete_save(self.cfg, name)

    async def load_save(self, name: str) -> str:
        if (self.loading or self.running or self._turn_active or self.running_role
                or self.pending_approvals or self.reviewer_lock.locked()
                or self._input_pending or not self.inbox.empty()
                or any(ad.busy for ad in self.adapters.values())
                or (self.reviewer and self.reviewer.busy)):
            raise ValueError("진행 중에는 불러올 수 없습니다. /stop 후 진행이 끝나면 다시 시도하세요.")
        meta, state, dialogue, saved_roles = read_save(self.cfg, name)
        warnings = []
        for role in set(state.sessions) | set(state.reviewer_sessions) | set(state.seen):
            current = self.cfg.roles.get(role)
            saved = saved_roles.get(role)
            if current is None or saved is None or current.cli != saved.get("cli"):
                state.sessions.pop(role, None)
                state.reviewer_sessions.pop(role, None)
                state.seen.pop(role, None)
                warnings.append(f"{role}: 역할이 없거나 CLI가 달라 저장된 세션을 버렸습니다.")
        if saved_roles != {n: r.to_yaml() for n, r in self.cfg.roles.items()}:
            warnings.append("저장 당시 역할 구성과 다릅니다. 현재 roles.yaml을 유지합니다.")
        if state.mode not in self.cfg.modes:
            warnings.append(f"저장된 모드 {state.mode}가 없어 review 모드를 사용합니다.")
            state.mode = "review"
        state.auto = False
        state.fork_on_resume = list(state.sessions)
        state.reviewer_fork_on_resume = list(state.reviewer_sessions)
        current_head = git_head(self.project)
        if meta.get("git_head") != current_head:
            warnings.append("저장 당시 git HEAD와 다릅니다. 코드는 복원하지 않았습니다. "
                            "/rollback 으로 코드도 맞출 수 있습니다.")
        self.loading = True
        self._session_ready.clear()
        try:
            autosave = "_autosave-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = write_save(self.cfg, autosave, autosave=True)
            self.bus.emit("save", None, name=autosave, last_n=backup["last_n"])
            # 원래 adapter들은 닫고, 다음 사람 메시지에서 저장된 id로 새로 생성한다.
            await self.close()
            self.dialogue.path.write_bytes(dialogue)
            self.cfg.state = state
            if state.task:
                state.task["waiting"] = True
                state.task["wait_reason"] = "저장본 복원 후 메인의 판단 대기"
                if state.task["role"] not in self.cfg.roles:
                    warnings.append(f"합의 task의 {state.task['role']} 역할이 없습니다. 복구하거나 CANCEL하세요.")
            self.cfg.save_state()
            self._stale_adapters.clear()
            self._stale_reviewer = False
            self.current_task.clear()
            self.hashes.clear()
            self.task_history.clear()
            self.notes.clear()
            self.policy = Policy(self.project, self.cfg.policy, self.cfg.main)
            self.policy.task = state.task
            self._warn_plan_hash()
            self.run_turns = self.extra_turns = 0
            self.stop_requested = False
            self.not_paused.set()
            autosaves = [m for m in self.list_saves()
                         if m.get("autosave") and m["name"].startswith("_autosave-")]
            for old in autosaves[5:]:
                try:
                    delete_save(self.cfg, old["name"])
                except OSError as e:
                    warnings.append(f"이전 자동 저장본 정리 실패: {e}")
            self.bus.emit("load", None, name=name, last_n=state.last_n)
            self.emit_status()
            for warning in warnings:
                self.notice(warning, "warn")
            message = f"저장본 {name} 불러옴 (턴 #{state.last_n}, 모드 {state.mode})"
            self.notice(message)
            return message
        finally:
            self.loading = False
            self._session_ready.set()

    def add_role(self, role: Role) -> None:
        self.cfg.roles[role.name] = role
        self.cfg.save_roles()
        self.bus.emit("roles", None, roles=list(self.cfg.roles.keys()))
        self.notice(f"역할 추가: {role.name} ({role.cli}/{role.model or '기본'})")
        self.emit_status()

    async def remove_role(self, name: str) -> str:
        if name == self.cfg.main:
            return "메인 역할은 지울 수 없습니다."
        if name not in self.cfg.roles:
            return f"'{name}' 역할이 없습니다."
        ad = self.adapters.pop(name, None)
        if ad:
            await ad.close()
        del self.cfg.roles[name]
        if self.cfg.state.task and self.cfg.state.task["role"] == name:
            self._wait_task(f"작업자 {name} 역할이 제거되었습니다. 복구하거나 CANCEL하세요.")
        self.cfg.state.sessions.pop(name, None)
        self.cfg.save_roles()
        self.cfg.save_state()
        self.bus.emit("roles", None, roles=list(self.cfg.roles.keys()))
        self.emit_status()
        return f"역할 삭제: {name}"

    async def edit_role(self, name: str, field: str, value: str) -> str:
        r = self.cfg.roles.get(name)
        if not r:
            return f"'{name}' 역할이 없습니다."
        if field not in ("cli", "model", "brief", "permissions", "effort"):
            return "바꿀 수 있는 항목: cli, model, brief, permissions, effort"
        if field == "cli" and value not in SUPPORTED_CLIS:
            return "cli 는 claude 또는 codex"
        setattr(r, field, value or None)
        if self.cfg.state.task and self.cfg.state.task["role"] == name and field in ("cli", "model", "permissions"):
            self._wait_task(f"작업자 {name}의 {field} 설정이 바뀌었습니다. 메인이 재개 여부를 판단하세요.")
        self.cfg.save_roles()
        ad = self.adapters.pop(name, None)
        if ad:  # 다음 턴부터 새 설정으로 세션을 다시 연다
            await ad.close()
        if field in ("cli", "model"):
            self.cfg.state.sessions.pop(name, None)
            self.cfg.save_state()
        if name == self.cfg.main and self.reviewer:
            await self.reviewer.close()
            self.reviewer = None
        return f"{name}.{field} = {value} (다음 턴부터 적용)"

    def set_mode(self, name: str) -> str:
        if name not in self.cfg.modes:
            return f"모드: {', '.join(self.cfg.modes)}"
        self.cfg.state.mode = name
        self.cfg.state.max_turns = -1
        self.cfg.save_state()
        self.emit_status()
        mt = self.cfg.max_turns
        return f"모드 {name} (턴 한도 {'∞' if mt is None else mt})"

    def set_max_turns(self, value: int | None) -> str:
        self.cfg.state.max_turns = value
        self.cfg.save_state()
        self.emit_status()
        return f"턴 한도: {'∞ (무제한)' if value is None else value}"

    def set_auto(self, on: bool) -> str:
        self.cfg.state.auto = on
        self.cfg.save_state()
        self.emit_status()
        return f"자동 위임: {'켜짐' if on else '꺼짐 (위임 전마다 확인)'}"

    def pause(self) -> None:
        self.not_paused.clear()
        self.emit_status()

    def resume(self) -> None:
        self.not_paused.set()
        self.emit_status()

    async def stop(self) -> None:
        self.stop_requested = True
        self._wait_task("사람이 /stop으로 중단했습니다")
        self.not_paused.set()
        if self.running_role and self.running_role in self.adapters:
            await self.adapters[self.running_role].interrupt()
        self.emit_status()

    async def rollback(self, n: int) -> str:
        if self.running:
            return "진행 중에는 롤백할 수 없습니다. /stop 후 다시 시도하세요."
        if not self.git.enabled:
            return "git 이 꺼져 있어 롤백할 수 없습니다."
        sha = self.git.find_turn_commit(n)
        if not sha:
            return f"#{n} 턴의 스냅샷을 찾지 못했습니다 (변경이 없던 턴은 스냅샷이 없습니다)."
        c = await self.ui.ask_choice("롤백 확인", f"#{n} 시점({sha[:8]})으로 되돌립니다. 이후의 코드·문서 변경은 사라집니다.",
                                     [("yes", "되돌리기"), ("no", "취소")])
        if c != "yes":
            return "롤백 취소"
        if not self.git.hard_reset(sha):
            return "git reset 실패"
        self.dialogue.append_note(f"[duet] 사람이 #{n} 시점으로 롤백했습니다. 이후 턴의 변경은 취소되었습니다.")
        return f"#{n} 시점으로 롤백했습니다."
