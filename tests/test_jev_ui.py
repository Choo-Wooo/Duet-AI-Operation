"""Public JEV status renders without credentials or raw decision payloads."""
import json

from duet.tests.test_markdown import STATIC, node


def test_settings_script_syntax_and_live_usage_rendering():
    node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
for(const match of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)) new vm.Script(match[1]);
const fn=html.slice(html.indexOf('function renderJevStatus(v)'),html.indexOf('function renderSettings()'));
const nodes=new Map();
const $=key=>{if(!nodes.has(key))nodes.set(key,{textContent:'',value:''});return nodes.get(key);};
const S={settings:{jev_mode:'off'},jev:{configured:false,source:'none'}};
vm.runInNewContext(fn+';renderJevStatus(v);',{S,$,v:{mode:'shadow',configured:true,source:'saved',calls:12,input_tokens:12000,output_tokens:0,estimated_cost_usd:.000504,fallbacks:2,last:{purpose:'flow',mode:'shadow',error:null,answers:{private_debug:'NEVER_RENDER'}}}});
assert.equal(S.settings.jev_mode,'shadow');
assert.equal($('#jevSettings [data-k="jev_mode"]').value,'shadow');
assert($('#jevUsage').textContent.includes('12회 판단'));
assert($('#jevUsage').textContent.includes('12,000'));
assert($('#jevUsage').textContent.includes('$0.000504'));
assert($('#jevUsage').textContent.includes('오류 복귀 2회'));
assert($('#jevLast').textContent.includes('정체 판단 · 관찰'));
assert(!$('#jevLast').textContent.includes('NEVER_RENDER'));
assert($('#jevKeyState').textContent.includes('저장됨'));
assert(!$('#jevKeyClear').disabled);
""", str(STATIC / "index.html"))


def test_health_hint_is_preserved_without_duplicate_prefix():
    activity = {"state": "tool", "detail": "pytest", "last_event_at": 100,
                "turn_started_at": 90, "jev_health_note": "JEV: 장시간 작업 가능성, 대기 유지"}
    title = node("const a=require(process.argv[1]);process.stdout.write(a.describe(JSON.parse(process.argv[2]),500).title);",
                 str(STATIC / "activity.js"), json.dumps(activity))
    assert title.count("JEV:") == 1
    assert "장시간 작업 가능성" in title
    from duet.tui.app import activity_label
    label = activity_label(activity, 500)[0]
    assert label.count("JEV:") == 1


def test_confidence_input_has_probability_bounds_and_sends_decimal():
    node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const code=html.slice(html.indexOf('function renderJevStatus(v)'),html.indexOf('function renderSaves()'));
const sent=[],input={dataset:{k:'jev_confidence_threshold'},value:'0.8',type:'number'};
const nodes={};
const $=key=>nodes[key]||(nodes[key]={classList:{toggle(){}},querySelectorAll(){return key==='#jevSettings'?[input]:[];}});
const S={settings:{jev_mode:'off',jev_confidence_threshold:0.8},status:{},settings_help:{}};
vm.runInNewContext(code+';renderSettings();',{$,S,esc:s=>s,send:m=>sent.push(m)});
const rendered=$('#jevSettings').innerHTML;
assert(rendered.includes('판단 확신 기준'));
assert(rendered.includes('value="0.8" min="0" max="1" step="0.01"'));
input.onchange();
assert.deepEqual(sent[0],{type:'setting',key:'jev_confidence_threshold',value:'0.8'});
""", str(STATIC / "index.html"))
