"""무인 벤치마크 실행(--bench): 가짜 에이전트로 끝까지 진행, 채점용 커밋 정리, 지표 JSON."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from duet import bench
from duet.core.config import Config, Role
from duet.core.events import Event

DUET_DIR = Path(bench.__file__).resolve().parent

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX 경로·셸 기준 시험")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


def make_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "main")
    (path / ".gitignore").write_text("node_modules/\n")
    (path / "README.md").write_text("base\n")
    (path / "docs").mkdir()
    (path / "docs" / "design.md").write_text("project design (base)\n")
    git(path, "add", "-A")
    git(path, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "base")
    return git(path, "rev-parse", "HEAD")


def test_finalize_keeps_task_changes_and_strips_duet_records(tmp_path):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    # duet 이 남기는 것
    (repo / ".gitignore").write_text("node_modules/\n\n# duet\n/duet/\n.duet/venv/\n.duet/logs/\n")
    (repo / "DIALOGUE.md").write_text("# 대화\n")
    (repo / ".duet").mkdir()
    (repo / ".duet" / "roles.yaml").write_text("roles: {}\n")
    (repo / "docs" / "design.md").write_text("changed by architect\n")
    (repo / "docs" / "work").mkdir()
    (repo / "docs" / "work" / "api.md").write_text("work log\n")
    (repo / "duet").mkdir()
    (repo / "duet" / "x.py").write_text("print()\n")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "duet snapshot")
    # 과제 결과 (일부는 커밋하지 않은 채로 끝남)
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def f():\n    return 1\n")
    (repo / "docs" / "usage.md").write_text("task doc\n")

    out = bench.finalize(repo, base, "duet")
    files = set(out["files"])
    assert files == {"src/app.py", "docs/usage.md"}, out
    assert git(repo, "show", "HEAD:.gitignore") == "node_modules/"
    assert git(repo, "show", "HEAD:docs/design.md") == "project design (base)"
    assert out["head"] == git(repo, "rev-parse", "HEAD") and out["head"] != base
    # 다시 불러도 추가 커밋 없이 같은 결과
    again = bench.finalize(repo, base, "duet")
    assert again["head"] == out["head"]


def test_finalize_keeps_task_edit_to_gitignore(tmp_path):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    (repo / ".gitignore").write_text("node_modules/\ndist/\n\n# duet\n/duet/\n.duet/venv/\n")
    out = bench.finalize(repo, base, "duet")
    assert out["files"] == [".gitignore"]
    assert git(repo, "show", "HEAD:.gitignore").splitlines() == ["node_modules/", "dist/"]


def test_finalize_without_changes_makes_no_commit(tmp_path):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    (repo / "DIALOGUE.md").write_text("only duet\n")
    out = bench.finalize(repo, base, "duet")
    assert out["head"] == base and out["files"] == []


def test_metrics_and_main_done():
    m = bench.Metrics()
    m.main = "architect"
    m.on_event(Event("usage", "implementer#api", {"cost_usd": 0.5, "tokens": 100}))
    m.on_event(Event("approval", "implementer", {"tier": "auto", "by": "auto", "allow": True}))
    m.on_event(Event("approval", "implementer", {"tier": "human", "by": "autopilot", "allow": False}))
    m.on_event(Event("turn_end", "architect", {"directives": [["STATUS", "done"]]}))
    assert m.cost_usd == 0.5 and m.tokens_by_role == {"implementer": 100}
    assert m.denied == 1 and m.approvals == {"auto:auto": 1, "human:autopilot": 1}
    assert m.turns == {"architect": 1} and m.main_done()
    m.on_event(Event("turn_end", "architect", {"directives": [["ASK_HUMAN", "어떻게?"]]}))
    assert not m.main_done()


def test_apply_role_models(tmp_path):
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles = {"architect": Role("architect", "claude", "a"), "implementer": Role("implementer", "codex", "b")}
    done = bench.apply_role_models(cfg, ["implementer=claude/claude-opus-5-5", "architect=claude/"])
    assert cfg.roles["implementer"].cli == "claude" and cfg.roles["implementer"].model == "claude-opus-5-5"
    assert cfg.roles["architect"].model is None and len(done) == 2
    with pytest.raises(ValueError):
        bench.apply_role_models(cfg, ["nobody=claude/x"])


@pytest.mark.parametrize("parallel", ["1", "3"])
def test_bench_fake_end_to_end(tmp_path, parallel):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    shutil.copytree(DUET_DIR, repo / "duet",
                    ignore=shutil.ignore_patterns(".git", "tests", "docs", "__pycache__", ".pytest_cache"))
    task = tmp_path / "instruction.md"
    task.write_text("src/app.py 에 hello 를 구현하세요.\n")
    out = tmp_path / "result.json"
    env = dict(os.environ, DUET_NO_VENV="1", DUET_FAKE_DELAY="0")
    p = subprocess.run([sys.executable, "duet", "--bench", "--fake", "--bench-task", str(task),
                        "--bench-out", str(out), "--bench-parallel", parallel, "--bench-timeout", "120"],
                       cwd=repo, env=env, capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stdout[-3000:] + p.stderr[-3000:]
    res = json.loads(out.read_text())
    assert res["finished"] == "done", res
    assert res["max_parallel"] == int(parallel)
    assert res["turns_total"] > 0 and res["git"]["base"] == base
    files = res["git"]["files"]
    assert files and not any(f.startswith((".duet", "DIALOGUE", "duet/", "docs/")) or f == ".gitignore"
                             for f in files), files
    assert git(repo, "diff", "--name-only", base, "HEAD").splitlines() == files
    assert "[bench] 끝: done" in p.stdout
