import json
import pytest
import asyncio
from duet.tests.test_agreement import orch
from duet.tests.test_markdown import node, STATIC


@pytest.mark.parametrize('age,state,word,web', [(59,'thinking','처리 중','처리 중'),(60,'tool','응답 대기','응답 대기'),
    (299,'reviewing','응답 대기','응답 대기'),(300,'starting','멈춘 것 같음','멈춤 의심'),(900,'awaiting_approval','승인 대기','승인 대기'),
    (900,'idle','대기','대기'),(900,'done','완료','대기'),(900,'error','오류','턴 중단')])
def test_web_tui_activity_clock_boundaries(age, state, word, web):
    a = {'state':state,'last_event_at':100,'turn_started_at':90,'detail':'Read'}
    text = node("const a=require(process.argv[1]);const c=a.clock(100,9000);process.stdout.write(a.describe(JSON.parse(process.argv[2]),c(9000+Number(process.argv[3]))).label);",
                str(STATIC/'activity.js'), json.dumps(a), str(age))
    assert text == web
    from duet.tui.app import activity_label
    assert word in activity_label(a, 100+age)[0]


def test_tui_widgets_tick_without_push(orch, monkeypatch):
    from duet.tui.app import DuetApp, _safe_id
    from textual.widgets import Static, TabbedContent
    import duet.tui.app as module
    async def run():
        app = DuetApp(orch.cfg, orch.bus, [], True, None)
        async def serve(): await asyncio.Event().wait()
        monkeypatch.setattr(app.orch, 'serve', serve)
        async with app.run_test(size=(220, 30)) as pilot:
            await pilot.pause()
            s = app.orch.status()
            s['server_now'] = 100
            s['activity']['implementer'] = dict(state='thinking', detail='Read', turn_started_at=90,last_event_at=40)
            s['work_sessions'] = [dict(id='one',session='implementer#one',role='implementer',state='awaiting_approval',last_event_at=1,turn_started_at=1,detail='')]
            app.orch.bus.emit('status', None, **s)
            await pilot.pause()
            tabs = app.query_one('#tabs', TabbedContent)
            assert '응답 대기' in str(tabs.get_tab(_safe_id('implementer')).label)
            assert '승인 대기' in str(tabs.get_tab(_safe_id('implementer')).label)
            app._status_received -= 301
            app._tick()
            assert '멈춘 것 같음' in str(tabs.get_tab(_safe_id('implementer')).label)
            assert '멈춘 것 같음' in str(app.query_one('#status', Static).render())
        await app.orch.close()
    asyncio.run(run())
