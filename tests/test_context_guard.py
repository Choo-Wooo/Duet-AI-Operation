"""컨텍스트·프롬프트 크기 안정화 로직 테스트. 모든 쓰기는 tmp_path 안에서만."""
import asyncio
from uuid import uuid4

import pytest

from duet.adapters.base import AgentAdapter, TurnResult, is_context_overflow
from duet.adapters.claude import _context_size
from duet.core.config import Config, Role
from duet.core.dialogue import Dialogue
from duet.core.events import EventBus
from duet.core.orchestrator import Orchestrator, summarize_paths


class NoUI:
    async def ask_choice(self, *args):
        raise AssertionError("Unexpected UI question")

    async def ask_approval(self, *args):
        raise AssertionError("Unexpected approval")


class SizedAdapter(AgentAdapter):
    """context_tokens 와 압축 성공 여부, 한도 초과 오류를 흉내 낸다."""
    instances: list = []
    compact_ok = True
    overflow_first = False

    async def start(self):
        self.session_id = self.session_id or uuid4().hex
        self.prompts, self.compacts, self.closed = [], 0, False
        SizedAdapter.instances.append(self)

    async def run_turn(self, prompt):
        prompt = self.recovery_prompt(prompt)
        self.prompts.append(prompt)
        if SizedAdapter.overflow_first and len(SizedAdapter.instances) == 1:
            return TurnResult("Prompt is too long", ok=False, error="Prompt is too long", context_overflow=True)
        reply = "완료\n\n```duet-memory\n## 목표\n맵 분석\n```"
        return TurnResult(reply, full_text="중간 설명\n\n" + reply)

    async def compact(self, instructions=""):
        self.compacts += 1
        self.last_instructions = instructions
        if SizedAdapter.compact_ok:
            self.context_tokens = 0
        return SizedAdapter.compact_ok

    async def interrupt(self):
        pass

    async def close(self):
        self.closed = True


@pytest.fixture
def orch(tmp_path, monkeypatch):
    SizedAdapter.instances = []
    SizedAdapter.compact_ok = True
    SizedAdapter.overflow_first = False
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles = {"architect": Role("architect", "claude"), "implementer": Role("implementer", "codex")}
    cfg.settings["git_snapshots"] = False
    cfg.save_roles()
    cfg.save_state()
    Dialogue(tmp_path).ensure()
    obj = Orchestrator(cfg, EventBus(), NoUI(), fake=True)
    obj.events = []
    obj.bus.subscribe(obj.events.append)

    def factory(role, project, bus, approver, session_id, system_append,
                reviewer=False, fake=False, fork_session=False):
        return SizedAdapter(role, project, bus, approver, session_id, system_append, reviewer, fork_session)

    monkeypatch.setattr("duet.core.orchestrator.make_adapter", factory)
    return obj


def run(coro):
    return asyncio.run(coro)


def test_overflow_detection_and_usage_size():
    assert is_context_overflow("Prompt is too long · the request is ~1229027 tokens")
    assert is_context_overflow(None, "context_length_exceeded")
    assert not is_context_overflow("You've hit your usage limit")
    assert _context_size({"input_tokens": 5, "cache_read_input_tokens": 90000,
                          "cache_creation_input_tokens": 1000, "output_tokens": 999}) == 91005


def test_summarize_paths_is_short():
    paths = [f"server/world/region/r.{i}.0.mca" for i in range(5000)] + ["backups/a.tar.gz"]
    s = summarize_paths(paths)
    assert "5001개 파일" in s and "server/world/ 5000개" in s and len(s) < 1000
    assert summarize_paths(["a", "b"]) == "a, b"


def test_long_prompt_is_moved_to_file(orch):
    orch.cfg.settings["prompt_max_chars"] = 1000
    guarded = orch._guard_prompt("x" * 5000, 7, "architect")
    assert len(guarded) < 1300 and ".duet/reports/prompt-7-architect.md" in guarded
    assert (orch.project / ".duet/reports/prompt-7-architect.md").read_text().startswith("x" * 5000)


