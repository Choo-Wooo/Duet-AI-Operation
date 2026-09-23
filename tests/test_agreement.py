"""T1~T10: 외부 모델/사람만 모의하고 합의 코어와 파일 I/O는 실제 실행한다."""
import asyncio
import copy
import json
import os
import shlex
import subprocess
import sys
from collections import defaultdict, deque
from pathlib import Path
from uuid import uuid4

import pytest

from duet import commands
from duet.adapters.base import AgentAdapter, TurnResult
from duet.bootstrap import _requirements_hash
from duet.core.agreement import (changed_files, fingerprint, new_task, parse_plan, plan_versions,
                                 read_plan, validate_task)
from duet.core.config import Config, Role
from duet.core.dialogue import Dialogue, extract_directives
from duet.core.events import EventBus
from duet.core.orchestrator import Orchestrator
from duet.core.policy import AUTO, DENY, ApprovalRequest

PLAN = """### 요구
응답 함수를 구현한다.
### 설계와 다른 점
없음
```files
src.py
test_src.py
```
### 수용 기준
AC1: answer()는 7을 반환한다.
### 테스트 계획
AC1: 실제 반환값 검사. 모델 응답만 모의한다.
test_command: python -m pytest -q
### 열린 질문
없음
<!-- duet: PLAN ready -->"""
DELEGATE = "<!-- duet: DELEGATE implementer -->\n<!-- duet: TASK 응답 함수를 구현하라 -->"


class UI:
    def __init__(self):
        self.answers = deque()
        self.questions = []

    async def ask_choice(self, title, body, options):
        self.questions.append((title, body, options))
        assert self.answers, f"Unexpected question: {title}"
        return self.answers.popleft()

    async def ask_approval(self, *args):
        raise AssertionError("Unexpected approval")


@pytest.fixture
def orch(tmp_path, monkeypatch):
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles = {"architect": Role("architect", "claude", permissions="read_only"),
                 "implementer": Role("implementer", "codex")}
    cfg.settings.update(git_snapshots=False, checkpoint_every=0)
    cfg.save_roles()
    cfg.save_state()
    Dialogue(tmp_path).ensure()
    bus, ui = EventBus(), UI()
    obj = Orchestrator(cfg, bus, ui, fake=True)
    obj.scripts = defaultdict(deque)
    obj.prompts, obj.events, obj.phase_states = [], [], []

    class ScriptAdapter(AgentAdapter):
        async def start(self):
            self.session_id = uuid4().hex
            self.fork_session = False
            self.closed = False

        async def run_turn(self, prompt):
            obj.prompts.append((self.role.name, self.turn_kind, prompt))
            assert obj.scripts[self.role.name], f"Missing script for {self.role.name}/{self.turn_kind}"
            body = obj.scripts[self.role.name].popleft()
            if callable(body):
                body = await body(self, prompt)
            return body if isinstance(body, TurnResult) else TurnResult(body, full_text=body)

        async def close(self):
            self.closed = True

        async def interrupt(self):
            pass

    def factory(role, project, bus, approver, session_id, system_append,
                reviewer=False, fake=False, fork_session=False):
        assert fake
        return ScriptAdapter(role, project, bus, approver, session_id, system_append, reviewer, fork_session)

    def event(ev):
        obj.events.append(ev)
        if ev.kind == "phase":
            obj.phase_states.append(copy.deepcopy(cfg.state.task))

    bus.subscribe(event)
    monkeypatch.setattr("duet.core.orchestrator.make_adapter", factory)
    monkeypatch.setattr("duet.core.orchestrator.git_head", lambda _: None)
    return obj


async def advance(orch, role, text, kind="system"):
    orch.scripts[role].append(text)
    result, turn = await orch._turn(role, kind, "테스트")
    return await orch._decide(role, kind, result, turn)


async def start(orch):
    return await advance(orch, "architect", DELEGATE, "human")


async def submit_plan(orch, body=PLAN):
    return await advance(orch, "implementer", body, "plan")


