import json
from pathlib import Path
import shutil
import subprocess
import pytest

STATIC = Path(__file__).parents[1] / 'web/static'


def node(script, *args):
    exe = shutil.which('node')
    if not exe: pytest.skip('node not installed')
    p = subprocess.run([exe, '-e', script, *args], text=True, capture_output=True)
    assert p.returncode == 0, p.stderr
    return p.stdout


@pytest.mark.parametrize('source', ['<script>alert(1)</script>', '[x](javascript:alert(1))',
    '[x](JaVaScRiPt:alert(1))', '[x](java\tscript:alert(1))', '[x](java&#115;cript:alert(1))',
    '[x](javascript&#58;alert(1))', '[x](//evil.test/x)', '[x](data:text/html,evil)',
    '[x](https://x/" onclick="alert(1))', '```html\n<script>alert(1)</script>\n```'])
def test_renderer_untrusted_html(source):
    html = node('const r=require(process.argv[1]);process.stdout.write(r(process.argv[2]));', str(STATIC/'md.js'), source)
    from html.parser import HTMLParser
    class Check(HTMLParser):
        def handle_starttag(self, tag, attrs):
            assert tag not in ('script', 'img', 'iframe', 'style')
            for k, v in attrs:
                assert not k.lower().startswith('on')
                if k == 'href':
                    compact = ''.join(v.split()).lower()
                    assert not compact.startswith(('javascript:', 'data:', '//'))
    Check().feed(html)
    if '<script>' in source: assert '&lt;script&gt;' in html


def test_renderer_features_and_exports():
    source = '# Title\n\nhello **bold** *em* `code` [doc](docs/a.md)\n\n- a\n  - b\n\n> quote\n\n---\n\n|a|b|\n|---|---|\n|1|2|\n\n```py\nx < y\n```'
    html = node('const r=require(process.argv[1]);process.stdout.write(r(process.argv[2]));', str(STATIC/'md.js'), source)
    for tag in ['h1','strong','em','code','a','ul','blockquote','hr','table','pre']:
        assert '<'+tag in html
    assert '&lt;' in html and html.count('<ul>') == 2
    node("const vm=require('vm'),fs=require('fs');const c={window:{}};vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),c);if(typeof c.window.renderMarkdown!=='function')throw Error('export');", str(STATIC/'md.js'))


def test_document_controller_poll_navigation_and_races():
    node(r"""
const assert=require('assert'), r=require(process.argv[1]);
(async()=>{
 let interval,cleared=0,rows,body,path,mtime=1,requests=[];
 const pending={};
 const fetch=async url=>{
   requests.push(url);
   if(url.startsWith('/api/docs')) return [{path:'docs/a.md',mtime},{path:'.duet/memory/a.md',mtime:1},{path:'DIALOGUE.md',mtime:1}];
   const p=new URL(url,'http://test').searchParams.get('path');
   if(p==='slow.md') return await new Promise(resolve=>pending.slow=resolve);
   return {path:p,content:'# '+p,mtime};
 };
 const v=r.createViewer({fetch,onList:x=>rows=x,onDocument:d=>{body=d.content;path=d.path;},onError:e=>{throw e;},
   setInterval:(f,ms)=>{assert.equal(ms,10000);interval=f;return 7;},clearInterval:()=>cleared++});
 assert(!interval);await v.setOpen(true);assert(interval);
 assert.deepEqual(rows.map(x=>x.path),['DIALOGUE.md','docs/a.md','.duet/memory/a.md']);
 v.filter('docs/');assert.deepEqual(rows.map(x=>x.path),['docs/a.md']);
 await v.select('docs/a.md');assert.equal(path,'docs/a.md');
 assert.equal(v.relative('../DIALOGUE.md'),'DIALOGUE.md');
 assert.equal(v.relative('../../escape.md'),null);
 assert.equal(v.relative('javascript:bad.md'),null);
 let slow=v.select('slow.md');await v.select('DIALOGUE.md');pending.slow({path:'slow.md',content:'old',mtime:1});await slow;
 assert.equal(path,'DIALOGUE.md');
 mtime=2;await interval();assert.equal(body,'# DIALOGUE.md');
 await v.setOpen(false);assert.equal(cleared,1);
 const n=requests.length;await interval();assert.equal(requests.length,n);
})().catch(e=>{console.error(e);process.exitCode=1});
""", str(STATIC/'md.js'))
