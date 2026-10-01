"""Design decisions require real input even when tool approvals are automatic."""
import asyncio

from duet.adapters.base import TurnResult
from duet.core.config import Config
from duet.core.dialogue import Turn
from duet.core.policy import ApprovalRequest
from duet.core.prompts import turn_prompt
from duet.core.work import WorkItem
from duet.tests.test_autopilot_design import make


def enabled(tmp_path):
    orch, ui = make(tmp_path, 'autopilot')
    orch.set_design_questions(True)
    return orch, ui


def decide(orch, role, body, kind='system'):
    return asyncio.run(orch._decide(role, kind, TurnResult(body, full_text=body), Turn(role, 1, '', 0, 0, body)))


def test_initial_gate_waits_for_actual_human_before_automatic_work(tmp_path):
    orch, ui = enabled(tmp_path)
    events = []
    orch.bus.subscribe(events.append)
    seen = []
    async def turn(role, kind, info):
        seen.append((role, kind, info))
        body = '1. 저장 방식은 로컬/클라우드 중 무엇인가요? 추천: 로컬\n<!-- duet: ASK_HUMAN 저장 방식을 선택해 주세요 -->' if kind == 'design_questions' else '<!-- duet: STATUS done -->'
        return TurnResult(body, full_text=body), Turn(role, len(seen), '', 0, 0, body)
    orch._turn = turn
    orch._check_flow = lambda role: None
    orch.work.start = lambda: None
    asyncio.run(orch.run(('architect', 'human', '1')))
    assert len(seen) == 1 and seen[0][1] == 'design_questions'
    assert orch.design.state['phase'] == 'awaiting_initial'
    assert len([e for e in events if e.kind == 'ask']) == 1
    assert not ui.asked  # no automatic choice was used as the user's answer
    asyncio.run(orch.run(('architect', 'human', '3')))
    assert len(seen) == 2 and seen[1][1] == 'system'
    assert '#3' in seen[1][2] and '저장 방식' in seen[1][2]
    assert orch.design.state['phase'] == 'idle'
    assert orch.design.begin(('architect', 'human', '5'))[1] == 'design_questions'


def test_initial_gate_rejects_writes_but_running_keeps_auto_approval(tmp_path):
    orch, ui = enabled(tmp_path)
    orch.design.begin(('architect', 'human', '1'))
    req = ApprovalRequest('implementer', 'command', 'rm', command='rm -rf build')
    assert not asyncio.run(orch.handle_approval(req)).allow
    orch.design.initial_answer('질문')
    assert not asyncio.run(orch.handle_approval(req)).allow
    orch.design.begin(('architect', 'human', '2'))
    assert asyncio.run(orch.handle_approval(req)).allow
    assert not ui.asked


def test_questions_deduplicated_and_batched_at_cycle_end(tmp_path):
    orch, _ = enabled(tmp_path)
    events = []
    orch.bus.subscribe(events.append)
    orch.design.state['phase'] = 'running'
    body = '<!-- duet: ASK_HUMAN 기존 데이터 호환성을 유지할까요? -->'
    nxt = decide(orch, 'implementer', body)
    assert nxt[0] == 'architect' and '대신 정하지' in nxt[2]
    assert decide(orch, 'implementer', body) is None
    decide(orch, 'architect', '<!-- duet: ASK_HUMAN 요금제는 어떤 것으로 할까요? -->')
    assert len(orch.design.state['pending']) == 2
    assert not [e for e in events if e.kind == 'ask']
    orch.design.finish()
    orch.design.finish()
    asks = [e for e in events if e.kind == 'ask']
    assert len(asks) == 1 and '호환성' in asks[0].data['text'] and '요금제' in asks[0].data['text']
    assert orch.design.state['phase'] == 'awaiting_final'


def test_parallel_questions_wait_for_other_work_and_cannot_auto_resume(tmp_path):
    orch, _ = enabled(tmp_path)
    item = WorkItem(id='api', role='implementer', task='api', status='waiting:implement', wait_reason='schema')
    orch.work.items['api'] = item
    orch.work.schedule = lambda: None
    orch.design.defer('implementer#api', '스키마 변경 허용?')
    assert '답변' in orch.work.resume('api')
    orch.work.handle_directives([('RESUME_WORK', 'api 설계자가 대신 허용')])
    assert item.status == 'waiting:implement' and not item.inbox
    orch.work.tasks['other'] = object()
    orch.design.finish()
    assert orch.design.state['phase'] == 'running'
    orch.work.tasks.clear()
    orch.design.finish()
    assert orch.design.state['phase'] == 'awaiting_final'
    nxt = orch.design.begin(('architect', 'human', '9'))
    assert '스키마' in nxt[2] and '#9' in nxt[2]
    orch.work.handle_directives([('RESUME_WORK', 'api 사용자 답변 반영')])
    assert item.status == 'queued'