async def agreed(orch):
    await start(orch)
    await submit_plan(orch)
    return await advance(orch, "architect", "<!-- duet: AGREE v1 -->", "plan_review")


def test_T1_config_defaults_task_validation_and_dev_hash(orch, tmp_path):
    cfg = orch.cfg
    cfg.modes_file.write_text("modes:\n  sprint: {}\n  review: {}\n  deliberate: {}\n  custom: {}\n")
    cfg.load()
    assert not cfg.modes["sprint"].agreement
    assert all(cfg.modes[k].agreement for k in ("review", "deliberate", "custom"))
    cfg.save_modes()
    assert "agreement: true" in cfg.modes_file.read_text()
    assert cfg.state.task is None
    task = new_task("implementer", "task", None, {})
    cfg.state.task = task
    cfg.save_state()
    other = Config(tmp_path)
    other.load()
    assert other.state.task == task
    task["phase"] = "invalid"
    with pytest.raises(ValueError):
        validate_task(task)
    cfg.save_state()
    with pytest.raises(ValueError):
        other.load()
    runtime = tmp_path / "requirements.txt"
    dev = tmp_path / "requirements-dev.txt"
    runtime.write_text("pyyaml>=6\n")
    before = _requirements_hash(runtime)
    dev.write_text("pytest>=8,<10\n")
    assert _requirements_hash(runtime) == before


@pytest.mark.parametrize("field", ["agreed_version", "human_approved_version", "agreed_text_sha256", "base_commit"])
def test_T1_missing_task_keys_rejected(field):
    task = new_task("implementer", "task", None, {})
    del task[field]
    with pytest.raises(ValueError, match="누락"):
        validate_task(task)


def test_T3_files_block_examples_are_not_accepted():
    example = "````markdown\n```files\nexample.py\n```\n````\ntest_command: pytest\n"
    with pytest.raises(ValueError, match="files"):
        parse_plan(example)
    with pytest.raises(ValueError, match="test_command"):
        parse_plan(PLAN + "\ntest_command: another-command\n")


def test_T2_plan_readonly_recording_and_multiblock_response(orch):
    async def scenario():
        nxt = await start(orch)
        assert nxt[:2] == ("implementer", "plan")
        policy = orch.policy
        for path in ("src.py", "docs/plans/anything.md", "DIALOGUE.md"):
            req = ApprovalRequest("implementer", "file", "write", paths=[path])
            policy.session_allow.add(req.cache_key())
            assert policy.classify(req, orch.cfg.roles["implementer"])[0] == DENY
        result = TurnResult("<!-- duet: PLAN ready -->", full_text=PLAN)
        await submit_plan(orch, result)
        task = orch.cfg.state.task
        path = orch.project / task["plan_path"]
        first = path.read_text()
        assert "AC1" in first and "src.py" in first
        assert task["submitted_version"] == 1
        assert "AC1" not in orch.dialogue.read()  # 전문은 계획서에만 기록
        assert task["plan_path"] in orch.dialogue.read()
        assert orch.adapters["implementer"].plan_read_only
        assert "DIALOGUE.md와 docs/plans도 직접 쓰지 마세요" in orch.prompts[-1][2]
        await advance(orch, "architect", "<!-- duet: REVISE 테스트 근거 보완 -->")
        await submit_plan(orch, PLAN.replace("모델 응답만", "모델과 사람 응답만"))
        assert path.read_text().startswith(first)
        assert set(plan_versions(path.read_text())) == {1, 2}

    asyncio.run(scenario())


