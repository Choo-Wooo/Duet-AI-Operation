import asyncio
import re
from unittest.mock import AsyncMock

import pytest

from duet import commands
from duet.adapters.base import TurnResult
from duet.core.config import Config
from duet.core.verification import VERIFICATION_LEVELS
from duet.core.work import WorkItem
from duet.tests.test_agreement import orch, agreed
from duet.tests.test_markdown import STATIC, node
from duet.tests.test_web import make_ui


def test_level_defaults_and_persists(orch):
    assert orch.cfg.settings["verification_level"] == "functional"
    orch.set_verification_level("balanced")
    cfg = Config(orch.project)
    cfg.load()
    assert cfg.settings["verification_level"] == "balanced"
    del cfg.settings["verification_level"]
    cfg.save_roles()
    restored = Config(orch.project)
    restored.load()
    assert restored.settings["verification_level"] == "functional"


@pytest.mark.parametrize("bad", ["off", "", None, ["balanced"]])
def test_invalid_saved_level_rejected(orch, bad):
    orch.cfg.settings["verification_level"] = bad
    orch.cfg.save_roles()
    with pytest.raises(ValueError, match="verification_level"):
        Config(orch.project).load()


def test_live_level_changes_reach_main_and_parallel_turns(orch):
    async def run():
        ad = await orch.adapter("architect")
        orch.usage.run = AsyncMock(return_value=TurnResult("done"))
        for level, label in VERIFICATION_LEVELS.items():
            orch.set_verification_level(level)
            await orch.run_agent_turn(ad, "메인 계획")
            prompt = orch.usage.run.call_args.args[1]
            assert f"현재 검증 수준: {label} ({level})" in prompt
            assert "메인 계획" in prompt
            if level == "functional":
                assert "별도 하네스" in prompt and "만들거나 새 의존성을 설치하지" in prompt
            item = WorkItem("api", "architect", "병렬 계획")
            await orch.work._turn(ad, item, "plan", "병렬 계획", "설계자")
            prompt = orch.usage.run.call_args.args[1]
            assert f"현재 검증 수준: {label} ({level})" in prompt
            assert "병렬 계획" in prompt
    asyncio.run(run())


def test_setting_does_not_change_agreed_command_or_gate(orch):
    async def run():
        await agreed(orch)
        old = dict(orch.cfg.state.task)
        response = await commands.handle(orch, "/verification functional")
        assert "다음 턴부터" in response
        assert orch.cfg.state.task == old
        assert "functional" in await commands.handle(orch, "/verification")
        assert "검증 수준:" in await commands.handle(orch, "/verification invalid")
        assert orch.cfg.settings["verification_level"] == "functional"
    asyncio.run(run())


def test_web_level_validation_and_persistence(tmp_path):
    async def run():
        ui = make_ui(tmp_path)
        assert ui.state()["settings"]["verification_level"] == "functional"
        for value in ("balanced", "thorough", "functional"):
            result = await ui.handle({"type": "setting", "key": "verification_level", "value": value})
            assert result["state"]["settings"]["verification_level"] == value
        bad = await ui.handle({"type": "setting", "key": "verification_level", "value": "off"})
        assert "허용 범위" in bad["text"]
        assert ui.cfg.settings["verification_level"] == "functional"
        await ui.close()
    asyncio.run(run())


def test_dropdown_sends_selected_level():
    html = (STATIC / "index.html").read_text()
    script = re.findall(r"<script>(.*?)</script>", html, re.S)[-1]
    start = script.index("function renderJevStatus(v)")  # renderSettings also renders the JEV status card
    end = script.index("function renderSaves()", start)
    node(r'''
const assert=require('assert'),vm=require('vm');
const sent=[],select={dataset:{k:'verification_level'},value:'thorough',type:'select-one'};
const els={};const $=s=>els[s]||(els[s]={classList:{toggle(){}},querySelectorAll(){return [select]}});
const ctx={$ ,send:m=>sent.push(m),esc:s=>s,confirm:()=>true,
 S:{settings:{verification_level:'functional'},status:{},settings_help:{}}};
vm.createContext(ctx);vm.runInContext(process.argv[1]+'\nrenderSettings();',ctx);
assert(els['#settings'].innerHTML.includes('검증 수준'));
for(const v of ['functional','balanced','thorough'])assert(els['#settings'].innerHTML.includes('value="'+v+'"'));
assert(els['#settings'].innerHTML.includes('value="functional" selected'));
select.onchange();assert.deepEqual(sent[0],{type:'setting',key:'verification_level',value:'thorough'});
''', script[start:end])