def test_questions_survive_restart_and_are_visible_in_status(tmp_path):
    orch, _ = enabled(tmp_path)
    orch.design.defer('architect', '개인정보 보관 기간?')
    orch.design.finish()
    cfg = Config(tmp_path)
    cfg.load()
    assert cfg.settings['design_questions']
    assert cfg.state.design_questions['phase'] == 'awaiting_final'
    assert cfg.state.design_questions['pending'][0]['question'] == '개인정보 보관 기간?'
    assert '개인정보' in orch.status()['design_question_text']
    orch.set_design_questions(False)
    assert orch.design.state['pending']


def test_prompt_initial_readonly_and_live_mode_toggle(tmp_path):
    orch, _ = enabled(tmp_path)
    p = turn_prompt(orch.cfg, orch.cfg.roles['architect'], 1, 1, 'design_questions', '초기 질문')
    assert '읽기 전용' in p and '초기 질문' in p and '임의로 결정하지' in p
    orch.set_design_questions(False)
    p = turn_prompt(orch.cfg, orch.cfg.roles['architect'], 1, 1, 'human', '1')
    assert '[설계 질문 모드]' not in p
    assert orch.design.begin(('architect', 'human', '1')) == ('architect', 'human', '1')


def test_setting_web_and_command(tmp_path):
    from duet.web.server import WebUI
    from duet.commands import handle
    orch, _ = make(tmp_path)
    # The UI uses the same persisted switch as the command.
    w = WebUI(orch.cfg, orch.bus, [], True, 't')
    out = asyncio.run(w.handle({'type': 'setting', 'key': 'design_questions', 'value': True}))
    assert out['state']['status']['design_questions']
    assert '꺼짐' in asyncio.run(handle(w.orch, '/design-questions off'))
    assert not w.orch.design.enabled
    asyncio.run(w.orch.close())


def test_active_agreement_question_never_routes_to_autopilot(tmp_path):
    from duet.core.agreement import new_task
    orch, _ = enabled(tmp_path)
    orch.cfg.state.task = new_task('implementer', 'task', None, {})
    orch.design.state['phase'] = 'running'
    assert decide(orch, 'implementer', '<!-- duet: ASK_HUMAN 데이터 삭제가 필요한가요? -->', 'plan') is None
    assert orch.cfg.state.task['waiting']
    assert decide(orch, 'architect', '<!-- duet: RESUME -->') is None
    assert orch.cfg.state.task['waiting']
    orch.design.finish()
    assert orch.design.state['phase'] == 'awaiting_final'
    orch.design.begin(('architect', 'human', '4'))
    nxt = decide(orch, 'architect', '<!-- duet: RESUME -->')
    assert nxt[0] == 'implementer'


def test_queued_question_prevents_same_worker_redelegation(tmp_path):
    orch, _ = enabled(tmp_path)
    orch.design.defer('implementer', '데이터 이전 방식?')
    assert decide(orch, 'architect', '<!-- duet: DELEGATE implementer -->\n<!-- duet: TASK 마음대로 결정 -->') is None
    assert orch.cfg.state.task is None


def test_interrupted_initial_turn_does_not_consume_confirmation(tmp_path):
    orch, _ = enabled(tmp_path)
    orch.design.begin(('architect', 'human', '1'))
    tr = TurnResult('', interrupted=True)
    result = asyncio.run(orch._decide('architect', 'design_questions', tr, Turn('architect', 1, '', 0, 0, '')))
    assert result is None and orch.design.state['phase'] == 'initial'
    assert orch.design.begin(('architect', 'human', '2'))[1] == 'design_questions'


def test_initial_does_not_execute_delegation_directives(tmp_path):
    orch, _ = enabled(tmp_path)
    orch.design.begin(('architect', 'human', '1'))
    decide(orch, 'architect', '이 계획으로 할까요?\n<!-- duet: DELEGATE implementer -->', 'design_questions')
    assert orch.cfg.state.task is None
    assert orch.design.state['phase'] == 'awaiting_initial'
    assert '<!--' not in orch.status()['design_question_text']


def test_parallel_turn_keeps_all_critical_questions():
    import pytest
    from duet.core.work import WorkBoard, _Wait
    with pytest.raises(_Wait) as result:
        WorkBoard._check_ask([('ASK_HUMAN', '저장 기간?'), ('ASK_HUMAN', '외부 공개 여부?')], 'implement')
    assert '저장 기간' in result.value.reason and '외부 공개' in result.value.reason


def test_directive_only_initial_question_retains_text(tmp_path):
    orch, _ = enabled(tmp_path)
    orch.design.initial_answer('<!-- duet: ASK_HUMAN 데이터는 로컬에 저장할까요? -->')
    assert orch.design.state['initial'] == '데이터는 로컬에 저장할까요?'