def test_T3_full_run_and_fixed_agreement(orch):
    async def implement(ad, prompt):
        assert not ad.plan_read_only
        assert "합의 v1" in prompt
        decision = await ad.approve(ApprovalRequest("implementer", "file", "write", paths=["src.py"]))
        assert decision.allow
        (ad.project / "src.py").write_text("def answer(): return 7\n")
        return "<!-- duet: REPORT done -->"

    orch.scripts["architect"].extend([DELEGATE, "<!-- duet: AGREE v1 -->",
                                      "<!-- duet: ACCEPT -->\n<!-- duet: STATUS done -->"])
    orch.scripts["implementer"].extend([PLAN, implement])
    asyncio.run(orch.run(("architect", "human", "1")))
    phases = [e.data["to"] for e in orch.events if e.kind == "phase"]
    assert phases == ["plan", "plan_review", "implement", "verify", "accepted"]
    assert orch.cfg.state.task is None
    implementation = next(t for t in orch.phase_states if t and t["phase"] == "implement")
    assert implementation["test_command"] == "python -m pytest -q"
    assert implementation["agreed_files"] == ["src.py", "test_src.py"]
    assert len(implementation["agreed_text_sha256"]) == 64
    verify_prompt = next(p for _, k, p in orch.prompts if k == "verify")
    assert "모의가 핵심 로직" in verify_prompt and "계획 외 변경 경고: 없음" in verify_prompt


@pytest.mark.parametrize("plan", [PLAN.replace("test_command:", "not_a_command:"),
                                  PLAN.replace("```files", "```text")])
def test_T3_missing_fields_reject_agree(orch, plan):
    async def scenario():
        await start(orch)
        await submit_plan(orch, plan)
        response = await advance(orch, "architect", "<!-- duet: AGREE v1 -->")
        assert response[1] == "system"
        assert orch.cfg.state.task["phase"] == "plan_review"
        assert orch.cfg.state.task["agreed_version"] is None
        assert any("AGREE 거부" in e.data.get("text", "") for e in orch.events)

    asyncio.run(scenario())


def test_T3_version_fields_not_mixed_and_bad_version_rejected(orch):
    async def scenario():
        await start(orch)
        await submit_plan(orch)
        bad = await advance(orch, "architect", "<!-- duet: AGREE v2 -->")
        assert bad[1] == "system"
        await advance(orch, "architect", "<!-- duet: REVISE 다른 테스트 -->")
        await submit_plan(orch, PLAN.replace("python -m pytest -q", "python -m pytest -x"))
        await advance(orch, "architect", "<!-- duet: AGREE v2 -->")
        task = orch.cfg.state.task
        assert task["test_command"] == "python -m pytest -x"
        assert parse_plan(read_plan(orch.project, task, 1))[1] == "python -m pytest -q"

    asyncio.run(scenario())


@pytest.mark.parametrize("choice", ["more", "wait", "cancel"])
def test_T4_round_boundary(orch, choice):
    async def scenario():
        await start(orch)
        for version in range(1, 4):
            await submit_plan(orch)
            assert orch.cfg.state.task["rounds"] == version
            assert not orch.ui.questions
            await advance(orch, "architect", f"<!-- duet: REVISE 근거 {version} -->")
        orch.ui.answers.append(choice)
        nxt = await submit_plan(orch)
        question = orch.ui.questions[-1]
        assert "구현자 #" in question[1] and "설계자 #" in question[1] and "근거 3" in question[1]
        if choice == "cancel":
            assert orch.cfg.state.task is None and nxt is None
        elif choice == "wait":
            assert orch.cfg.state.task["waiting"] and nxt is None
        else:
            assert nxt[1] == "plan_review"
            assert orch.cfg.state.task["rounds_extra"] == 1

    asyncio.run(scenario())


def test_T4_human_approval_and_auto_off(orch):
    async def scenario():
        orch.cfg.state.auto = False
        orch.ui.answers.append("yes")
        await start(orch)
        assert len(orch.ui.questions) == 1
        orch.cfg.settings["plan_approval"] = "human"
        await submit_plan(orch)
        orch.ui.answers.append("no")
        nxt = await advance(orch, "architect", "<!-- duet: AGREE v1 -->")
        assert nxt[1] == "plan" and not orch.cfg.state.task["agreed_version"]
        await submit_plan(orch)
        orch.ui.answers.append("yes")
        await advance(orch, "architect", "<!-- duet: AGREE v2 -->")
        assert orch.cfg.state.task["human_approved_version"] == 2
        await orch.stop()
        orch.stop_requested = False
        await advance(orch, "architect", "<!-- duet: RESUME -->")
        assert len(orch.ui.questions) == 3  # 새 위임 + 최종 승인 2회

    asyncio.run(scenario())


