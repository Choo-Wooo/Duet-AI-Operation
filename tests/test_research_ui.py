"""Workflow selection and both sets of role settings share the live web UI."""
from html.parser import HTMLParser

from duet.tests.test_markdown import STATIC, node


def test_workflow_button_is_next_to_pause_and_both_role_sections_are_present():
    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = []

        def handle_starttag(self, tag, attrs):
            element_id = dict(attrs).get("id")
            if element_id:
                self.ids.append(element_id)

    elements = Elements()
    elements.feed((STATIC / "index.html").read_text())
    assert elements.ids.index("btnWorkflow") + 1 == elements.ids.index("btnPause")
    assert {"roles", "researchRoles", "workflowDialog", "researchProgress"} <= set(elements.ids)


def test_workflow_selection_waits_for_server_and_respects_running_request():
    node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
for(const m of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))new vm.Script(m[1]);
const code=html.slice(html.indexOf('function renderWorkflow(s)'),html.indexOf('function renderTargets()'));
const nodes={},sent=[],messages=[],options=['development','research'].map(workflow=>({dataset:{workflow},setAttribute(k,v){this[k]=v;}}));
const $=key=>nodes[key]||(nodes[key]={textContent:'',classList:{toggle(){}},querySelectorAll(){return options;},close(){this.closed=true;}});
const S={status:{workflow:'development',workflow_can_change:true}},ctx={S,$,send:m=>{sent.push(m);return true;},toast:m=>messages.push(m)};
vm.createContext(ctx);vm.runInContext(code,ctx);
ctx.renderWorkflow(S.status);
assert.equal($('#btnWorkflow').textContent,'개발 모드 ▾');
assert.equal(options[0]['aria-pressed'],'true');
ctx.chooseWorkflow('research');
assert.deepEqual(sent,[{type:'workflow',name:'research'}]);
assert.equal(S.status.workflow,'development'); // only a server state update changes selected mode
assert.equal($('#workflowDialog').closed,true);
S.status={workflow:'research',workflow_can_change:false,workflow_change_reason:'토론 진행 중',research:{phase:'running',stage:'verify',turns:3,active_role:'critic',goal:'<script>goal</script>'}};
ctx.renderWorkflow(S.status);
assert.equal($('#btnWorkflow').textContent,'연구 모드 ▾');
assert.equal($('#researchPhase').textContent,'진행 중 · 3턴');
assert.equal($('#researchGoal').textContent,'<script>goal</script>');
assert.equal($('#researchAction').textContent,'비판자 · 검증');
assert.equal($('#researchProgress').hidden,false);
assert.equal($('#researchRolesActive').hidden,false);
assert.equal(options[0].disabled,true);
assert.equal(options[1].disabled,false);
ctx.chooseWorkflow('development');
assert.equal(sent.length,1);assert.equal(messages.at(-1),'토론 진행 중');
ctx.chooseWorkflow('unknown');assert.equal(sent.length,1);
S.status.workflow_can_change=true;ctx.send=()=>false;
$('#workflowDialog').closed=false;ctx.chooseWorkflow('development');
assert.equal($('#workflowDialog').closed,false);assert(messages.at(-1).includes('연결'));
""", str(STATIC / "index.html"))


def test_research_roles_reuse_model_controls_without_development_main_actions():
    node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const code=html.slice(html.indexOf('const modelsFor ='),html.indexOf('function renderModels()'));
const nodes={},sent=[];
const $=key=>nodes[key]||(nodes[key]={options:[{}],value:'',querySelectorAll(){return [];}});
const roles=[
 {name:'architect',workflow:'development',cli:'claude',model:'claude-a',is_main:true,permissions:'read_only'},
 {name:'researcher',workflow:'development',cli:'codex',model:'codex-a',permissions:'read_only'},
 {name:'explorer',label:'탐색자',workflow:'research',cli:'claude',model:'claude-a',permissions:'read_only',is_main:true},
 {name:'critic',label:'비판자',workflow:'research',cli:'codex',model:'codex-a',permissions:'read_only'}
];
const S={roles,clis:['claude','codex'],permissions:['read_only','workspace_write'],models:{clis:{claude:{models:[{id:'claude-a',name:'Claude A'}]},codex:{models:[{id:'codex-a',name:'Codex A'}]}}}};
const controls=[{value:'codex::codex-a',dataset:{f:'model'}},{value:'',dataset:{f:'context_limit'}}];
$('#v-roles').querySelectorAll=selector=>{assert.equal(selector,'tr[data-n]');return [{dataset:{n:'explorer'},querySelectorAll(){return controls;},querySelector(){return null;}}];};
const ctx={S,$,esc:s=>String(s??'').replace(/</g,'&lt;'),short:s=>s||'',colorOf:()=>'',send:m=>sent.push(m)};
vm.createContext(ctx);vm.runInContext(code,ctx);ctx.renderRoles();
const dev=$('#roles').innerHTML,research=$('#researchRoles').innerHTML;
assert(dev.includes('data-n="researcher"')); // existing web research worker retains its role
assert(!dev.includes('data-n="explorer"'));
assert(research.includes('data-n="explorer"'));assert(research.includes('data-n="critic"'));
assert(research.includes('탐색자'));assert(research.includes('비판자'));
for(const field of ['cli','model','effort','permissions','context_limit'])assert(research.includes('data-f="'+field+'"'));
assert(!research.includes('data-main'));assert(!research.includes('data-del'));assert(!research.includes(' ★'));
assert(dev.includes('data-main'));assert(dev.includes('data-del'));
controls[0].onchange();
assert.deepEqual(sent,[{type:'role_set',name:'explorer',field:'cli',value:'codex'},{type:'role_set',name:'explorer',field:'model',value:'codex-a'}]);
controls[1].onchange();assert.equal(sent[2].value,'default');
""", str(STATIC / "index.html"))


