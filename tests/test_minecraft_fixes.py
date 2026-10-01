"""마인크래프트 장기 실행에서 드러난 문제 회귀 시험.

1. 같은 턴의 ACCEPT + duet-work 블록: 순차 작업을 마친 뒤 블록을 처리한다.
2. duet-work 의 값에 ': ' 가 섞여도 해석하고, 고칠 수 없으면 줄 번호와 함께 알린다.
3. 작업자가 워크트리에서 고친 docs/work/<id>.md 는 병합 전에 되돌린다 (메인 트리 기록과 충돌 방지).
4. Claude SDK 메시지 버퍼를 1MB 보다 크게 잡는다.
5. status 이벤트는 바뀐 경우에만, 일정 간격으로 로그에 남긴다.
"""
import asyncio
import json
import subprocess

import pytest

from duet.adapters.claude import MAX_BUFFER, ClaudeAdapter
from duet.core import events as events_mod
from duet.core.config import Role
from duet.core.events import EventBus
from duet.core.work import parse_work_block
from duet.core.worktrees import Worktrees

from test_agreement import advance, agreed, orch  # noqa: F401  (픽스처 재사용)

WORK = "```duet-work\n- id: next-a\n  role: implementer\n  task: 다음 단계: 문 열기\n```"


def test_accept_then_work_block_same_turn(orch):  # noqa: F811
    seen = []
    orch.work.submit = lambda specs, n: seen.append(specs) or []

    async def scenario():
        await agreed(orch)
        await advance(orch, "implementer", "<!-- duet: REPORT done -->", "implement")
        nxt = await advance(orch, "architect", "검증 통과.\n" + WORK + "\n<!-- duet: ACCEPT -->", "verify")
        assert nxt is None
        assert orch.cfg.state.task is None  # 순차 작업은 완료
        assert seen and seen[0][0]["id"] == "next-a" and seen[0][0]["task"] == "다음 단계: 문 열기"

    asyncio.run(scenario())


def test_flow_directive_without_task_still_takes_work_block(orch):  # noqa: F811
    seen = []
    orch.work.submit = lambda specs, n: seen.append(specs) or []

    async def scenario():
        nxt = await advance(orch, "architect", WORK + "\n<!-- duet: ACCEPT -->")
        assert nxt is None and seen

    asyncio.run(scenario())


def test_flow_directive_without_task_and_no_block_is_rejected(orch):  # noqa: F811
    async def scenario():
        nxt = await advance(orch, "architect", "<!-- duet: ACCEPT -->")
        assert nxt and "활성 합의 task가 없습니다" in nxt[2]

    asyncio.run(scenario())


def test_work_block_colon_values_are_quoted():
    text = ("```duet-work\n- id: a\n  role: codex\n  task: 고치기: 문을 연다 'x'\n  files: [a.js]\n"
            "- id: b\n  role: codex\n  task: \"이미: 따옴표\"\n  depends_on: [a]\n```")
    specs = parse_work_block(text)
    assert specs[0]["task"] == "고치기: 문을 연다 'x'" and specs[0]["files"] == ["a.js"]
    assert specs[1]["task"] == "이미: 따옴표" and specs[1]["depends_on"] == ["a"]


