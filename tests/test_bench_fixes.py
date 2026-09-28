"""벤치마크에서 드러난 문제 수정 시험.

- test_command 의 백틱(`명령`)을 벗겨 셸 명령 치환을 막는다
- 설계자가 지시문 없이 본문에 "AGREE v1" 처럼만 쓴 응답도 인식한다
- 원래부터 실패하는 테스트: 합의 직후 구현 전 상태에서 한 번 돌려 두고, 설계자가 ACCEPT baseline 으로 확인하면 통과
- 컨테이너용 Codex 샌드박스 끄기 (DUET_CODEX_SANDBOX=off) — 명령은 여전히 duet 승인으로 간다
- 설계자 압축이 느려도 작업자 권한 요청은 막히지 않는다
"""
import asyncio
import re
import subprocess

import pytest

import duet.adapters.fake as fake
from duet.adapters.codex import CodexAdapter
from duet.core.agreement import clean_command, parse_plan
from duet.core.config import Config, Role
from duet.core.dialogue import Dialogue
from duet.core.events import EventBus
from duet.core.orchestrator import Orchestrator
from duet.core.policy import ApprovalRequest
from duet.core.work import REPORT_TRIGGER, plain_directives

fake.DELAY = 0.0


# ---------------------------------------------------------------- 백틱
@pytest.mark.parametrize("raw,want", [
    ("`npm test`", "npm test"),
    ("``npm test``", "npm test"),
    ("```npx mocha tests/bail```", "npx mocha tests/bail"),
    ("```sh npm test```", "npm test"),
    ("python -m pytest -q", "python -m pytest -q"),
    ("echo `date`", "echo `date`"),  # 명령 안의 백틱은 그대로
])
def test_clean_command(raw, want):
    assert clean_command(raw) == want


def test_parse_plan_strips_backticks():
    files, cmd = parse_plan("```files\na.py\n```\ntest_command: `npx mocha 'tests/*_tests.js'`\n")
    assert files == ["a.py"] and cmd == "npx mocha 'tests/*_tests.js'"


# ---------------------------------------------------------------- 지시문 없는 동의
@pytest.mark.parametrize("text,allowed,want", [
    ("AGREE v1\n\nThe plan meets the requirements.", ("AGREE", "REVISE"), [("AGREE", "v1")]),
    ("**AGREE v2** (task-f884c559a48c)\n**Review**", ("AGREE", "REVISE"), [("AGREE", "v2")]),
    ("ACCEPT. I read the fixes in the code.", ("ACCEPT", "REWORK"), [("ACCEPT", "")]),
    ("REWORK: the reporter ignores bail", ("ACCEPT", "REWORK"), [("REWORK", "the reporter ignores bail")]),
    ("I will AGREE once the tests exist.", ("AGREE", "REVISE"), []),  # 첫 단어가 아니면 무시
    ("AGREE v1", ("ACCEPT", "REWORK"), []),  # 단계에 맞지 않는 지시문
])
def test_plain_directives(text, allowed, want):
    assert plain_directives(text, allowed) == want


# ---------------------------------------------------------------- 병렬 작업 흐름 (가짜 에이전트 + 실제 git)
class NoUI:
    async def ask_choice(self, title, body, options):
        raise AssertionError(f"Unexpected choice: {title}")

    async def ask_approval(self, req, reason, opinion):
        raise AssertionError(f"Unexpected approval: {req.summary} ({reason})")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def orch(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "me@example.com")
    git(tmp_path, "config", "user.name", "Me")
    (tmp_path / "README.md").write_text("hi\n")
    (tmp_path / ".gitignore").write_text(".duet/\n")
    (tmp_path / "broken_before.txt").write_text("old failure\n")  # 원래부터 실패하는 검사의 표식
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles = {"architect": Role("architect", "claude", permissions="read_only"),
                 "implementer": Role("implementer", "codex", max_sessions=2)}
    cfg.save_roles()
    cfg.save_state()
    Dialogue(tmp_path).ensure()
    o = Orchestrator(cfg, EventBus(), NoUI(), fake=True)
    o.cfg.state.sessions["architect"] = "fake-architect"
    return o