def test_T5_deviation_rework_and_next_delegate(orch):
    async def scenario():
        await agreed(orch)
        nxt = await advance(orch, "implementer", "<!-- duet: REPORT deviation 파일 추가 -->", "implement")
        assert nxt[1] == "plan"
        await submit_plan(orch)
        await advance(orch, "architect", "<!-- duet: AGREE v2 -->")
        deviation = PLAN.replace("<!-- duet: PLAN ready -->", "<!-- duet: REPORT deviation 테스트 보완 -->")
        nxt = await advance(orch, "implementer", deviation, "implement")
        assert nxt[1] == "plan_review" and orch.cfg.state.task["submitted_version"] == 3
        await advance(orch, "architect", "<!-- duet: AGREE v3 -->")
        await advance(orch, "implementer", "<!-- duet: REPORT done -->", "implement")
        baseline = copy.deepcopy(orch.cfg.state.task["agree_fingerprint"])
        await advance(orch, "architect", "<!-- duet: REWORK 경계값 검사 -->", "verify")
        assert orch.cfg.state.task["agree_fingerprint"] == baseline
        assert orch.cfg.state.task["agreed_version"] == 3
        await advance(orch, "implementer", "<!-- duet: REPORT done -->", "implement")
        old = orch.cfg.state.task["id"]
        nxt = await advance(orch, "architect", "<!-- duet: ACCEPT -->\n" + DELEGATE, "verify")
        assert nxt[1] == "plan" and orch.cfg.state.task["id"] != old

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["plan", "implement"])
def test_T5_resume_waiting_preserves_agreement_rounds_and_baselines(orch, phase):
    async def scenario():
        await (start(orch) if phase == "plan" else agreed(orch))
        orch._wait_task("원래 대기 사유")
        before = copy.deepcopy(orch.cfg.state.task)
        nxt = await advance(orch, "architect", "<!-- duet: RESUME -->", "human")
        assert nxt[:2] == ("implementer", phase)
        assert "원래 대기 사유" in nxt[2] and "메인 #" in nxt[2]
        main_prompt = orch.prompts[-1][2]
        assert "작업자 진행 상황을 검토" in main_prompt
        assert "계획 전문을 응답하세요." not in main_prompt
        after = orch.cfg.state.task
        for key in ("rounds", "agreed_version", "agreed_files", "test_command", "delegate_fingerprint", "agree_fingerprint"):
            assert before[key] == after[key]
        assert not after["waiting"]

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["no_task", "not_waiting", "worker", "plan_review", "verify"])
def test_T5_invalid_resume(orch, case):
    async def scenario():
        if case != "no_task":
            await start(orch)
            if case in ("plan_review", "verify"):
                await submit_plan(orch)
            if case == "verify":
                await advance(orch, "architect", "<!-- duet: AGREE v1 -->")
                await advance(orch, "implementer", "<!-- duet: REPORT done -->")
            if case != "not_waiting":
                orch._wait_task("대기")
        before = copy.deepcopy(orch.cfg.state.task)
        who = "implementer" if case == "worker" else "architect"
        nxt = await advance(orch, who, "<!-- duet: RESUME -->")
        assert nxt[1] == "system"
        assert orch.cfg.state.task == before

    asyncio.run(scenario())


def test_T5_cancel_and_no_overwrite(orch):
    async def scenario():
        await start(orch)
        task_id = orch.cfg.state.task["id"]
        nxt = await advance(orch, "architect", DELEGATE)
        assert nxt[1] == "system" and orch.cfg.state.task["id"] == task_id
        assert (await advance(orch, "implementer", "<!-- duet: CANCEL 임의 종료 -->"))[1] == "system"
        await advance(orch, "architect", "<!-- duet: CANCEL 사람이 취소 -->")
        assert orch.cfg.state.task is None

    asyncio.run(scenario())


