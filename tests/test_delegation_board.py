import asyncio

import pytest
from duet.core.agreement import new_task
from duet.core.config import Config
from duet.core.work import WorkItem
from duet.tests.test_agreement import orch
from duet.tests.test_layout import fn_body
from duet.tests.test_markdown import node


@pytest.mark.parametrize('phase,role', [('plan', 'implementer'), ('plan_review', 'architect'),
                                       ('implement', 'implementer'), ('verify', 'architect')])
def test_single_work_visible_alongside_parallel_without_scheduling(orch, phase, role):
    task = new_task('implementer', 'Deploy the map', None, {})
    task['phase'] = phase
    orch.cfg.state.task = task
    orch.work.items['other'] = WorkItem('other', 'implementer', 'Parallel work')
    status = orch.status()
    assert len(status['work']) == 1
    assert len(status['board_work']) == 2
    card = next(x for x in status['board_work'] if x['id'] == task['id'])
    assert card['task'] == 'Deploy the map'
    assert card['status'] == phase
    assert card['active_role'] == role
    assert card['thread'] is None
    task.update(waiting=True, wait_reason='Need input', submitted_version=1)
    card = orch.status()['board_work'][-1]
    assert card['status'] == 'waiting:' + phase
    assert card['wait_reason'] == 'Need input'
    assert card['thread'] == task['plan_path']


@pytest.mark.parametrize('outcome', ['accepted', 'cancelled'])
def test_finished_delegations_survive_reload(orch, outcome):
    async def run():
        task = new_task('implementer', 'First work', None, {})
        orch.cfg.state.task = task
        orch._finish_task(outcome)
        assert orch.status()['board_work'][-1]['status'] == outcome
        cfg = Config(orch.project)
        cfg.load()
        assert cfg.state.completed_delegations[-1]['id'] == task['id']
        orch.cfg.state.task = new_task('implementer', 'Next work', None, {})
        assert len(orch.status()['board_work']) == 2
        assert orch.status()['work'] == []
        await orch.close()
    asyncio.run(run())


def test_single_card_ui_and_parallel_controls():
    node(r'''
const assert = require('assert');
const S = {status: {activity: {architect: {state:'thinking'}}, work_sessions: []}};
const setText = (e,t) => e.textContent = t;
const colorOf = () => 'blue', short = t => t, activityNow = () => 0;
const DuetActivity = {describe: () => ({text: '처리 중', title: 'active'})};
// Supply the function's free bindings in its actual lexical scope.
const update = eval('(function(c,w)' + process.argv[1] + ')');
const c = {};
for(const key of ['id','role','status','sessions','task','deps','files','wait','merged','open','resume','cancel']) c[key] = {style:{}};
update(c, {id:'one',kind:'delegation',role:'implementer',active_role:'architect',status:'plan_review',task:'single'});
assert.equal(c.status.textContent, '계획 검토');
assert(c.role.textContent.includes('순차 위임'));
assert(c.sessions.textContent.includes('architect'));
assert(c.resume.hidden && c.cancel.hidden && c.open.disabled);
update(c, {id:'one',kind:'delegation',role:'implementer',status:'accepted',task:'single',thread:'docs/plans/one.md'});
assert.equal(c.status.textContent, '완료');
assert.equal(c.open.disabled, false);
assert.equal(c.sessions.textContent, '세션 —');
update(c, {id:'parallel',role:'implementer',status:'waiting:implement',task:'parallel',thread:'docs/work/a.md'});
assert(!c.resume.hidden && !c.cancel.hidden && !c.resume.disabled);
''', fn_body('updateCard'))