def test_work_block_unfixable_error_has_line():
    with pytest.raises(ValueError, match=r"YAML 오류 \(\d+행\)"):
        parse_work_block("```duet-work\n- id: a\n  task: [열린 목록\n```")


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def test_revert_work_logs(tmp_path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "me@example.com")
    _git(tmp_path, "config", "user.name", "Me")
    (tmp_path / "docs" / "work" / "carry").mkdir(parents=True)
    (tmp_path / "docs" / "work" / "feat.md").write_text("기록\n")
    (tmp_path / "docs" / "work" / "carry" / "a.txt").write_text("산출물\n")
    (tmp_path / ".gitignore").write_text(".duet/\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    wt = Worktrees(tmp_path)
    path = wt.create("feat", "main")
    (path / "docs" / "work" / "feat.md").write_text("작업자가 덮어씀\n")
    (path / "docs" / "work" / "other.md").write_text("새 기록\n")
    (path / "docs" / "work" / "carry" / "a.txt").write_text("옮김\n")
    (path / "code.txt").write_text("구현\n")
    reverted = wt.revert_work_logs(path, "main")
    assert sorted(reverted) == ["docs/work/feat.md", "docs/work/other.md"]
    assert (path / "docs" / "work" / "feat.md").read_text() == "기록\n"
    assert not (path / "docs" / "work" / "other.md").exists()
    assert (path / "docs" / "work" / "carry" / "a.txt").read_text() == "옮김\n"  # 하위 폴더 산출물은 유지
    wt.commit_all(path, "work")
    changed = _git(path, "diff", "--name-only", "main", "HEAD").split()
    assert sorted(changed) == ["code.txt", "docs/work/carry/a.txt"]


def test_claude_buffer_raised(tmp_path):
    ad = ClaudeAdapter(Role("architect", "claude"), tmp_path, EventBus(), None, None, "")
    opts = ad._options(None)
    assert opts.max_buffer_size == MAX_BUFFER and MAX_BUFFER > 1024 * 1024


def test_status_log_throttled(tmp_path, monkeypatch):
    bus = EventBus(tmp_path)
    bus.emit("status", None, a=1)
    bus.emit("status", None, a=1)          # 같은 내용
    bus.emit("status", None, a=2)          # 간격 미달
    bus.emit("notice", None, text="x")     # 다른 이벤트는 항상
    bus._status_ts -= events_mod.STATUS_LOG_INTERVAL + 1  # 간격이 지난 것으로
    bus.emit("status", None, a=3)
    bus.close()
    rows = [json.loads(x) for f in tmp_path.glob("*.jsonl") for x in f.read_text().splitlines()]
    assert [(r["event"], r["data"].get("a")) for r in rows] == [("status", 1), ("notice", None), ("status", 3)]


# ---------------------------------------------------------------- 하위 프로세스가 고아로 남지 않는지
import os  # noqa: E402
import signal  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402

from duet.core import procs  # noqa: E402

posix = pytest.mark.skipif(sys.platform == "win32", reason="POSIX 프로세스 그룹")


def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # 좀비(부모가 아직 거두지 않음)는 죽은 것으로 본다
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().split(")")[-1].split()[0] != "Z"
    except OSError:
        return True


def _wait_dead(pid, limit=5.0):
    end = time.time() + limit
    while time.time() < end and _alive(pid):
        time.sleep(0.05)
    return not _alive(pid)


BG = "sleep 300 >/dev/null 2>&1 & echo $! > {f}"


@posix
def test_reap_sweeps_background_children_after_leader_exits(tmp_path):
    pidf = tmp_path / "bg.pid"

    async def go():
        proc = await asyncio.create_subprocess_exec("sh", "-c", BG.format(f=pidf), **procs.group_kwargs())
        procs.track(proc)
        await proc.wait()
        bg = int(pidf.read_text())
        assert _alive(bg)  # 리더는 끝났지만 자손은 살아 있다
        await procs.reap(proc)
        return bg
    bg = asyncio.run(go())
    assert _wait_dead(bg) and bg not in procs._GROUPS


@posix
def test_kill_all_groups_is_exit_safety_net(tmp_path):
    pidf = tmp_path / "bg.pid"

    async def go():
        proc = await procs.create_shell(BG.format(f=pidf) + "; sleep 300")
        for _ in range(100):
            if pidf.exists() and pidf.read_text().strip():
                break
            await asyncio.sleep(0.02)
        assert proc.pid in procs._GROUPS
        procs.kill_all_groups()
        await proc.wait()
        procs.close_transport(proc)
        return int(pidf.read_text())
    bg = asyncio.run(go())
    assert _wait_dead(bg) and not procs._GROUPS


@posix
def test_work_test_command_background_server_is_cleaned(orch, tmp_path, monkeypatch):  # noqa: F811
    from duet.core import work as work_mod
    from duet.core.work import WorkItem
    monkeypatch.setattr(work_mod.Policy, "classify", lambda self, req, role: (work_mod.AUTO, "시험"))
    pidf = tmp_path / "srv.pid"
    orch.cfg.settings["work_test_timeout"] = 30
    it = WorkItem(id="t", role="implementer", task="x")

    async def go():
        return await orch.work._run_test_active(it, tmp_path, BG.format(f=pidf) + "; true")
    code, _ = asyncio.run(go())
    assert code == 0
    assert _wait_dead(int(pidf.read_text()))


@posix
def test_sighup_handler_respects_nohup(monkeypatch):
    from duet import app
    monkeypatch.setattr("atexit.register", lambda f: None)
    old = signal.getsignal(signal.SIGHUP)
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        app._install_exit_cleanup()
        assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        app._install_exit_cleanup()
        h = signal.getsignal(signal.SIGHUP)
        assert callable(h) and h not in (signal.SIG_DFL, signal.SIG_IGN)
        with pytest.raises(KeyboardInterrupt):
            h(signal.SIGHUP, None)
    finally:
        signal.signal(signal.SIGHUP, old)
