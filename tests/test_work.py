"""병렬 작업 보드: 워크트리·로컬 브랜치·계획 합의·검증·squash 병합·삭제·협의 (가짜 에이전트 + 실제 git)."""
import asyncio
import subprocess

import pytest

from duet.core.config import Config, Role
from duet.core.dialogue import Dialogue
from duet.core.events import EventBus
from duet.core.orchestrator import Orchestrator
from duet.core.work import REPORT_TRIGGER, parse_work_block
from duet.core.worktrees import Worktrees

import duet.adapters.fake as fake
fake.DELAY = 0.0


class NoUI:
    async def ask_choice(self, title, body, options):
        raise AssertionError(f"Unexpected choice: {title}")

    async def ask_approval(self, req, reason, opinion):
        raise AssertionError(f"Unexpected approval: {req.summary} ({reason})")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def orch(tmp_path):
    git(tmp_path, "init", "-q", "-b", "feature/assigned")
    git(tmp_path, "config", "user.email", "me@example.com")
    git(tmp_path, "config", "user.name", "Me")
    (tmp_path / "README.md").write_text("hi\n")
    (tmp_path / ".gitignore").write_text(".duet/\n")
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
            to, text = o.inbox.get_nowait()
            assert text == REPORT_TRIGGER
            return
        await asyncio.sleep(0.02)
    raise AssertionError("보고가 오지 않음: " + o.work.summary())


def test_parse_block():
    text = "설계\n```duet-work\n- id: a\n  role: implementer\n  task: x\n```\n"
    assert parse_work_block(text) == [{"id": "a", "role": "implementer", "task": "x"}]
    assert parse_work_block("없음") is None


def test_rejects_bad_specs(orch):
    errs = orch.work.submit([{"id": "Bad_ID", "role": "architect", "task": ""},
                             {"id": "b", "role": "implementer", "task": "x", "depends_on": ["zzz"]}], 1)
    joined = "\n".join(errs)
    assert "소문자" in joined and "맡길 수 없습니다" in joined and "task" in joined and "zzz" in joined
    cyc = orch.work.submit([{"id": "a", "role": "implementer", "task": "x", "depends_on": ["b"]},
                            {"id": "b", "role": "implementer", "task": "y", "depends_on": ["a"]}], 1)
    assert any("순환" in e for e in cyc)


def test_parallel_flow_merges_into_base_and_cleans_up(orch):
    root = orch.project

    async def go():
        errs = orch.work.submit([
            {"id": "api", "role": "implementer", "task": "api.txt 를 만든다"},
            {"id": "db", "role": "implementer", "task": "db.txt 를 만든다"},
            {"id": "ui", "role": "implementer", "task": "ui.txt 를 만든다", "depends_on": ["api"]},
        ], 3)
        assert errs == []
        # 역할별 max_sessions=2 → 처음엔 둘만 동시에
        assert set(orch.work.tasks) == {"api", "db"}
        await wait_report(orch)
        await orch.close()
    asyncio.run(go())
    items = orch.work.items
    assert all(i.status == "merged" for i in items.values()), orch.work.summary()
    assert git(root, "branch", "--show-current") == "feature/assigned"
    for f in ("api.txt", "db.txt", "ui.txt"):
        assert (root / f).exists()
    log = git(root, "log", "--format=%an|%s")
    assert "Me|api.txt 를 만든다" in log and "Me|ui.txt 를 만든다" in log
    assert Worktrees(root).local_work_branches() == []
    assert not any((root / ".duet" / "worktrees").iterdir())
    hook = root / ".git" / "hooks" / "pre-push"
    assert "duet/work" in hook.read_text()
    thread = (root / "docs" / "work" / "ui.md").read_text()
    assert "계획 v1 합의" in thread and "병합 완료" in thread
    assert "병렬 작업 보고" in orch.work.report_prompt()


def test_consult_goes_to_architect_fork(orch):
    async def go():
        assert orch.work.submit([{"id": "consult-a", "role": "implementer", "task": "c"}], 1) == []
        await wait_report(orch)
        await orch.close()
    asyncio.run(go())
    it = orch.work.items["consult-a"]
    assert it.status == "merged" and it.consults == 1
    thread = (orch.project / it.thread).read_text()
    assert "work_consult" in thread and "파일 이름은 작업 id" in thread


def test_pre_push_blocks_work_branch(orch, tmp_path):
    wt = Worktrees(orch.project)
    wt.install_pre_push()
    remote = tmp_path.parent / (tmp_path.name + "-remote.git")
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    git(orch.project, "remote", "add", "origin", str(remote))
    git(orch.project, "branch", "duet/work/x")
    r = subprocess.run(["git", "push", "origin", "duet/work/x"], cwd=orch.project, capture_output=True, text=True)
    assert r.returncode != 0 and "push 하지 않습니다" in r.stderr
    r = subprocess.run(["git", "push", "origin", "feature/assigned"], cwd=orch.project, capture_output=True, text=True)
    assert r.returncode == 0


def test_delegate_rejected_while_work_running_and_report_trigger(orch):
    async def go():
        orch.work.submit([{"id": "api", "role": "implementer", "task": "a"}], 1)
        from duet.adapters.base import TurnResult
        from duet.core.dialogue import Turn
        body = "<!-- duet: DELEGATE implementer -->\n<!-- duet: TASK x -->"
        nxt = await orch._decide("architect", "human", TurnResult(body, full_text=body),
                                 Turn("architect", 5, "", 0, 0, body))
        await wait_report(orch)
        await orch.close()
        return nxt
    nxt = asyncio.run(go())
    assert nxt[0] == "architect" and "병렬 작업이 진행 중" in nxt[2]


def test_conflict_goes_back_to_worker_then_merges(orch, monkeypatch):
    orig = fake.FakeAdapter._work_turn

    async def work_turn(self, prompt):
        if self.turn_kind == "work_plan":
            import re as _re
            wid = _re.search(r"병렬 작업 ([a-z0-9-]+)", prompt).group(1)
            return ("```files\nshared.txt\n```\ntest_command: test -f shared.txt\n<!-- duet: PLAN ready -->")
        if self.turn_kind == "work_implement":
            p = self.project / "shared.txt"
            wid = self.role.name.split("#")[1]
            if p.exists() and "<<<<<<<" in p.read_text():
                p.write_text("a\nb\n")  # 충돌 해결
            else:
                p.write_text(wid + "\n")
            return "done\n<!-- duet: REPORT done -->"
        return await orig(self, prompt)
    monkeypatch.setattr(fake.FakeAdapter, "_work_turn", work_turn)

    async def go():
        assert orch.work.submit([{"id": "a", "role": "implementer", "task": "a"},
                                 {"id": "b", "role": "implementer", "task": "b"}], 1) == []
        await wait_report(orch)
        await orch.close()
    asyncio.run(go())
    assert all(i.status == "merged" for i in orch.work.items.values()), orch.work.summary()
    assert (orch.project / "shared.txt").read_text() == "a\nb\n"
    threads = (orch.project / "docs/work/a.md").read_text() + (orch.project / "docs/work/b.md").read_text()
    assert "병합 충돌" in threads
