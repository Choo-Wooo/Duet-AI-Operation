"""읽기 전용 단계 도구 확장, 역할별 자동 허용 도구·MCP, 디자이너 기본값, 전권 자동(autopilot) 모드."""
import asyncio

from duet.core.config import Config, Role, default_roles, DEFAULT_MODES
from duet.core.dialogue import Dialogue, Turn
from duet.core.events import EventBus
from duet.core.orchestrator import Orchestrator
from duet.core.policy import ARCHITECT, AUTO, DENY, HUMAN, ApprovalRequest, Policy, read_only_decision
from duet.core.prompts import system_append
from duet.adapters.base import TurnResult
from duet.core.config import DEFAULT_POLICY


class RecUI:
    def __init__(self):
        self.asked = []

    async def ask_choice(self, title, body, options):
        self.asked.append(title)
        return options[-1][0]

    async def ask_approval(self, req, reason, opinion):
        self.asked.append(req.summary)
        from duet.core.policy import Decision
        return Decision(False, "사람 거부", by="human")


def make(tmp_path, mode="review"):
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles = {"architect": Role("architect", "claude", permissions="read_only"),
                 "implementer": Role("implementer", "codex"),
                 "designer": Role("designer", "claude", auto_tools=["mcp__playwright__*"])}
    cfg.settings["git_snapshots"] = False
    cfg.state.mode = mode
    cfg.save_roles()
    cfg.save_state()
    Dialogue(tmp_path).ensure()
    ui = RecUI()
    return Orchestrator(cfg, EventBus(), ui, fake=True), ui


def test_plan_phase_allows_reads_and_denies_writes(tmp_path):
    p = Policy(tmp_path, DEFAULT_POLICY, "architect")
    p.task = {"phase": "plan", "waiting": False, "test_command": ""}
    r = Role("designer", "claude", auto_tools=["mcp__playwright__*"])
    cmd = lambda c: p.classify(ApprovalRequest("designer", "command", c, command=c), r)[0]
    tool = lambda t: p.classify(ApprovalRequest("designer", "tool", t, tool=t), r)[0]
    assert cmd("ls -la src") == AUTO and cmd("git log -5 | head") == AUTO
    assert cmd("npm install") == DENY and cmd("cat a > b") == DENY
    assert tool("ToolSearch") == AUTO and tool("Agent") == AUTO and tool("LS") == AUTO
    assert tool("mcp__playwright__browser_take_screenshot") == AUTO  # 역할 자동 허용 도구
    assert tool("mcp__other__write") == DENY


def test_role_auto_tools_outside_plan(tmp_path):
    p = Policy(tmp_path, DEFAULT_POLICY, "architect")
    d = Role("designer", "claude", auto_tools=["mcp__playwright__*"])
    i = Role("implementer", "codex")
    req = ApprovalRequest("designer", "tool", "shot", tool="mcp__playwright__browser_navigate")
    assert p.classify(req, d)[0] == AUTO
    assert p.classify(ApprovalRequest("implementer", "tool", "s", tool="mcp__playwright__browser_navigate"), i)[0] == ARCHITECT


def test_read_only_decision():
    assert read_only_decision(ApprovalRequest("a", "command", "ls", command="ls -la")).allow
    assert read_only_decision(ApprovalRequest("a", "tool", "t", tool="ToolSearch")).allow
    assert not read_only_decision(ApprovalRequest("a", "command", "x", command="rm x")).allow


def test_designer_defaults_and_guide(tmp_path):
    _, roles = default_roles({"claude": "x", "codex": "y", "agy": "z"})
    d = roles["designer"]
    assert d.model == "claude-opus-5-5" and "mcp__playwright__*" in d.auto_tools and "playwright" in d.mcp
    assert roles["researcher"].cli == "agy" and roles["researcher"].model == "gemini-3.8-flash-medium"
    cfg = Config(tmp_path)
    cfg.roles = roles
    assert "스크린샷" in system_append(cfg, d) and "출처" in system_append(cfg, roles["researcher"])
    # yaml 왕복
    cfg.dir.mkdir()
    cfg.save_roles()
    cfg2 = Config(tmp_path)
    cfg2.load()
    assert cfg2.roles["designer"].mcp == d.mcp and cfg2.roles["designer"].auto_tools == d.auto_tools


def test_claude_options_carry_role_mcp(tmp_path, monkeypatch):
    from duet.adapters.claude import ClaudeAdapter
    monkeypatch.setattr("duet.adapters.claude.which", lambda _: None)
    _, roles = default_roles({"claude": "x"})
    ad = ClaudeAdapter(roles["designer"], tmp_path, EventBus(), None, None, "")
    assert "playwright" in ad._options(None).mcp_servers


def test_autopilot_mode_config():
    m = DEFAULT_MODES["autopilot"]
    assert m.autonomy == "full" and m.max_turns is None and m.agreement


