import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from duet.console import ConsoleUI
import yaml
from duet.core.config import DEFAULT_POLICY, Role
from duet.core.policy import ARCHITECT, AUTO, DENY, HUMAN, ApprovalRequest, Policy, writes_output


ROLES = [Role("architect", "claude", permissions="read_only"), Role("implementer", "codex")]


def command(policy, text, role=ROLES[1]):
    req = ApprovalRequest(role.name, "command", text, command=text)
    return policy.classify(req, role)[0]


@pytest.fixture
def policy(tmp_path):
    return Policy(tmp_path, DEFAULT_POLICY, "architect")


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("cmd", [
    "cat f & python3 evil.py", "ls $(x)", "ls `x`", "ls $(python3 evil.py)",
    "bash -c 'ls'; evil; echo 'x'", "bash -c 'ls'; python3 evil.py; echo 'x'",
    "sed -Ei s/a/b/ f", "sort -o f g", "git diff --output=x", "echo \\' > out \\'",
    "ls <(x)", "ls >(x)", "cat <<EOF", "ls \\x", "ls 'x", 'ls "x',
    "bash -c 'ls' extra", "sh -lc 'ls' extra", "zsh -c 'ls' extra",
    'ls "$(x)"', 'ls "`x`"',
])
def test_shell_bypasses(policy, role, cmd):
    assert command(policy, cmd, role) == HUMAN


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("cmd", [
    "ls -la", "cat a | grep b", "git status && git diff", "bash -c 'ls -la'", "rg foo",
    "cat f 2>&1 | tail", "cat f >&2", "cat f &>/dev/null", "cat f >/dev/null",
    "echo '>'", "echo '$(x)'", "grep 'a|b' f", "sed -n '1,3p' f",
    "sed -E 's/a/b/' f", "awk 'NR==1{m=$1}'", "sort -n f",
    "sh -lc 'ls -la'", "zsh -c 'rg foo'", "git branch", "git grep foo",
])
def test_normal_reads_and_fd_redirects(policy, role, cmd):
    assert command(policy, cmd, role) == AUTO


@pytest.mark.parametrize("cmd", ["cat f &> out.txt", "cat f > out.txt", "cat f >>out.txt"])
def test_file_redirects(policy, cmd):
    assert writes_output(cmd)
    assert command(policy, cmd, ROLES[0]) == HUMAN
    assert command(policy, cmd) == ARCHITECT


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("cmd", [
    "GIT_EXTERNAL_DIFF=./evil git diff", "GIT_PAGER=./evil git log",
    "PAGER=./evil git show", "LESSOPEN='|./evil %s' less f",
    "cat=./evil git diff", "PATH=/tmp/evil ls", "X=1 Y=2 git status",
    "/tmp/x/cat f", "./ls", "/tmp/x/bash -c 'ls'",
    "/usr/bin/../tmp/cat f", "/bin//cat f",
    "rg --pre ./evil x", "rg --pre=./evil x", "sort --compress-program=./evil f",
    "find . -fls out", "git log --output=x", "git show --output=x", "tree -o out",
    "git log --output x", "git show --output x", "tree -oout",
    "xxd -r a b", "xxd a b", "uniq a b", "git branch foo",
    "git branch -M foo", "git branch -c foo", "git branch -C foo", "git branch -f foo",
    "git branch --delete foo", "git branch --move foo", "git branch --copy foo",
    "git branch --force foo", "git branch --set-upstream-to=origin/main foo",
    "git branch -u origin/main", "git branch --edit-description",
    "less f", "more f", "less +command f", "less -o out f", "less --log-file=out f",
])
def test_rework_read_bypasses(policy, role, cmd):
    assert command(policy, cmd, role) != AUTO


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("cmd", [
    "git branch", "git branch -a", "git branch -r", "git branch -v", "git branch -vv",
    "git branch --list", "git branch -l", "git branch --show-current",
    "git branch --contains main", "git branch --merged main", "git branch --list 'feat*'",
    "rg foo", "sort f", "/usr/bin/grep x f", "/bin/cat f", "/usr/local/bin/rg foo",
    "/opt/homebrew/bin/rg foo", "xxd a", "xxd -l 8 a", "uniq a", "uniq -f 1 a",
])
def test_rework_normal_reads(policy, role, cmd):
    assert command(policy, cmd, role) == AUTO


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("cmd", [
    "sed -i s/a/b/ f", "sed --in-place s/a/b/ f", "sed -Ei s/a/b/ f",
    "sed 'w out' f", "sed 'W out' f", "sed 'e id' f", "sed -n 'w out' f",
    "sed 's/a/b/w out' f", "sed 's/a/b/e' f", "sed -n -e 'w out' f",
    "sed -n ' /foo/w out' f", "sed -n -e'w out' f", "sed --expression='w out' f",
    "sed -ne'w out' f",
    "awk '{print > \"f\"}'", "awk '{print | \"sh\"}'", "awk '{system(\"id\")}'",
    "sort -o f g", "sort -of g", "git diff --output=x", "git diff --output x",
    "git grep --open-files-in-pager=sh x", "git grep --open-files-in-pager x", "git grep -Osh x",
    "git branch -D x", "git branch -d x", "git branch -m x y",
])
def test_write_options_cannot_fall_through_auto_patterns(policy, role, cmd):
    assert command(policy, cmd, role) == HUMAN


@pytest.mark.parametrize("first,second", [
    ("rm -rf build", "rm -rf ~"), ("git push origin feat", "git push --force origin main"),
])
def test_dangerous_session_cache(policy, first, second):
    policy.remember(ApprovalRequest("implementer", "command", first, command=first))
    assert command(policy, first) == HUMAN
    assert command(policy, second) == HUMAN


