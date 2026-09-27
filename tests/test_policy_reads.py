"""읽기 명령은 자동 허용, 쓰기·목록 밖 명령은 막지 않고 사람 확인으로."""
from pathlib import Path

import pytest

from duet.core.config import DEFAULT_POLICY, Role
from duet.core.policy import ARCHITECT, AUTO, HUMAN, ApprovalRequest, Policy, split_commands

ARCH = Role("architect", "claude", permissions="read_only")
IMPL = Role("implementer", "codex")


def classify(role, cmd, task=None):
    pol = Policy(Path("/tmp"), DEFAULT_POLICY, "architect")
    pol.task = task
    return pol.classify(ApprovalRequest(role.name, "command", cmd, command=cmd), role)[0]


@pytest.mark.parametrize("cmd", [
    'grep -n -i "playerdata\\|player data\\|carry" docs/a.md | head -30',
    "head -c 6000 .duet/memory/architect.md; echo ----; grep -n '^## \\[' DIALOGUE.md | tail -8",
    "npm test 2>&1 | tail -15",
    "cd \"/x/Tretogor\" && ls region | sed -E 's/r\\.(-?[0-9]+)/\\1/' | sort -n | awk 'NR==1{m=$1}'",
    'find . -name "*.js" | head',
    "git diff --stat | tail -3",
])
def test_reads_are_automatic_even_for_read_only_role(cmd):
    assert classify(ARCH, cmd) == AUTO


@pytest.mark.parametrize("cmd", [
    "node -e 'console.log(1)'", "cat a > b", "sed -i 's/a/b/' x", "find . -name x -delete",
    "awk '{system(\"rm x\")}' f", "mkdir tmp",
])
def test_read_only_role_other_commands_ask_human(cmd):
    assert classify(ARCH, cmd) == HUMAN


def test_writer_role_unchanged():
    assert classify(IMPL, "rm -rf build") == HUMAN
    assert classify(IMPL, "pip install x") == ARCHITECT
    assert classify(IMPL, "cat x | tee y") == ARCHITECT


def test_verify_allows_agreed_command_trimmed_and_reads():
    task = {"phase": "verify", "test_command": "npm test", "waiting": False, "role": "implementer"}
    for cmd in ("npm test", "npm test 2>&1 | tail -15", "grep -n x docs/a.md | head"):
        assert classify(ARCH, cmd, task) == AUTO, cmd
    for cmd in ("node scripts/x.js", "npm test > out.txt", "npm test; rm x", "bash -lc 'npm test | grep fail'"):
        assert classify(ARCH, cmd, task) == HUMAN, cmd


def test_split_respects_quotes():
    assert split_commands('grep "a|b" f | head') == ['grep "a|b" f', "head"]
