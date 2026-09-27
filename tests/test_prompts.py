import pytest
from duet.tests.test_agreement import orch
from duet.core import prompts, work


@pytest.mark.parametrize('kind', ['architect','implementer','worker','reviewer','ask','review','opinion','turn'])
def test_background_agent_result_instruction(orch, kind):
    if kind in orch.cfg.roles: text = prompts.system_append(orch.cfg, orch.cfg.roles[kind])
    elif kind == 'worker': text = work.WORK_SYSTEM
    elif kind == 'reviewer': text = work.ARCH_WORK_SYSTEM
    elif kind == 'ask': text = prompts.ASK_SYSTEM
    elif kind == 'turn': text = prompts.turn_prompt(orch.cfg, orch.cfg.main_role(), 1, 0, 'human', 'hello')
    else: text = getattr(prompts, kind+'_prompt')('a','b','c','d')
    assert 'foreground' in text and '결과를 받기 전에 응답을 끝내지' in text
