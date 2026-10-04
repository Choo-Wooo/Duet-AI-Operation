import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from duet.adapters.base import TurnResult
from duet.core.usage_wait import classify
from duet.tests.test_autopilot_design import make


def failure(error='Usage limit reached', reset=None):
    return TurnResult('', ok=False, error=error, usage_reset_at=reset)


def test_classifier_only_explicit_failures():
    for text in ['Usage limit reached', "You've hit your limit", 'rate_limit_exceeded', 'Too many requests', '사용량 한도 초과']:
        assert classify(failure(text)).usage_limited
    for text in ['insufficient_quota rate_limit', 'billing limit reached', 'Authentication error', 'network disconnected', 'permission denied', 'context window too long']:
        assert not classify(failure(text)).usage_limited
    assert not classify(TurnResult('Example: usage limit reached')).usage_limited
    assert not classify(TurnResult('', ok=False, error='usage limit', interrupted=True)).usage_limited
    assert not classify(TurnResult('', ok=False, error='usage limit', context_overflow=True)).usage_limited


def test_reset_metadata_and_text():
    assert classify(failure(), {'resetsAt': 2000000000}, now=100).usage_reset_at == 2000000000
    assert classify(failure(), {'resets_at': 2000000000000}, now=100).usage_reset_at == 2000000000
    assert classify(failure(), {'retry_after': 60}, now=100).usage_reset_at == 160
    assert classify(failure('Usage limit reached; try again in 2 hours'), now=100).usage_reset_at == 7300
    assert classify(failure('Usage limit resets at 2026-09-30T05:00:00+09:00')).usage_reset_at == datetime(2026,9,29,20,tzinfo=timezone.utc).timestamp()
    now=datetime(2026,9,29,20,tzinfo=timezone.utc).timestamp()
    assert classify(failure("You've hit your limit · resets 3pm (Asia/Seoul)"),now=now).usage_reset_at == datetime(2026,9,30,6,tzinfo=timezone.utc).timestamp()
    assert classify(failure(), {'retry_after':'nan'}, now=100).usage_reset_at is None
    assert classify(failure('unknown'), {'codexErrorInfo':'usageLimitExceeded'}).usage_limited


class Adapter:
    def __init__(self, results):
        self.role=SimpleNamespace(cli='codex')
        self.label='implementer'
        self.session_id='saved-thread'
        self.results=list(results)
        self.prompts=[]
        self.reconnections=0
    async def run_turn(self,prompt):
        self.prompts.append(prompt)
        return self.results.pop(0)
    async def close(self):
        pass
    async def start(self):
        self.reconnections+=1
        assert self.session_id=='saved-thread'


def ready(tmp_path):
    orch,_=make(tmp_path)
    orch.set_usage_retry(True)
    now=[1000.0]
    orch.usage.clock=lambda:now[0]
    ticks=[]
    async def tick():
        ticks.append(dict(orch.usage.waits))
        now[0]=max(x['until'] for x in orch.usage.waits.values())
        await asyncio.sleep(0)
    orch.usage._tick=tick
    return orch,now,ticks


def test_off_is_default_and_never_retries(tmp_path):
    orch,_=make(tmp_path)
    ad=Adapter([failure()])
    result=asyncio.run(orch.run_agent_turn(ad,'task'))
    assert not orch.usage.enabled and not result.ok
    assert len(ad.prompts)==1 and not orch.usage.waits


def test_reset_wait_resumes_same_session_and_no_duplicate_instruction(tmp_path):
    orch,now,ticks=ready(tmp_path)
    ad=Adapter([failure(reset=1100),TurnResult('done')])
    result=asyncio.run(orch.run_agent_turn(ad,'task'))
    assert result.ok and now[0]==1105 and ad.reconnections==1
    assert len(ad.prompts)==2 and '중복 실행하지' in ad.prompts[1]
    assert ticks[0]['implementer']['until']==1105
    assert not orch.usage.waits


def test_unknown_reset_uses_configured_five_hour_wait(tmp_path):
    orch,now,_=ready(tmp_path)
    ad=Adapter([failure(),TurnResult('done')])
    assert asyncio.run(orch.run_agent_turn(ad,'task')).ok
    assert now[0]==1000+18000


def test_retry_count_is_bounded(tmp_path):
    orch,now,_=ready(tmp_path)
    orch.cfg.settings['usage_limit_max_retries']=2
    ad=Adapter([failure(),failure(),failure()])
    result=asyncio.run(orch.run_agent_turn(ad,'task'))
    assert not result.ok and len(ad.prompts)==3 and ad.reconnections==2


def test_stop_and_disable_cancel_without_retry(tmp_path):
    async def go():
        orch,_,_=ready(tmp_path)
        ad=Adapter([failure()])
        async def tick():
            await orch.stop()
        orch.usage._tick=tick
        assert (await orch.run_agent_turn(ad,'task')).interrupted
        assert not orch.usage.waits and len(ad.prompts)==1
        orch.stop_requested=False
        ad=Adapter([failure()])
        async def off():
            orch.set_usage_retry(False)
        orch.usage._tick=off
        assert (await orch.run_agent_turn(ad,'task')).interrupted
        assert not orch.usage.waits and len(ad.prompts)==1
    asyncio.run(go())


