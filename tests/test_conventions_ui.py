"""Convention registration, persistence and editing races in the settings UI."""
import asyncio

from duet.core.config import Config
from duet.core.conventions import MAX_CONVENTION_BYTES
from duet.tests.test_markdown import STATIC, node
from duet.tests.test_web import make_ui


def test_conventions_registration_read_save_enable_roundtrip(tmp_path):
    async def run():
        ui = make_ui(tmp_path)
        try:
            state = ui.state()
            assert state["settings"]["coding_conventions_enabled"] is False
            assert "content" not in state["conventions"]
            blocked = await ui.handle({"type": "setting", "key": "coding_conventions_enabled", "value": True})
            assert "먼저" in blocked["text"]
            assert not ui.cfg.settings["coding_conventions_enabled"]
            source = "# Coding conventions\r\n\r\n- 공개 함수에 타입 힌트를 작성한다.\r\n"
            saved = await ui.handle({"type": "convention_save", "content": source, "expected_sha256": ""})
            assert saved["saved"]
            assert saved["conventions"]["content"] == source
            path = tmp_path / "docs/CODING_CONVENTIONS.md"
            assert path.read_bytes() == source.encode("utf-8")
            assert not saved["conventions"]["enabled"]
            version = saved["conventions"]["sha256"]
            enabled = await ui.handle({"type": "setting", "key": "coding_conventions_enabled", "value": True})
            assert enabled["state"]["conventions"]["enabled"] is True
            assert "content" not in enabled["state"]["conventions"]
            restored = Config(tmp_path)
            restored.load()
            assert restored.settings["coding_conventions_enabled"] is True
            read = await ui.handle({"type": "conventions_read"})
            assert read["conventions"]["content"] == source
            assert read["conventions"]["sha256"] == version
            empty = await ui.handle({"type": "convention_save", "content": " "})
            assert not empty["saved"]
            assert path.read_bytes() == source.encode("utf-8")
            assert not ui.history  # uploaded Markdown isn't chat history
        finally:
            await ui.close()
    asyncio.run(run())


def test_convention_save_size_conflict_and_boolean_validation(tmp_path):
    async def run():
        ui = make_ui(tmp_path)
        try:
            for bad in (None, ["bad"], "한" * (MAX_CONVENTION_BYTES // 3 + 1)):
                response = await ui.handle({"type": "convention_save", "content": bad})
                assert not response["saved"]
                assert response["error"]
            assert not (tmp_path / "docs/CODING_CONVENTIONS.md").exists()
            initial = await ui.handle({"type": "convention_save", "content": "# Rules\n- snake_case"})
            version = initial["conventions"]["sha256"]
            path = tmp_path / "docs/CODING_CONVENTIONS.md"
            path.write_text("# Other editor changed this\n", encoding="utf-8")
            conflict = await ui.handle({"type": "convention_save", "content": "stale draft", "expected_sha256": version})
            assert not conflict["saved"]
            assert "다른 창" in conflict["error"]
            assert path.read_text() == "# Other editor changed this\n"
            response = await ui.handle({"type": "setting", "key": "coding_conventions_enabled", "value": "yes"})
            assert not ui.cfg.settings["coding_conventions_enabled"]
            assert "참 또는 거짓" in response["text"]
        finally:
            await ui.close()
    asyncio.run(run())


def test_conventions_editor_preserves_drafts_and_requires_explicit_save():
    node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert'),{TextEncoder}=require('util');
const html=fs.readFileSync(process.argv[1],'utf8');
for(const m of html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g))new vm.Script(m[1]);
const code=html.slice(html.indexOf('const conventionEditor ='),html.indexOf('function renderSettings()'));
const nodes={},sent=[],messages=[];
const $=key=>nodes[key]||(nodes[key]={value:'',checked:false});
let rid=0;
const S={conventions:{enabled:false,path:'docs/CODING_CONVENTIONS.md',sha256:'a'.repeat(64),error:null}};
const ctx={S,$,TextEncoder,confirm:()=>true,toast:s=>messages.push(s),send:m=>{m.rid=++rid;sent.push(m);return true;}};
vm.createContext(ctx);vm.runInContext(code,ctx);
ctx.renderConventions();assert.equal(sent[0].type,'conventions_read');
ctx.receiveConventions({type:'conventions',operation:'read',rid:1,conventions:{...S.conventions,content:'# Original'}});
const input=$('#conventionsContent');assert.equal(input.value,'# Original');
input.value='# Draft';input.oninput();
S.conventions={...S.conventions,enabled:true};ctx.renderConventions();
assert.equal(input.value,'# Draft'); // status refresh must never reset draft
$('#conventionsSave').onclick();assert.equal(sent[1].type,'convention_save');
assert.equal(sent[1].expected_sha256,'a'.repeat(64));
input.value='# New edit while save is pending';input.oninput();
ctx.receiveConventions({operation:'save',rid:2,saved:true,conventions:{...S.conventions,sha256:'b'.repeat(64),content:'# Draft'}});
assert.equal(input.value,'# New edit while save is pending');
assert($('#conventionsDraft').textContent.includes('저장하지 않은 편집'));
S.conventions={...S.conventions,sha256:'c'.repeat(64)};ctx.renderConventions();
assert.equal(input.value,'# New edit while save is pending');
assert($('#conventionsDraft').textContent.includes('저장본 변경됨'));
(async()=>{
 const upload=$('#conventionsFile');
 const before=sent.length;
 await upload.onchange({target:{files:[{name:'RULES.md',size:20,text:async()=>'# Imported'}],value:'selected'}});
 assert.equal(input.value,'# Imported');assert.equal(sent.length,before);
 await upload.onchange({target:{files:[{name:'rules.txt',size:10,text:async()=>'bad'}],value:''}});
 assert.equal(input.value,'# Imported');
 let resolve;
 const pending=upload.onchange({target:{files:[{name:'slow.md',size:10,text:()=>new Promise(r=>resolve=r)}],value:''}});
 input.value='# Typed during upload';input.oninput();resolve('# Obsolete upload');await pending;
 assert.equal(input.value,'# Typed during upload');assert.equal(sent.length,before);
})().catch(e=>{console.error(e);process.exitCode=1});
""", str(STATIC / "index.html"))