@pytest.mark.parametrize("directive", ["REPORT blocked 원인", "ASK_HUMAN 선택 요청"])
def test_T5_wait_then_human_goes_to_main_even_with_to(orch, directive):
    async def scenario():
        await agreed(orch)
        await advance(orch, "implementer", f"<!-- duet: {directive} -->", "implement")
        assert orch.cfg.state.task["waiting"]
        wait_reason = orch.cfg.state.task["wait_reason"]
        orch.scripts["architect"].extend(["<!-- duet: RESUME -->", "<!-- duet: ACCEPT -->"])
        orch.scripts["implementer"].append("<!-- duet: REPORT done -->")
        done = asyncio.Event()
        orch.bus.subscribe(lambda e: done.set() if e.kind == "phase" and e.data["to"] == "accepted" else None)
        orch.submit("계속", to="implementer")
        server = asyncio.create_task(orch.serve())
        try:
            await asyncio.wait_for(done.wait(), 3)
            assert any(role == "architect" and kind == "human" for role, kind, _ in orch.prompts)
            resumed = next(p for r, k, p in reversed(orch.prompts) if r == "implementer")
            assert "RESUME" in resumed and wait_reason in resumed and "메인 #" in resumed
        finally:
            server.cancel()
            await asyncio.gather(server, return_exceptions=True)

    asyncio.run(scenario())


def test_T6_policy_cache_and_exact_verify_command(orch):
    policy = orch.policy
    task = new_task("implementer", "task", None, {})
    policy.task = task
    claude = Role("implementer", "claude")
    for req in (ApprovalRequest("implementer", "command", "touch", command="touch src.py"),
                ApprovalRequest("implementer", "file", "write", paths=["docs/plans/a.md"]),
                ApprovalRequest("implementer", "tool", "mcp", tool="mcp__write"),
                ApprovalRequest("implementer", "permissions", "expand")):
        policy.session_allow.add(req.cache_key())
        assert policy.classify(req, claude)[0] == DENY
    assert policy.classify(ApprovalRequest("implementer", "tool", "Read", tool="Read"), claude)[0] == AUTO
    task.update(phase="verify", test_command=".duet/venv/bin/python -m pytest -q")
    main = orch.cfg.roles["architect"]
    exact = ApprovalRequest("architect", "command", "test", command="  " + task["test_command"] + "  ")
    assert policy.classify(exact, main)[0] == AUTO
    policy.remember(exact)
    assert exact.cache_key() not in policy.session_allow
    for cmd in (task["test_command"] + " -x", "python -m pytest -q", "X=1 " + task["test_command"],
                "bash -lc '" + task["test_command"] + "'", task["test_command"] + "; touch x"):
        req = ApprovalRequest("architect", "command", "test", command=cmd)
        policy.session_allow.add(req.cache_key())
        assert policy.classify(req, main)[0] == DENY
    assert policy.classify(ApprovalRequest("architect", "command", "read", command="cat src.py"), main)[0] == AUTO


def test_T7_approved_subprocess_pass_and_fail(orch, tmp_path):
    async def scenario():
        project = tmp_path / "subprocess"
        project.mkdir()
        target = project / "answer.py"
        target.write_text("def answer(): return 7\n")
        test = project / "test_answer.py"
        test.write_text("from answer import answer\ndef test_answer(): assert answer() == 7\n")
        argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q", str(test)]
        await start(orch)
        await submit_plan(orch, PLAN.replace("python -m pytest -q", shlex.join(argv)))
        await advance(orch, "architect", "<!-- duet: AGREE v1 -->")
        await advance(orch, "implementer", "<!-- duet: REPORT done -->", "implement")
        req = ApprovalRequest("architect", "command", "test", command=orch.cfg.state.task["test_command"])
        decision = await orch.handle_approval(req)
        assert decision.allow and decision.scope == "once"
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        passed = subprocess.run(shlex.split(req.command), cwd=project, env=env, capture_output=True, text=True, timeout=30)
        assert passed.returncode == 0 and "1 passed" in passed.stdout
        target.write_text("def answer(): return 700\n")
        decision = await orch.handle_approval(req)
        assert decision.allow and decision.scope == "once"
        failed = subprocess.run(shlex.split(req.command), cwd=project, env=env, capture_output=True, text=True, timeout=30)
        assert failed.returncode != 0 and "1 failed" in failed.stdout
        assert req.cache_key() not in orch.policy.session_allow

    asyncio.run(scenario())