async def wait_report(o, timeout=20):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if not o.inbox.empty():
            _, text = o.inbox.get_nowait()
            assert text == REPORT_TRIGGER
            return
        await asyncio.sleep(0.02)
    raise AssertionError("보고가 오지 않음: " + o.work.summary())


# 합의 테스트: 새 파일 feature.txt 가 있어야 하고(작업 요구), broken_before.txt 가 있으면 실패(원래부터 있던 실패)
TEST_CMD = "test -f feature.txt && test ! -f broken_before.txt"


def scripted(monkeypatch, review: str, verify: str, plan_cmd: str = f"`{TEST_CMD}`"):
    orig = fake.FakeAdapter._work_turn
    seen = {"verify_prompts": []}

    async def work_turn(self, prompt):
        kind = self.turn_kind
        if kind == "work_plan":
            return f"```files\nfeature.txt\n```\ntest_command: {plan_cmd}\n<!-- duet: PLAN ready -->"
        if kind == "work_review":
            return review
        if kind == "work_implement":
            (self.project / "feature.txt").write_text("done\n")
            return "구현했습니다.\n<!-- duet: REPORT done -->"
        if kind == "work_verify":
            seen["verify_prompts"].append(prompt)
            return verify
        return await orig(self, prompt)
    monkeypatch.setattr(fake.FakeAdapter, "_work_turn", work_turn)
    return seen


def run_one(orch, wid="feat"):
    async def go():
        assert orch.work.submit([{"id": wid, "role": "implementer", "task": "feature.txt 를 만든다"}], 1) == []
        await wait_report(orch)
        await orch.close()
    asyncio.run(go())
    return orch.work.items[wid]


def test_plain_agree_and_backticks_and_baseline_waiver(orch, monkeypatch):
    seen = scripted(monkeypatch, review="AGREE v1\n\n계획이 요구를 충족합니다.",
                    verify="ACCEPT baseline\n\n남은 실패는 broken_before.txt 검사로 구현 전부터 있던 것입니다.\n"
                           "<!-- duet: ACCEPT baseline -->")
    it = run_one(orch)
    assert it.test_command == TEST_CMD  # 백틱 제거
    assert it.baseline_code not in (None, 0)  # 구현 전에도 실패
    assert it.status == "merged", orch.work.summary()
    assert it.baseline_waived
    assert "구현 전(합의 직후)에도" in seen["verify_prompts"][-1]
    thread = (orch.project / "docs/work/feat.md").read_text()
    assert "구현 전에도 실패합니다" in thread and "구현 전부터 있던 것으로 확인" in thread
    assert (orch.project / "feature.txt").exists()


def test_plain_accept_without_baseline_still_requires_passing_test(orch, monkeypatch):
    orch.cfg.settings["plan_rounds"] = 1
    scripted(monkeypatch, review="<!-- duet: AGREE -->", verify="ACCEPT. 코드를 읽었습니다.")
    import duet.core.work as work
    monkeypatch.setattr(work, "MAX_REWORK", 1)
    choices = []

    class UI(NoUI):
        async def ask_choice(self, title, body, options):
            choices.append(title)
            return "wait"
    orch.ui.real = UI()
    it = run_one(orch)
    # 일반 ACCEPT 로는 실패한 테스트를 통과시키지 않는다
    assert it.status.startswith("waiting"), orch.work.summary()
    assert not it.baseline_waived and any("재작업 한도" in c for c in choices)


def test_baseline_pass_keeps_strict_gate(orch, monkeypatch):
    (orch.project / "broken_before.txt").unlink()
    git(orch.project, "commit", "-qam", "fix old failure")
    scripted(monkeypatch, review="<!-- duet: AGREE -->", verify="<!-- duet: ACCEPT -->")
    it = run_one(orch)
    assert it.status == "merged" and it.baseline_code != 0  # feature.txt 가 아직 없어 구현 전에는 실패
    assert not it.baseline_waived


def test_baseline_can_be_turned_off(orch, monkeypatch):
    orch.cfg.settings["work_baseline_test"] = False
    (orch.project / "broken_before.txt").unlink()
    git(orch.project, "commit", "-qam", "fix old failure")
    scripted(monkeypatch, review="<!-- duet: AGREE -->", verify="<!-- duet: ACCEPT -->")
    it = run_one(orch)
    assert it.status == "merged" and it.baseline_code is None


