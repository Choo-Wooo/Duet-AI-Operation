import json
import re

from duet.tests.test_markdown import node, STATIC

HTML = (STATIC / 'index.html').read_text(encoding='utf-8')
SCRIPT = re.findall(r'<script>(.*?)</script>', HTML, re.S)[-1]


def _rules():
    css = ''.join(re.findall(r'<style>(.*?)</style>', HTML, re.S))
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    css = re.sub(r'@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}', '', css)
    rules = {}
    for sel, body in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
        props = {}
        for d in body.split(';'):
            if ':' in d:
                k, v = d.split(':', 1)
                props[k.strip()] = ' '.join(v.split())
        for s in sel.split(','):
            rules.setdefault(' '.join(s.split()), {}).update(props)
    return rules


RULES = _rules()


def css(selector):
    assert selector in RULES, selector
    return RULES[selector]


def fn_body(name):
    m = re.search(r'\b' + name + r'\s*:\s*\w*\s*=>\s*\{|function\s+' + name + r'\s*\(', SCRIPT)
    assert m, name
    i, depth = SCRIPT.index('{', m.start()), 0
    for j in range(i, len(SCRIPT)):
        depth += {'{': 1, '}': -1}.get(SCRIPT[j], 0)
        if depth == 0:
            return SCRIPT[i:j + 1]


def test_views_are_columns():
    assert css('.view.on')['flex-direction'] == 'column'


def test_docs_toolbar_before_layout_and_fixed():
    section = re.search(r'<section class="view" id="v-docs">(.*?)</section>', HTML, re.S).group(1)
    tags = re.findall(r'<div ([^>]*)>', section)
    assert 'id="docToolbar"' in tags[0] and 'class="docs-layout"' in tags[1]
    assert section.index('id="docFilter"') < section.index('class="docs-layout"')
    bar = css('.doctoolbar')
    assert bar['height'] == '40px' and bar['flex'] == 'none' and bar['white-space'] == 'nowrap'
    assert css('#docPath')['text-overflow'] == 'ellipsis'
    lay = css('.docs-layout')
    assert lay['grid-template-columns'] == '260px minmax(0,1fr)' and lay['min-height'] == '0' and lay['flex'] == '1'
    assert 'calc' not in json.dumps(lay)
    for sel in ('#docList', '#docBody'):
        assert css(sel)['overflow'] == 'auto' and css(sel)['min-height'] == '0'


def test_markdown_styles_normalized():
    md = css('.md')
    assert md['max-width'] == '860px' and md['overflow-wrap'] == 'anywhere' and md['min-width'] == '0'
    assert css('.md img')['max-width'] == '100%'
    assert css('.md pre')['overflow-x'] == 'auto'
    assert css('.md .tablewrap')['overflow-x'] == 'auto'
    for h in range(1, 7):
        assert 'font-size' in css(f'.md h{h}')
    html = node('const r=require(process.argv[1]);process.stdout.write(r(process.argv[2]));',
                str(STATIC / 'md.js'), '|a|b|\n|---|---|\n|1|2|')
    assert html.startswith('<div class="tablewrap"><table>') and html.endswith('</table></div>')


def test_activity_row_fixed():
    row = css('#activityRoles')
    assert row['height'] == '32px' and row['flex-wrap'] == 'nowrap' and row['overflow-x'] == 'auto' and row['flex'] == 'none'
    chip = css('.achip')
    assert chip['width'] == '220px' and chip['white-space'] == 'nowrap' and chip['text-overflow'] == 'ellipsis'
    assert chip['overflow'] == 'hidden' and chip['flex'] == 'none'


def test_runtxt_written_only_by_status_summary():
    body = fn_body('renderStatus')
    outside = SCRIPT.replace(body, '')
    assert 'runtxt' in body and 'runtxt' not in outside
    assert 'activityBadge' not in SCRIPT
    assert 'runtxt' not in fn_body('drawActivity')


def test_doc_list_is_incremental():
    body = fn_body('onList')
    assert 'replaceChildren' not in body and 'innerHTML' not in body


