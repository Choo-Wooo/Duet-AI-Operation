"""모든 상태/대화 쓰기는 pytest tmp_path 안에서만 수행한다."""
import asyncio
import json
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

import pytest

from duet import commands
from duet.adapters.base import AgentAdapter, TurnResult
from duet.app import main, parse_args
from duet.core.config import Config, Role
from duet.core.dialogue import Dialogue
from duet.core.events import EventBus
from duet.core.orchestrator import Orchestrator


class NoUI:
    async def ask_choice(self, *args):
        raise AssertionError("Unexpected UI question")

    async def ask_approval(self, *args):
        raise AssertionError("Unexpected approval")


class RecordingAdapter(AgentAdapter):
    async def start(self):
        self.fork_requested = self.fork_session
        self.source = self.session_id
        if not self.session_id or self.fork_session:
            self.session_id = uuid4().hex
        self.fork_session = False
        self.closed = False
        self.prompts = []

    async def run_turn(self, prompt):
        self.prompts.append(prompt)
        return TurnResult("테스트 턴 완료")

    async def interrupt(self):
        pass

    async def close(self):
        self.closed = True


@pytest.fixture
def orch(tmp_path, monkeypatch):
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles = {"architect": Role("architect", "claude"), "implementer": Role("implementer", "codex")}
    cfg.settings["git_snapshots"] = False
    cfg.save_roles()
    cfg.state.sessions = {"architect": "arch-original", "implementer": "impl-original"}
    cfg.state.reviewer_sessions = {"architect": "review-original"}
    cfg.state.seen = {"architect": 1, "implementer": 1}
    cfg.state.last_n = 1
    cfg.state.turns_since_checkpoint = 1
    cfg.save_state()
    dlg = Dialogue(tmp_path)
    dlg.ensure()
    dlg.append_turn("human", 1, "저장할 대화")
    obj = Orchestrator(cfg, EventBus(), NoUI(), fake=True)
    obj.events = []
    obj.bus.subscribe(obj.events.append)

    def factory(role, project, bus, approver, session_id, system_append,
                reviewer=False, fake=False, fork_session=False):
        assert fake
        return RecordingAdapter(role, project, bus, approver, session_id, system_append,
                                reviewer, fork_session)

    monkeypatch.setattr("duet.core.orchestrator.make_adapter", factory)
    return obj


def test_save_creates_four_files_and_metadata(orch):
    archive = orch.dialogue.archive_dir
    archive.mkdir()
    (archive / "old.md").write_text("archive")
    meta = orch.save("첫저장", note="메모")
    dest = orch.cfg.saves_dir / "첫저장"
    assert {p.name for p in dest.iterdir()} == {"meta.json", "state.json", "DIALOGUE.md", "roles.yaml"}
    assert json.loads((dest / "meta.json").read_text()) == meta
    assert meta["name"] == "첫저장" and meta["last_n"] == 1 and meta["mode"] == "review"
    assert meta["note"] == "메모" and meta["created_at"] and meta["duet_version"]
    assert meta["roles"]["architect"]["cli"] == "claude"
    assert meta["archives"] == ["DIALOGUE-archive/old.md"]
    assert (dest / "DIALOGUE.md").read_bytes() == orch.dialogue.path.read_bytes()
    assert any(e.kind == "save" and e.data == {"name": "첫저장", "last_n": 1} for e in orch.events)


def test_save_turns_load_restores_state_and_autosaves(orch):
    async def scenario():
        initial_ad = await orch.adapter("architect")
        initial_reviewer = await orch.get_reviewer()
        orch.save("before")
        original_dialogue = orch.dialogue.path.read_bytes()
        original_state = json.loads((orch.cfg.saves_dir / "before/state.json").read_text())
        await orch._turn("architect", "human", "1")
        await orch._turn("implementer", "human", "1")
        branch_ad = orch.adapters["architect"]
        branch_reviewer = await orch.get_reviewer()
        recent_dialogue = orch.dialogue.path.read_bytes()
        assert recent_dialogue != original_dialogue
        await orch.load_save("before")
        assert orch.dialogue.path.read_bytes() == original_dialogue
        expected = {**original_state, "auto": False,
                    "fork_on_resume": list(original_state["sessions"]),
                    "reviewer_fork_on_resume": list(original_state["reviewer_sessions"])}
        assert asdict(orch.cfg.state) == expected
        assert json.loads(orch.cfg.state_file.read_text()) == expected
        assert initial_ad.closed and initial_reviewer.closed and branch_ad.closed and branch_reviewer.closed
        assert not orch.adapters and orch.reviewer is None
        auto = next(m for m in orch.list_saves() if m["autosave"])
        assert (orch.cfg.saves_dir / auto["name"] / "DIALOGUE.md").read_bytes() == recent_dialogue
        assert any(e.kind == "load" and e.data == {"name": "before", "last_n": 1} for e in orch.events)
        assert not orch.cfg.state.auto and orch.not_paused.is_set()

    asyncio.run(scenario())