def test_T8_fingerprints_ignore_gitignore_and_runtime(orch):
    root = orch.project
    (root / ".gitignore").write_text("duet/\n")
    (root / "duet").mkdir()
    (root / "duet/code.py").write_text("old")
    (root / "deleted.py").write_text("deleted")
    before, errors = fingerprint(root)
    assert not errors
    (root / "duet/code.py").write_text("changed")
    (root / "deleted.py").unlink()
    (root / "new.py").write_text("new")
    for directory in (".git", ".duet/venv", ".duet/logs", ".duet/saves", "__pycache__",
                      ".pytest_cache", "node_modules", "DIALOGUE-archive", "docs/plans", ".idea", ".vscode"):
        p = root / directory
        p.mkdir(parents=True, exist_ok=True)
        (p / "noise").write_text("ignore")
    (root / ".DS_Store").write_text("ignore")
    orch.dialogue.append_turn("human", 1, "noise")
    after, errors = fingerprint(root)
    assert not errors
    assert changed_files(before, after) == ["deleted.py", "duet/code.py", "new.py"]


@pytest.mark.parametrize("configured", [None, [], ["local-cache", "local.log"]])
def test_T8_configured_exclusions_are_additive_and_match_names(orch, configured):
    cfg, root = orch.cfg, orch.project
    if configured is None:
        cfg.settings.pop("fingerprint_exclude")  # 기존 roles.yaml의 설정 누락
    else:
        cfg.settings["fingerprint_exclude"] = configured
    cfg.save_roles()
    loaded = Config(root)
    loaded.load()
    orch.cfg.settings = loaded.settings
    if configured is None:
        assert {".idea", ".vscode", ".DS_Store"} <= set(loaded.settings["fingerprint_exclude"])
    else:
        assert loaded.settings["fingerprint_exclude"] == configured

    defaults = [".idea/workspace.xml", "nested/.idea/workspace.xml", ".vscode/settings.json",
                ".DS_Store", "nested/.DS_Store", ".git/noise", "node_modules/noise",
                ".duet/logs/noise"]
    custom = ["local-cache/cache.bin", "nested/local-cache/cache.bin", "local.log", "nested/local.log"]
    regular = ["source.py", "nested/source.py", "local-cache-other.txt"]
    for name in defaults + custom + regular:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("before")
    before = orch._fingerprint()
    assert not set(defaults) & before.keys()
    for name in defaults + custom + regular:
        (root / name).write_text("after")
    after = orch._fingerprint()
    assert not set(defaults) & after.keys()
    assert changed_files(before, after) == sorted(regular + ([] if configured else custom))
    if configured:
        assert not set(custom) & after.keys()


@pytest.mark.parametrize("value", ["local-cache", None, [42], ["nested/cache"]])
def test_T8_invalid_exclusion_settings_rejected(orch, value):
    orch.cfg.settings["fingerprint_exclude"] = value
    orch.cfg.save_roles()
    with pytest.raises(ValueError, match="fingerprint_exclude"):
        Config(orch.project).load()


def test_T8_plan_and_verify_use_distinct_baselines(orch):
    async def scenario():
        await start(orch)
        (orch.project / "plan-violation.py").write_text("unauthorized")
        await submit_plan(orch)
        assert "plan-violation.py" in orch.cfg.state.task["plan_changes"]
        assert any("plan 단계 파일 변경 경고" in e.data.get("text", "") for e in orch.events)
        await advance(orch, "architect", "<!-- duet: AGREE v1 -->", "plan_review")
        (orch.project / "src.py").write_text("allowed")
        (orch.project / "unplanned.py").write_text("extra")
        await advance(orch, "implementer", "<!-- duet: REPORT done -->", "implement")
        await advance(orch, "architect", "<!-- duet: ACCEPT -->", "verify")
        prompt = orch.prompts[-1][2]
        assert "계획 외 변경 경고: unplanned.py" in prompt
        assert "AGREE 시점 이후 변경: src.py, unplanned.py" in prompt

    asyncio.run(scenario())