def test_pause_survives_reset_time(tmp_path):
    async def go():
        orch,now,_=ready(tmp_path)
        ad=Adapter([failure(reset=1100),TurnResult('done')])
        orch.pause()
        calls=[]
        async def tick():
            calls.append(1)
            now[0]=1200
            assert len(ad.prompts)==1
            if len(calls)==2:
                orch.resume()
        orch.usage._tick=tick
        assert (await orch.run_agent_turn(ad,'task')).ok
        assert len(calls)==2
    asyncio.run(go())


def test_budget_deadline_cancels_wait(tmp_path):
    orch,now,_=ready(tmp_path)
    orch.started=1000
    orch.max_hours=1
    ad=Adapter([failure()])
    assert asyncio.run(orch.run_agent_turn(ad,'task')).interrupted
    assert len(ad.prompts)==1


def test_cancellation_clears_wait_status(tmp_path):
    async def go():
        orch,_,_=ready(tmp_path)
        ad=Adapter([failure()])
        entered=asyncio.Event()
        async def tick():
            entered.set()
            await asyncio.Event().wait()
        orch.usage._tick=tick
        task=asyncio.create_task(orch.run_agent_turn(ad,'task'))
        await entered.wait()
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        assert not orch.usage.waits
    asyncio.run(go())


def test_web_and_command_option(tmp_path):
    from duet.commands import handle
    from duet.web.server import WebUI
    orch,_=make(tmp_path)
    w=WebUI(orch.cfg,orch.bus,[],True,'t')
    out=asyncio.run(w.handle({'type':'setting','key':'usage_limit_retry','value':True}))
    assert out['state']['settings']['usage_limit_retry']
    assert '꺼짐' in asyncio.run(handle(w.orch,'/usage-retry off'))
    assert not w.orch.usage.enabled
    asyncio.run(w.orch.close())


def test_codex_exhausted_window_notification(tmp_path):
    from duet.adapters.codex import CodexAdapter
    orch,_=make(tmp_path)
    ad=CodexAdapter(orch.cfg.roles['implementer'],tmp_path,orch.bus,None,'session','')
    ad._on_notification('account/rateLimits/updated',{'rateLimits':{'primary':{'usedPercent':100,'resetsAt':2000},'secondary':{'usedPercent':50,'resetsAt':9000}}})
    assert ad._usage_reset_at==2000
    ad._on_notification('account/rateLimits/updated',{'rateLimits':{'primary':{'usedPercent':10,'resetsAt':2000}}})
    assert ad._usage_reset_at is None


def test_claude_structured_rejection_keeps_reset(tmp_path):
    from collections import deque
    from claude_agent_sdk import AssistantMessage, ResultMessage
    from duet.adapters.claude import ClaudeAdapter
    orch,_=make(tmp_path)
    ad=ClaudeAdapter(orch.cfg.roles['architect'],tmp_path,orch.bus,None,'session','')
    event=type('RateLimitEvent',(),{'rate_limit_info':SimpleNamespace(status='rejected',resets_at=2000)})()
    class Client:
        async def query(self,prompt): pass
        async def receive_response(self):
            yield event
            yield AssistantMessage(content=[],model='test',error='rate_limit')
            yield ResultMessage(subtype='error',duration_ms=1,duration_api_ms=1,is_error=True,num_turns=1,session_id='session')
    ad.client=Client()
    ad._stderr=deque()
    result=asyncio.run(ad.run_turn('task'))
    assert result.usage_limited and not result.ok and result.usage_reset_at==2000


def test_parallel_work_turn_retries_before_failing(tmp_path):
    from duet.core.work import WorkItem
    orch,_,_=ready(tmp_path)
    ad=Adapter([failure(),TurnResult('done')])
    ad.label='implementer#api'
    item=WorkItem(id='api',role='implementer',task='task')
    result,full,dirs=asyncio.run(orch.work._turn(ad,item,'implement','task','작업자'))
    assert result.ok and full=='done' and ad.reconnections==1


def test_reviewer_retries_with_separate_attempt_timeout(tmp_path):
    orch,_,_=ready(tmp_path)
    ad=Adapter([failure(),TurnResult('allow')])
    ad.label='architect (심사)'
    result=asyncio.run(orch._review_turn(ad,'review',.5))
    assert result.ok and ad.reconnections==1


def test_shared_cli_cooldown_does_not_block_another_provider(tmp_path):
    orch,now,ticks=ready(tmp_path)
    orch.usage.cooldowns['codex']=1200
    other=Adapter([TurnResult('done')]);other.role.cli='claude'
    assert asyncio.run(orch.run_agent_turn(other,'task')).ok
    assert now[0]==1000 and not ticks
    same=Adapter([TurnResult('done')])
    assert asyncio.run(orch.run_agent_turn(same,'task')).ok
    assert now[0]==1200 and len(same.prompts)==1