def test_invalid_names_duplicates_and_force(orch):
    for name in ("../x", "", ".", "..", "x/y", "x\\y", "/tmp/x", "a" * 65):
        with pytest.raises(ValueError):
            orch.save(name)
    orch.save("valid")
    with pytest.raises(ValueError, match="이미"):
        orch.save("valid")
    orch.save("valid", note="교체", force=True)
    assert orch.list_saves()[0]["note"] == "교체"


def test_load_refused_while_running_including_paused_loop(orch):
    orch.save("idle")
    before = orch.dialogue.read()
    orch.running = True
    for paused in (False, True):
        if paused:
            orch.pause()
        with pytest.raises(ValueError, match="진행 중"):
            asyncio.run(orch.load_save("idle"))
    assert orch.dialogue.read() == before
    assert len(orch.list_saves()) == 1


def test_removed_and_changed_cli_sessions_discarded_with_warning(orch):
    orch.save("roles")
    del orch.cfg.roles["implementer"]
    orch.cfg.roles["architect"].cli = "codex"
    orch.cfg.save_roles()
    roles_before = orch.cfg.roles_file.read_bytes()
    asyncio.run(orch.load_save("roles"))
    assert not orch.cfg.state.sessions
    assert not orch.cfg.state.reviewer_sessions
    assert not orch.cfg.state.seen
    assert orch.cfg.roles_file.read_bytes() == roles_before
    warnings = [e.data["text"] for e in orch.events if e.kind == "notice" and e.data.get("level") == "warn"]
    assert any("implementer" in w for w in warnings)
    assert any("architect" in w for w in warnings)


def test_fork_passed_once_per_load_and_save_protects_original(orch):
    async def scenario():
        original = await orch.adapter("architect")
        orch.save("branch")
        branch = await orch.adapter("architect")
        assert original.closed and branch is not original
        assert branch.fork_requested and branch.source == "arch-original"
        assert branch.session_id != "arch-original"
        assert await orch.adapter("architect") is branch
        for _ in range(2):
            await orch.load_save("branch")
            assert "architect" in orch.cfg.state.fork_on_resume
            ad = await orch.adapter("architect")
            assert ad.fork_requested and ad.source == "arch-original"
            assert "architect" not in orch.cfg.state.fork_on_resume
            assert await orch.adapter("architect") is ad
            rev = await orch.get_reviewer()
            assert rev.fork_requested and rev.source == "review-original"
            assert "architect" not in orch.cfg.state.reviewer_fork_on_resume
            assert await orch.get_reviewer() is rev
        assert json.loads(orch.cfg.state_file.read_text())["sessions"]["architect"] == ad.session_id
        assert json.loads((orch.cfg.saves_dir / "branch/state.json").read_text())["sessions"]["architect"] == "arch-original"

    asyncio.run(scenario())


def test_cli_load_and_new_session_conflict():
    with pytest.raises(SystemExit) as exc:
        parse_args(["--load", "saved", "--new-session"])
    assert exc.value.code == 2


def test_autosave_retains_latest_five(orch):
    orch.save("base")
    orch.save("_autosave-manual")
    for n in range(8):
        orch.cfg.state.last_n = n + 10
        asyncio.run(orch.load_save("base"))
    autos = [m for m in orch.list_saves() if m["autosave"]]
    assert len(autos) == 5
    assert [m["last_n"] for m in autos] == [17, 16, 15, 14, 13]
    assert {m["name"] for m in orch.list_saves()} >= {"base", "_autosave-manual"}


def test_save_refused_during_turn_allowed_after_pause_boundary(orch):
    orch.running = True
    with pytest.raises(ValueError):
        orch.save("busy")
    orch.pause()
    orch._turn_active = True
    with pytest.raises(ValueError):
        orch.save("busy")
    orch._turn_active = False
    assert orch.save("paused")["name"] == "paused"