def test_autopilot_approvals(tmp_path):
    orch, ui = make(tmp_path, "autopilot")
    assert orch.full_auto

    async def go():
        rm = await orch.handle_approval(ApprovalRequest("implementer", "command", "rm -rf build", command="rm -rf build"))
        curl = await orch.handle_approval(ApprovalRequest("implementer", "command", "curl x", command="curl https://x"))
        push = await orch.handle_approval(ApprovalRequest("implementer", "command", "git push", command="git push origin main"))
        sudo = await orch.handle_approval(ApprovalRequest("implementer", "command", "sudo", command="sudo rm x"))
        tool = await orch.handle_approval(ApprovalRequest("implementer", "tool", "x", tool="mcp__unknown"))
        return rm, curl, push, sudo, tool
    rm, curl, push, sudo, tool = asyncio.run(go())
    assert rm.allow and curl.allow and tool.allow and rm.by == "auto"
    assert not push.allow and not sudo.allow
    assert ui.asked == []  # 사람에게 묻지 않음


def test_normal_mode_still_asks(tmp_path):
    orch, ui = make(tmp_path, "review")

    async def go():
        return await orch.handle_approval(ApprovalRequest("architect", "command", "rm -rf b", command="rm -rf b"))
    d = asyncio.run(go())
    assert not d.allow and ui.asked


def test_autopilot_choices(tmp_path):
    orch, ui = make(tmp_path, "autopilot")

    async def go():
        a = await orch.ui.ask_choice("턴 한도에 도달했습니다", "", [("+10", "10턴 더"), ("+30", "30"), ("stop", "멈추기")])
        b = await orch.ui.ask_choice("흐름이 막힌 것 같습니다", "", [("continue", "계속"), ("summarize", "정리"), ("stop", "멈춤")])
        c = await orch.ui.ask_choice("예산 한도", "", [("continue", "해제"), ("stop", "멈추기")])
        reps = [await orch.ui.ask_choice("[x] 계획 합의 라운드 한도", "", [("more", "더"), ("wait", "보류")]) for _ in range(7)]
        return a, b, c, reps
    a, b, c, reps = asyncio.run(go())
    assert (a, b, c) == ("+30", "summarize", "stop")
    assert reps[:5] == ["more"] * 5 and reps[5:] == ["wait", "wait"]
    assert ui.asked == []


def test_autopilot_routes_worker_question_to_main(tmp_path):
    orch, _ = make(tmp_path, "autopilot")
    body = "모르겠음\n<!-- duet: ASK_HUMAN 색상은 파랑? 초록? -->"

    async def go():
        return await orch._decide("designer", "delegate", TurnResult(body, full_text=body), Turn("designer", 3, "", 0, 0, body))
    nxt = asyncio.run(go())
    assert nxt[0] == "architect" and "대신 결정" in nxt[2] and "색상은" in nxt[2]


def test_resume_work_with_answer(tmp_path):
    orch, _ = make(tmp_path)
    from duet.core.work import WorkItem
    it = WorkItem(id="ui", role="designer", task="t", status="waiting:implement", wait_reason="질문")
    orch.work.items["ui"] = it
    orch.work.schedule = lambda: None
    orch.work.handle_directives([("RESUME_WORK", "ui 파랑으로 가세요")])
    assert it.status == "queued" and it.inbox == ["설계자: 파랑으로 가세요"]


def test_presets_and_existing_project_fill(tmp_path):
    from duet.core.config import ensure_preset_roles, preset_role, presets_info
    cfg = Config(tmp_path)
    cfg.dir.mkdir()
    cfg.roles = {"architect": Role("architect", "claude", permissions="read_only"),
                 "designer": Role("designer", "claude", "claude-opus-5-5", "내 설명")}
    cfg.save_roles()
    cfg.load()
    msgs = ensure_preset_roles(cfg, {"claude": 1, "agy": 1})
    assert cfg.roles["designer"].brief == "내 설명" and "playwright" in cfg.roles["designer"].mcp
    assert cfg.roles["researcher"].cli == "agy" and len(msgs) == 2
    # 사람이 지운 역할은 다시 넣지 않는다
    del cfg.roles["researcher"]
    cfg.save_roles()
    cfg2 = Config(tmp_path)
    cfg2.load()
    assert ensure_preset_roles(cfg2, {"claude": 1, "agy": 1}) == [] and "researcher" not in cfg2.roles
    # CLI 가 없으면 다음 선호 CLI 로
    assert preset_role("researcher", {"claude": 1}).cli == "claude"
    assert preset_role("tester", {}) is None
    info = {p["key"]: p for p in presets_info({"codex": 1})}
    assert info["tester"]["available"] and info["tester"]["cli"] == "codex" and info["tester"]["effort"] == "medium"
    assert info["designer"]["cli"] == "codex" and info["designer"]["available"]