def test_activity_labels_fixed_and_detail_only_in_title():
    out = node(r"""
const a=require(process.argv[1]);const res=[];
const detail='$ cd /Users/mac/Workspace && git ls-files -s test/duet | head -3; '+'x'.repeat(200);
for(const state of ['idle','starting','thinking','tool','awaiting_approval','reviewing','done','error'])
 for(const age of [0,61,301]) for(const d of [detail,null]){
  const x=a.describe({state,last_event_at:1000-age,turn_started_at:990,detail:d},1000);
  res.push({state,age,d:!!d,x,chip:a.chipParts('architect',x)});
 }
process.stdout.write(JSON.stringify({labels:a.LABELS,res}));
""", str(STATIC / 'activity.js'))
    data = json.loads(out)
    labels = data['labels']
    assert set(labels) == {'처리 중', '응답 대기', '멈춤 의심', '승인 대기', '턴 중단', '대기'}
    assert all(len(x) <= 8 for x in labels)
    for r in data['res']:
        x, chip = r['x'], r['chip']
        assert x['label'] in labels
        assert 'git ls-files' not in x['label'] + x['text'] + chip['text']
        assert len(chip['text']) < 40
        if r['d']:
            assert 'git ls-files' in x['title'] and 'git ls-files' in chip['title']
    by = {(r['state'], r['age']): r['x']['label'] for r in data['res']}
    assert by[('error', 0)] == '턴 중단' and by[('done', 0)] == '대기'
    assert by[('thinking', 301)] == '멈춤 의심' and by[('tool', 61)] == '응답 대기'
    assert by[('awaiting_approval', 301)] == '승인 대기'


def test_viewer_same_input_does_not_rerender():
    node(r"""
const assert=require('assert'),r=require(process.argv[1]);
(async()=>{
 let tick,mtime=1,docs=0,lists=0;
 const v=r.createViewer({fetch:async url=>url.startsWith('/api/docs')?[{path:'docs/a.md',mtime}]:{path:'docs/a.md',content:'# a',mtime},
   onList:()=>lists++,onDocument:()=>docs++,onError:e=>{throw e;},setInterval:f=>{tick=f;return 1;},clearInterval:()=>{}});
 await v.setOpen(true);await v.select('docs/a.md');assert.equal(docs,1);
 await tick();await tick();assert.equal(docs,1);assert.equal(lists,3);
 mtime=2;await tick();assert.equal(docs,2);
 await v.refresh(true);assert.equal(docs,3);
})().catch(e=>{console.error(e);process.exitCode=1});
""", str(STATIC / 'md.js'))


def test_header_fixed_single_line():
    h = css('header')
    assert h['height'] == '44px' and h['flex-wrap'] == 'nowrap' and h['white-space'] == 'nowrap'
    assert css('header > *')['flex'] == 'none'
    for sel in ('#proj', '#runchip', '#runtxt'):
        assert css(sel)['flex'] == '0 1 auto' and css(sel)['min-width'] == '0'
    assert 'id="runchip"' in HTML and css('#runtxt')['text-overflow'] == 'ellipsis'
    for i in ('btnPause', 'btnStop', 'pendingBadge'):
        assert RULES.get('#' + i, {}).get('flex', 'none') == 'none' and 'id="' + i + '"' in HTML
    rt = css('#runtxt')
    assert rt['max-width'] == '280px' and rt['text-overflow'] == 'ellipsis' and rt['overflow'] == 'hidden'
    assert css('#btnPause')['min-width'] == '84px'
    labels = re.findall(r'c\.textContent = "([^"]*)"', SCRIPT) + re.findall(r'id="conn"[^>]*>([^<]*)<', HTML)
    assert labels and all(len(x) <= 4 for x in labels)


def test_pending_tray_out_of_flow():
    tray = css('#pendingTray')
    assert tray['position'] == 'fixed' and tray['width'] == '420px'
    assert css('#pendingTray.zero')['display'] == 'none' and 'id="pendingTray" class="zero"' in HTML
    assert tray['bottom'] == 'calc(var(--composer-h) + 12px)' and css(':root')['--composer-h'] == '72px'
    track = fn_body('trackComposer')
    assert '.composer' in track and 'offsetHeight' in track and '--composer-h' in track
    assert 'classList.remove("collapsed")' in fn_body('showPending')
    assert css('.trayhead .grow')['flex'] == '1'
    toast = css('.toast')
    assert toast['position'] == 'fixed' and toast['top'] == '56px' and 'bottom' not in toast
    assert css('#pending')['max-height'] == '50vh' and css('#pending')['overflow-y'] == 'auto'
    main = re.search(r'<main>(.*?)</main>', HTML, re.S).group(1)
    assert 'id="pending"' not in main
    for i in ('id="pendingTray"', 'id="pendingCount"', 'id="pendingBadge"', 'id="pendingToggle"'):
        assert i in HTML
    assert 'pendingCount' in fn_body('updateBadges')


def test_board_incremental_and_buttons_disabled():
    body = fn_body('renderBoard')
    assert 'innerHTML' not in body
    card = fn_body('boardCard')
    assert 'c.resume = mk(' in card and 'c.cancel = mk(' in card and not re.search(r'\?\s*mk\(', card)
    upd = fn_body('updateCard')
    assert 'c.resume.disabled' in upd and 'c.cancel.disabled' in upd
    batch = fn_body('renderBatch')
    assert 'if (!b)' in batch and 'id="boardBatch"' in HTML
    assert css('.boardbatch')['min-height'] == '28px'