def test_commands_save_list_load_delete_and_help(orch):
    async def scenario():
        assert "저장됨" in await commands.handle(orch, "/save 명령 -- 메모 --force 그대로")
        assert orch.list_saves()[0]["note"] == "메모 --force 그대로"
        assert "이미" in await commands.handle(orch, "/save 명령")
        assert "저장됨" in await commands.handle(orch, "/save 명령 --force -- 바뀐 메모")
        assert "바뀐 메모" in await commands.handle(orch, "/saves")
        assert "불러옴" in await commands.handle(orch, "/load 명령")
        assert "삭제됨" in await commands.handle(orch, "/save-delete 명령")
        assert "찾지 못했습니다" in await commands.handle(orch, "/load 명령")
        assert "사용법" in await commands.handle(orch, "/load")
        assert "/save-delete" in await commands.handle(orch, "/help")
        assert "저장됨" in await commands.handle(orch, "/save")

    asyncio.run(scenario())


def test_corrupt_snapshot_does_not_close_adapters_or_change_state(orch):
    async def scenario():
        ad = await orch.adapter("architect")
        orch.save("corrupt")
        (orch.cfg.saves_dir / "corrupt/state.json").write_text('{"sessions": []}')
        state, dialogue = asdict(orch.cfg.state), orch.dialogue.read()
        with pytest.raises(ValueError, match="읽을 수 없습니다"):
            await orch.load_save("corrupt")
        assert not ad.closed
        assert asdict(orch.cfg.state) == state and orch.dialogue.read() == dialogue
        assert len(orch.list_saves()) == 1

    asyncio.run(scenario())


def test_cli_list_saves_read_only_without_project_initialization(tmp_path, monkeypatch, capsys):
    def forbidden(*args):
        raise AssertionError("list-saves must not initialize the project")

    monkeypatch.setattr("duet.app.setup_project", forbidden)
    assert main(Path("duet"), tmp_path, ["--list-saves"]) == 0
    assert "없습니다" in capsys.readouterr().out
    assert list(tmp_path.iterdir()) == []


def test_cli_load_restores_before_explicit_mode_overrides(orch, monkeypatch):
    orch.save("startup")
    orch.cfg.state.mode = "sprint"
    orch.cfg.save_state()
    observed = []

    async def console(cfg, bus, msgs, fake, first):
        observed.append(cfg.state.mode)
        assert not cfg.state.auto
        assert cfg.state.last_n == 1
        assert any("불러옴" in m for m in msgs)

    monkeypatch.setattr("duet.app.detect_clis", lambda: {})
    monkeypatch.setattr("duet.console.run_console", console)
    assert main(Path("duet"), orch.project, ["--fake", "--no-tui", "--load", "startup", "--mode", "deliberate"]) == 0
    assert observed == ["deliberate"]
    autos = [m for m in orch.list_saves() if m["autosave"]]
    assert autos[0]["mode"] == "sprint"


def test_load_blocks_other_mutations_and_queues_new_message_until_ready(orch, monkeypatch):
    async def scenario():
        orch.save("race")
        entered, release = asyncio.Event(), asyncio.Event()

        async def close():
            entered.set()
            await release.wait()

        monkeypatch.setattr(orch, "close", close)
        loading = asyncio.create_task(orch.load_save("race"))
        await entered.wait()
        assert "불러오는 중" in await commands.handle(orch, "/mode sprint")
        with pytest.raises(ValueError):
            await orch.load_save("race")
        with pytest.raises(ValueError):
            orch.save("concurrent")
        orch.submit("복원 뒤 메시지")
        server = asyncio.create_task(orch.serve())
        await asyncio.sleep(0)
        assert "복원 뒤 메시지" not in orch.dialogue.read()
        release.set()
        await loading
        await asyncio.sleep(0)
        assert "복원 뒤 메시지" in orch.dialogue.read()
        server.cancel()
        with pytest.raises(asyncio.CancelledError):
            await server

    asyncio.run(scenario())


def test_git_head_mismatch_warns_without_restoring_code(orch, monkeypatch):
    orch.save("git")
    monkeypatch.setattr("duet.core.orchestrator.git_head", lambda project: "different-head")
    code = orch.project / "source.py"
    code.write_text("unchanged")
    asyncio.run(orch.load_save("git"))
    assert code.read_text() == "unchanged"
    assert any("/rollback" in e.data.get("text", "") for e in orch.events)


def test_symlink_save_paths_rejected(orch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    orch.cfg.saves_dir.mkdir()
    (orch.cfg.saves_dir / "link").symlink_to(outside, target_is_directory=True)
    for call in (lambda: orch.save("link", force=True), lambda: orch.delete_save("link")):
        with pytest.raises(ValueError, match="심볼릭"):
            call()
    assert outside.exists()