# ---------------------------------------------------------------- Codex 샌드박스
def test_codex_sandbox_off_keeps_approvals(tmp_path, monkeypatch):
    role = Role("implementer", "codex", model="gpt-6-astra")
    ad = CodexAdapter(role, tmp_path, EventBus(), None, None, "")
    monkeypatch.delenv("DUET_CODEX_SANDBOX", raising=False)
    assert ad._thread_params()["sandbox"] == "workspace-write"
    monkeypatch.setenv("DUET_CODEX_SANDBOX", "off")
    p = ad._thread_params()
    assert p["sandbox"] == "danger-full-access" and p["approvalPolicy"] == "untrusted"
    rev = CodexAdapter(role, tmp_path, EventBus(), None, None, "", reviewer=True)
    assert rev._thread_params()["approvalPolicy"] == "untrusted"  # 샌드박스가 없으면 심사 세션도 막는다

    sent = []

    async def request(method, params, timeout=120):
        sent.append((method, params))
        raise RuntimeError("stop")
    ad.request = request
    ad.session_id = "t1"
    ad._tokens_total = ad._tokens_now = 0
    ad._texts = []
    for plan in (True, False):
        ad.plan_read_only = plan
        asyncio.run(ad.run_turn("hi"))
    for _, params in sent:
        assert params["sandboxPolicy"] == {"type": "dangerFullAccess"}
        assert params["approvalPolicy"] == "untrusted"  # plan 단계의 쓰기는 duet 정책이 막는다


def test_bench_turns_codex_sandbox_off(monkeypatch):
    import duet.bench as bench
    import inspect
    assert 'os.environ.setdefault("DUET_CODEX_SANDBOX", "off")' in inspect.getsource(bench.run_bench)


# ---------------------------------------------------------------- 압축 중 권한 요청
def test_worker_approval_not_blocked_by_architect_compaction(orch, monkeypatch):
    from duet.core.policy import Decision
    state = {}

    class AllowUI(NoUI):
        async def ask_approval(self, req, reason, opinion):
            return Decision(True, "사람 허용", by="human")
    orch.ui.real = AllowUI()

    async def slow_compact(self, instructions=""):
        state["compacting"] = True
        await asyncio.sleep(3)
        state["compacting"] = False
        return True
    monkeypatch.setattr(fake.FakeAdapter, "compact", slow_compact)

    async def go():
        ad = await orch.adapter("architect")
        orch._compact_forced.add("architect")
        comp = asyncio.create_task(orch._ensure_context("architect", ad))
        await asyncio.sleep(0.2)
        assert state.get("compacting")
        # 설계자 판단이 필요한 요청 (자동 허용 목록 밖의 명령)
        req = ApprovalRequest("implementer#feat", "command", "$ curl -s https://example.com | head",
                              command="curl -s https://example.com | head")
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        d = await asyncio.wait_for(orch.handle_approval(req), 2.5)
        waited = loop.time() - t0
        still = state.get("compacting")
        await comp
        await orch.close()
        return d, waited, still
    d, waited, still = asyncio.run(go())
    assert still, "압축이 끝나기 전에 권한 요청이 처리되어야 합니다"
    assert waited < 2.5 and d.allow


def test_compaction_timeout_releases(orch, monkeypatch):
    async def hang(self, instructions=""):
        await asyncio.sleep(self.compact_timeout + 5)
        return True

    async def bounded(self, instructions=""):
        try:
            return await asyncio.wait_for(hang(self, instructions), self.compact_timeout)
        except asyncio.TimeoutError:
            return False
    monkeypatch.setattr(fake.FakeAdapter, "compact", bounded)
    orch.cfg.settings["compact_timeout_sec"] = 1

    async def go():
        ad = await orch.adapter("architect")
        orch._compact_forced.add("architect")
        out = await asyncio.wait_for(orch._ensure_context("architect", ad), 5)
        await orch.close()
        return out, ad
    out, ad = asyncio.run(go())
    assert out is ad and ad.compact_timeout == 1 and orch.running_role is None