def test_full_command_cache(policy):
    req = ApprovalRequest("implementer", "command", "custom", command="custom run build")
    policy.remember(req)
    assert command(policy, "custom   run build") == AUTO
    assert command(policy, "custom run home") == ARCHITECT


@pytest.mark.parametrize("source", ["default", "project"])
@pytest.mark.parametrize("path", [
    ".DUET/policy.yaml", ".claude/settings.local.json", "key.pem", ".env.local",
    ".agents/config", "key.key", ".env", "sub/key.pem", "sub/key.key", "sub/.env",
    "sub/.env.local", "secrets/a", "sub/secrets/a", ".GIT/config", "x/../key.PEM",
])
def test_protected_paths_and_cache(tmp_path, source, path):
    conf = DEFAULT_POLICY if source == "default" else yaml.safe_load(
        (Path(__file__).resolve().parents[2] / ".duet/policy.yaml").read_text())
    pol = Policy(tmp_path, conf, "architect")
    req = ApprovalRequest("implementer", "file", path, paths=[path])
    pol.remember(req)
    assert pol.classify(req, ROLES[1])[0] == HUMAN


def test_resolved_paths_and_readonly_writable(policy, tmp_path):
    (tmp_path / "secrets").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "secrets", target_is_directory=True)
    (tmp_path / "docs").mkdir()
    (tmp_path / "notes").symlink_to(tmp_path / "docs", target_is_directory=True)
    for path, role, tier in [("alias/key.txt", ROLES[1], HUMAN), ("notes/a.md", ROLES[0], AUTO),
                             ("DOCS/a.md", ROLES[0], AUTO), ("docs/../code.py", ROLES[0], DENY),
                             ("../outside", ROLES[1], HUMAN), ("duet/core/policy.py", ROLES[1], AUTO)]:
        assert policy.classify(ApprovalRequest(role.name, "file", path, paths=[path]), role)[0] == tier


@pytest.mark.parametrize("suffix", [
    "", " | head -10", " | tail -20", " | grep fail", " | rg fail", " | wc -l", " | cat",
    " | sed -n '1,10p'", " 2>&1 | tail -20", " | grep fail | head -10",
])
def test_verify_safe_filters(policy, suffix):
    test = "cd /project && custom test"
    policy.task = {"phase": "verify", "test_command": test, "waiting": False}
    assert command(policy, test + suffix, ROLES[0]) == AUTO


@pytest.mark.parametrize("suffix", [
    " -x", " | sed -i s/a/b/ f", " | sed -n 'w out'", " | tee f", " | tail; cat f",
    " | tail && cat f", " | tail &", " | tail $(x)", " | tail > out", " > out",
    "; touch x", " && touch x", " | tail || cat", " | tail |", " | tail\ncat f",
    "2>&1 | tail", " | head-evil",
    " | rg --pre=./evil x",
])
def test_verify_rejects_extensions(policy, suffix):
    test = "custom test"
    policy.task = {"phase": "verify", "test_command": test, "waiting": False}
    req = ApprovalRequest("architect", "command", "test", command=test + suffix)
    policy.session_allow.add(req.cache_key())
    assert not policy.verification_command(req)
    assert policy.classify(req, ROLES[0])[0] == HUMAN


@pytest.mark.parametrize("cmd", ["bash -lc 'custom test'", "cd /tmp && custom test", "X=1 custom test"])
def test_verify_does_not_strip_prefixes(policy, cmd):
    policy.task = {"phase": "verify", "test_command": "custom test", "waiting": False}
    assert command(policy, cmd, ROLES[0]) == HUMAN


@pytest.mark.parametrize("answer", ["abort", "yes please", "allow anything", "session extra"])
def test_console_rejects_prefixes(answer):
    ui = ConsoleUI()
    ui._ask = AsyncMock(return_value=answer)
    decision = asyncio.run(ui.ask_approval(ApprovalRequest("implementer", "command", "test"), "test", None))
    assert not decision.allow


@pytest.mark.parametrize("answer,scope", [("y", "once"), ("yes", "once"), ("allow", "once"),
                                         ("a", "session"), ("s", "session"), ("session", "session"),
                                         (" ALLOW ", "once")])
def test_console_exact_tokens(answer, scope):
    ui = ConsoleUI()
    ui._ask = AsyncMock(return_value=answer)
    result = asyncio.run(ui.ask_approval(ApprovalRequest("implementer", "command", "test"), "test", None))
    assert result.allow and result.scope == scope


def test_tui_approval_guard(monkeypatch):
    from textual.app import App
    from textual.widgets import Button
    from duet.tui import screens

    now = [100.0]
    monkeypatch.setattr(screens, "monotonic", lambda: now[0])

    async def scenario():
        app = App()
        results = []
        async with app.run_test() as pilot:
            screen = screens.ApprovalScreen(ApprovalRequest("implementer", "command", "test"), "test", None)
            app.push_screen(screen, results.append)
            await pilot.pause()
            assert screen.focused.id == "deny"
            await pilot.press("y", "a", "enter", "tab")
            assert not results
            assert screen.focused.id == "deny"
            now[0] = 100.499
            await pilot.press("y")
            assert not results
            now[0] = 100.5
            await pilot.press("a")
            assert not results
            await pilot.press("enter")
            assert len(results) == 1 and not results[0].allow
            screen = screens.ApprovalScreen(ApprovalRequest("implementer", "command", "test"), "test", None)
            app.push_screen(screen, results.append)
            await pilot.pause()
            now[0] += 0.5
            screen.query_one("#allow_session", Button).focus()
            await pilot.press("enter")
            assert results[-1].allow and results[-1].scope == "session"

    asyncio.run(scenario())