def test_compacts_when_context_over_limit(orch):
    async def go():
        ad = await orch.adapter("architect")
        ad.context_tokens = 150_000
        orch._turn_counter = 10
        same = await orch._ensure_context("architect", ad)
        return ad, same
    ad, same = run(go())
    assert same is ad and ad.compacts == 1 and not ad.closed


def test_rotates_when_compaction_fails(orch):
    SizedAdapter.compact_ok = False

    async def go():
        ad = await orch.adapter("implementer")
        old_session = ad.session_id
        ad.context_tokens = 150_000
        orch._turn_counter = 10
        new = await orch._ensure_context("implementer", ad)
        return ad, new, old_session
    ad, new, old_session = run(go())
    assert ad.closed and new is not ad and new.session_id != old_session
    assert new.handoff and "새 세션" in new.handoff
    assert orch.cfg.state.sessions["implementer"] == new.session_id


def test_rotates_if_still_over_right_after_compaction(orch):
    async def go():
        ad = await orch.adapter("architect")
        orch._turn_counter = 10
        orch._compacted_at["architect"] = 9  # 방금 압축했는데도
        ad.context_tokens = 150_000
        return ad, await orch._ensure_context("architect", ad)
    ad, new = run(go())
    assert ad.compacts == 0 and ad.closed and new is not ad


def test_under_limit_does_nothing(orch):
    async def go():
        ad = await orch.adapter("architect")
        ad.context_tokens = 50_000
        return ad, await orch._ensure_context("architect", ad)
    ad, same = run(go())
    assert same is ad and ad.compacts == 0


def test_overflow_error_retries_once_in_new_session(orch):
    SizedAdapter.overflow_first = True
    res = run(orch._turn("architect", "human", "1"))
    assert res is not None
    tr, turn = res
    assert tr.ok and len(SizedAdapter.instances) == 2
    first, second = SizedAdapter.instances
    assert first.closed and second.prompts[0].startswith("[duet] 이전 세션이 컨텍스트 한도를 넘어")
    assert not any(e.kind == "error" for e in orch.events)


def test_memory_block_is_saved_and_stripped(orch):
    tr, turn = run(orch._turn("architect", "human", "1"))
    mem = (orch.project / ".duet/memory/architect.md").read_text()
    assert "## 목표\n맵 분석" in mem
    assert "duet-memory" not in (tr.text or "") and "duet-memory" not in (tr.full_text or "")
    assert "duet-memory" not in orch.dialogue.read()
    assert any(e.kind == "memory" for e in orch.events)


def test_role_context_limit_overrides_setting(orch):
    orch.cfg.roles["architect"].context_limit = 500_000

    async def go():
        ad = await orch.adapter("architect")
        ad.context_tokens = 300_000
        return ad, await orch._ensure_context("architect", ad)
    ad, same = run(go())
    assert same is ad and ad.compacts == 0 and orch.context_limit("architect") == 500_000
    assert orch.context_limit("implementer") == 100_000


def test_task_boundary_compaction_uses_role_instructions(orch):
    async def go():
        ad = await orch.adapter("architect")
        small = await orch.adapter("implementer")
        ad.context_tokens, small.context_tokens = 80_000, 10_000
        orch.mark_compaction_due()
        await orch._ensure_context("architect", ad)
        await orch._ensure_context("implementer", small)
        return ad, small
    ad, small = run(go())
    assert ad.compacts == 1 and "설계 결정" in ad.last_instructions
    assert small.compacts == 0  # 기준(40k) 미만은 건너뜀


def test_forced_compaction_ignores_floor(orch):
    async def go():
        ad = await orch.adapter("implementer")
        ad.context_tokens = 5_000
        orch._compact_forced.add("implementer")
        await orch._ensure_context("implementer", ad)
        return ad
    ad = run(go())
    assert ad.compacts == 1 and "바꾼 파일" in ad.last_instructions


def test_memory_dir_not_in_fingerprint(tmp_path):
    from duet.core.agreement import fingerprint
    (tmp_path / ".duet" / "memory").mkdir(parents=True)
    (tmp_path / ".duet" / "memory" / "architect.md").write_text("x")
    (tmp_path / "a.txt").write_text("y")
    values, _ = fingerprint(tmp_path)
    assert "a.txt" in values and not any(k.startswith(".duet/memory") for k in values)