def test_T8_verify_turn_changes_warn_before_accept(orch):
    async def verify(ad, prompt):
        (ad.project / "review-extra.txt").write_text("review changed a file")
        return "<!-- duet: ACCEPT -->"

    async def scenario():
        await agreed(orch)
        await advance(orch, "implementer", "<!-- duet: REPORT done -->", "implement")
        await advance(orch, "architect", verify, "verify")
        assert orch.cfg.state.task is None
        assert any(e.data.get("text") == "계획 외 변경 경고: review-extra.txt" for e in orch.events)

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["plan", "plan_review", "implement", "verify"])
def test_T9_save_load_and_restart_keep_task_and_route_main(orch, phase):
    async def scenario():
        await start(orch)
        if phase != "plan":
            await submit_plan(orch)
        if phase in ("implement", "verify"):
            await advance(orch, "architect", "<!-- duet: AGREE v1 -->")
        if phase == "verify":
            await advance(orch, "implementer", "<!-- duet: REPORT done -->")
        task = copy.deepcopy(orch.cfg.state.task)
        orch.save("task")
        reloaded = Config(orch.project)
        reloaded.load()
        restarted = Orchestrator(reloaded, EventBus(), UI(), fake=True)
        assert restarted.cfg.state.task["phase"] == phase
        assert restarted.cfg.state.task["waiting"]
        if task["agreed_version"]:
            (orch.project / task["plan_path"]).write_text("changed outside snapshot")
        await orch.load_save("task")
        restored = orch.cfg.state.task
        assert restored["waiting"]
        for key in ("id", "phase", "instruction", "agreed_version", "test_command", "rounds", "agree_fingerprint"):
            assert restored[key] == task[key]
        if task["agreed_version"]:
            assert any("합의 계획 문서 경고" in e.data.get("text", "") for e in orch.events)
            (orch.project / task["plan_path"]).unlink()
            orch.events.clear()
            orch._warn_plan_hash()
            assert any("합의 계획 문서 경고" in e.data.get("text", "") for e in orch.events)
            assert restored["test_command"] == task["test_command"]
            assert restored["agreed_files"] == task["agreed_files"]
        orch.submit("현재 상황 설명", to="implementer")
        nxt = orch._drain_inbox(None)
        assert nxt[0] == "architect" and nxt[1] == "human"
        damaged = orch.cfg.saves_dir / "task/state.json"
        state = json.loads(damaged.read_text())
        state["task"]["phase"] = "broken"
        damaged.write_text(json.dumps(state))
        with pytest.raises(ValueError, match="task"):
            await orch.load_save("task")

    asyncio.run(scenario())


def test_T10_common_directive_filter_status_plan_and_sprint(orch):
    body = ('```html\n<!-- duet: DELEGATE ignored -->\n```\n'
            '> <!-- duet: CANCEL quoted -->\n~~~\n<!-- duet: ACCEPT -->\n~~~\n'
            '<!-- duet: REPORT done -->')
    assert extract_directives(body) == [("REPORT", "done")]

    async def scenario():
        assert "없음" in await commands.handle(orch, "/plan")
        await start(orch)
        task = orch.cfg.state.task
        assert task["plan_path"] in await commands.handle(orch, "/plan")
        assert "plan" in await commands.handle(orch, "/status")
        orch.set_mode("sprint")
        assert orch.cfg.state.task["agreement"]
        await advance(orch, "architect", "<!-- duet: CANCEL 테스트 종료 -->")
        nxt = await start(orch)
        assert nxt[:2] == ("implementer", "delegate")
        assert orch.cfg.state.task is None
        nxt = await advance(orch, "implementer", body, "delegate")
        assert nxt[:2] == ("architect", "report")

    asyncio.run(scenario())