def test_composer_uses_active_main_and_drops_inactive_workflow_target():
    node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const code=html.slice(html.indexOf('function renderTargets()'),html.indexOf('// ---------------- 이벤트'));
const sel={value:'implementer',options:[],set innerHTML(v){this.markup=v;this.options=[...v.matchAll(/value="([^"]*)"/g)].map(m=>({value:m[1]}));}};
const S={main:'explorer',status:{workflow:'research'},roles:[{name:'architect',is_main:true},{name:'implementer'},{name:'explorer'},{name:'critic'}]};
const ctx={S,$:()=>sel,esc:s=>s,isResearchRole:r=>['explorer','critic'].includes(r.name)};
vm.createContext(ctx);vm.runInContext(code,ctx);ctx.renderTargets();
assert(sel.markup.includes('메인 (explorer)'));assert(sel.markup.includes('@critic'));
assert(!sel.markup.includes('@explorer'));assert(!sel.markup.includes('implementer'));assert.equal(sel.value,'');
S.main='architect';S.status.workflow='development';sel.value='critic';ctx.renderTargets();
assert(sel.markup.includes('메인 (architect)'));assert(sel.markup.includes('@implementer'));
assert(!sel.markup.includes('critic'));assert(!sel.markup.includes('explorer'));assert.equal(sel.value,'');
""", str(STATIC / "index.html"))


def test_status_only_workflow_change_updates_composer_main():
    node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const code=html.slice(html.indexOf('function renderStatus(s)'),html.indexOf('function renderWorkflow(s)'));
const targets=html.slice(html.indexOf('function renderTargets()'),html.indexOf('// ---------------- 이벤트'));
const nodes={},S={main:'architect',roles:[{name:'architect'},{name:'explorer'},{name:'critic'}]};
const target={value:'architect',options:[],set innerHTML(v){this.markup=v;this.options=[...v.matchAll(/value="([^"]*)"/g)].map(m=>({value:m[1]}));}};
const $=key=>key==='#target'?target:(nodes[key]||(nodes[key]={}));
const ctx={S,$,esc:s=>s,renderWorkflow(){},renderJevStatus(){},renderBoard(){},drawActivity(){},
 performance:{now:()=>1},DuetActivity:{clock:()=>1},isResearchRole:r=>['explorer','critic'].includes(r.name)};
vm.createContext(ctx);vm.runInContext(code+targets,ctx);
ctx.renderStatus({workflow:'research',main:'explorer',tokens:0});
assert.equal(S.main,'explorer');assert(target.markup.includes('메인 (explorer)'));
assert(!target.markup.includes('architect'));assert.equal(target.value,'');
ctx.renderStatus({workflow:'development',main:'architect',tokens:0});
assert.equal(S.main,'architect');assert(target.markup.includes('메인 (architect)'));
""", str(STATIC / "index.html"))


def test_dialogue_summary_hides_only_research_peer_protocol_fence():
    node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8');
const code=html.slice(html.indexOf('function visibleTurnSummary('),html.indexOf('function onEvent('));
const ctx={};vm.createContext(ctx);vm.runInContext(code,ctx);
const tick=String.fromCharCode(96),fence=tick.repeat(3);
const source='반례를 확인했습니다.\n'+fence+'duet-research\n{"candidate_id":"c1"}\n'+fence+'\n추가 검증이 필요합니다.';
assert.equal(ctx.visibleTurnSummary(source,'critic'),'반례를 확인했습니다.\n추가 검증이 필요합니다.');
assert.equal(ctx.visibleTurnSummary('새 해결안입니다.\n'+fence+'duet-research\n{"candidate_id":"c1",…','explorer'),'새 해결안입니다.');
assert.equal(ctx.visibleTurnSummary(source,'human'),source);
assert.equal(ctx.visibleTurnSummary(source,'architect'),source);
for(const quoted of ['문법은 '+fence+'duet-research 라고 씁니다.','> '+fence+'duet-research\n> 설명 예시',fence+tick+'text\n'+fence+'duet-research\n샘플\n'+fence+tick]) {
 assert.equal(ctx.visibleTurnSummary(quoted,'explorer'),quoted);
}
const raw=html.slice(html.indexOf('case "text":'),html.indexOf('case "tool":'));
assert(raw.includes('esc(d.text)'));assert(!raw.includes('visibleTurnSummary'));
""", str(STATIC / "index.html"))
