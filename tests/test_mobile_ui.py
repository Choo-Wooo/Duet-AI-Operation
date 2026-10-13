"""Phone navigation shares existing controls and survives viewport changes."""
from html.parser import HTMLParser

from duet.tests.test_markdown import STATIC, node


HARNESS = r"""
const assert = require('assert'), mobile = require(process.argv[1]);
class Element {
  constructor(id='') {
    this.id=id; this.attrs={}; this.dataset={}; this.children=[]; this.listeners={};
    this.style={values:{},setProperty(k,v){this.values[k]=v;},removeProperty(k){delete this.values[k];}};
    this.classes=new Set(); this.classList={add:k=>this.classes.add(k),remove:k=>this.classes.delete(k),
      toggle:(k,v)=>v?this.classes.add(k):this.classes.delete(k),contains:k=>this.classes.has(k)};
  }
  appendChild(child) { return this.insertBefore(child,null); }
  insertBefore(child,before) {
    if(child===before)return child;
    if(child.parentNode){const previous=child.parentNode.children;previous.splice(previous.indexOf(child),1);}
    const i=before?this.children.indexOf(before):this.children.length;
    this.children.splice(i,0,child);child.parentNode=this;return child;
  }
  get nextSibling(){return this.parentNode?.children[this.parentNode.children.indexOf(this)+1]||null;}
  setAttribute(k,v){this.attrs[k]=v;}
  removeAttribute(k){delete this.attrs[k];}
  addEventListener(k,fn,capture){(this.listeners[k]||=[]).push({fn,capture});}
  fire(k,event={}){for(const {fn} of this.listeners[k]||[])fn({target:this,...event});}
  click(){if(this.onclick)this.onclick();this.fire('click');}
  focus(){this.focused=true;}
  showModal(){this.open=true;}
  close(){this.open=false;}
}
const nodes={},get=id=>nodes[id]||(nodes[id]=new Element(id)),header=new Element('header'),nav=new Element('nav');
const moved=['conversationMode','designQuestionsBadge','autopilot','turns','cost','ctx'];
for(const id of moved)(id==='ctx'?nav:header).appendChild(get(id));
const root=new Element('html'),media={matches:false,listeners:[],addEventListener(k,fn){this.listeners.push(fn);}},frames=[],resizeObservers=[];
const tabs=['flow','stream'].map(name=>{const el=get('tab-'+name);el.dataset.livePanel=name;return el;});
const views=['live','work','docs','ask','sessions','roles','settings'];
const buttons=views.map(view=>{const el=get('mobile-'+view);el.dataset.mobileView=view;return el;});
const visited=[];
for(const view of views)get('nav-'+view).onclick=()=>{visited.push(view);controller.viewChanged(view);};
get('live').dataset.mobilePanel='flow';
for(const id of ['input','askInput']){get(id).placeholder='desktop '+id;get(id).tagName='TEXTAREA';}
const doc={documentElement:root,activeElement:null,listeners:{},
  querySelector(selector){
    if(selector==='.live')return get('live');
    if(selector==='.app > header')return header;
    if(selector==='#docList .sel')return get('selected-document');
    const match=selector.match(/^#nav button\[data-v="(.+)"\]$/);
    return get(match?'nav-'+match[1]:selector.slice(1));
  },
  querySelectorAll(selector){return selector==='[data-live-panel]'?tabs:buttons;},
  createComment(text){return new Element(text);},
  addEventListener(k,fn){this.listeners[k]=fn;}
};
const win={innerHeight:844,visualViewport:{height:844,offsetTop:0,scale:1,addEventListener(){}},
  matchMedia:()=>media,addEventListener(){},requestAnimationFrame(fn){frames.push(fn);return frames.length;},
  ResizeObserver:class {constructor(fn){resizeObservers.push(fn);}observe(){}}};
const flush=()=>{while(frames.length)frames.shift()();};
const resize=compact=>{media.matches=compact;media.listeners.forEach(fn=>fn());flush();};
const controller=mobile.mount(doc,win);flush();
"""


def test_mobile_assets_and_accessible_controls_are_connected():
    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = []

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                self.ids.append(attrs["id"])

    html = (STATIC / "index.html").read_text()
    elements = Elements()
    elements.feed(html)
    assert len(elements.ids) == len(set(elements.ids))
    assert {"mobileNav", "mobileMoreDialog", "mobileLiveTabs", "mobileDocBack", "flowPanel", "streamPanel"} <= set(elements.ids)
    assert html.index('/static/mobile.css') > html.rindex('</style>')
    assert html.index('/static/mobile.js') > html.rindex('connect();')
    assert 'viewport-fit=cover' in html
    assert 'data-label="컨텍스트 한도"' in html
    css = (STATIC / "mobile.css").read_text()
    assert '@media (max-width: 860px)' in css
    assert 'env(safe-area-inset-bottom' in css
    assert '.mobile-only { display: none; }' in css


def test_viewport_keyboard_adjustment_does_not_treat_pinch_zoom_as_a_keyboard():
    node(r"""
const assert=require('assert'),{viewportState:v}=require(process.argv[1]);
assert.deepEqual(v(844,null,false),{height:844,top:0,bottom:0,keyboard:false});
assert.deepEqual(v(844,{height:480,offsetTop:30,scale:1},true),{height:480,top:30,bottom:334,keyboard:true});
assert.equal(v(844,{height:780,offsetTop:0,scale:1},true).keyboard,false); // browser chrome
assert.equal(v(844,{height:480,offsetTop:30,scale:1},false).keyboard,false);
assert.deepEqual(v(844,{height:422,offsetTop:100,scale:2},true),{height:844,top:0,bottom:0,keyboard:false});
""", str(STATIC / "mobile.js"))


def test_mobile_breakpoint_moves_live_controls_and_restores_desktop_exactly():
    node(HARNESS + r"""
const original=header.children.filter(e=>!e.id.startsWith('mobile-home:')).map(e=>e.id);
assert.equal(get('mode').parentNode,undefined); // no duplicated mode select is created
resize(true);
for(const id of moved)assert.equal(get(id).parentNode,get('mobileSessionOptions'));
assert.equal(root.style.values['--mobile-viewport-h'],'844px');
assert.equal(get('flowPanel').attrs.role,'tabpanel');
get('mobileMoreButton').click();assert.equal(get('mobileMoreDialog').open,true);
resize(false);
assert.equal(get('mobileMoreDialog').open,false);
assert.deepEqual(header.children.filter(e=>!e.id.startsWith('mobile-home:')).map(e=>e.id),original);
assert.equal(get('ctx').parentNode,nav);
assert.equal(root.style.values['--mobile-viewport-h'],undefined);
assert.equal(get('flowPanel').attrs.role,undefined);
assert.equal(get('input').placeholder,'desktop input');
resize(true);resize(false);
assert.deepEqual(header.children.filter(e=>!e.id.startsWith('mobile-home:')).map(e=>e.id),original);
""", str(STATIC / "mobile.js"))


def test_mobile_navigation_delegates_to_existing_views_and_closes_more():
    node(HARNESS + r"""
resize(true);
get('mobileMoreButton').click();assert.equal(get('mobileMoreDialog').open,true);
get('mobile-roles').click();
assert.deepEqual(visited,['roles']);
assert.equal(get('mobileMoreDialog').open,false);
assert.equal(get('mobileMoreButton').attrs['aria-current'],'page');
assert.equal(get('mobileMoreLabel').textContent,'역할·모델');
get('mobile-docs').click();
assert.equal(get('mobile-docs').attrs['aria-current'],'page');
assert.equal(get('mobileMoreButton').attrs['aria-current'],undefined);
assert.equal(get('mobileMoreLabel').textContent,'더보기');
controller.openDocument();assert(get('v-docs').classList.contains('mobile-document-open'));
get('mobileDocBack').click();assert(!get('v-docs').classList.contains('mobile-document-open'));
assert.equal(get('selected-document').focused,true);
// A document polling update never calls openDocument and cannot undo Back.
controller.viewChanged('docs');assert(!get('v-docs').classList.contains('mobile-document-open'));
""", str(STATIC / "mobile.js"))


def test_mobile_live_tabs_and_return_key_keep_phone_composition_safe():
    node(HARNESS + r"""
resize(true);tabs[1].click();
assert.equal(get('live').dataset.mobilePanel,'stream');
assert.equal(tabs[0].attrs['aria-selected'],'false');assert.equal(tabs[1].attrs['aria-selected'],'true');
let prevented=false;tabs[1].fire('keydown',{key:'ArrowLeft',preventDefault(){prevented=true;}});
assert(prevented);assert.equal(get('live').dataset.mobilePanel,'flow');assert.equal(tabs[0].focused,true);
let stopped=false;
get('input').fire('keydown',{key:'Enter',stopImmediatePropagation(){stopped=true;}});
assert(stopped);assert.equal(get('input').listeners.keydown[0].capture,true);
stopped=false;get('input').fire('keydown',{key:'Enter',ctrlKey:true,stopImmediatePropagation(){stopped=true;}});assert(!stopped);
resize(false);get('input').fire('keydown',{key:'Enter',stopImmediatePropagation(){stopped=true;}});assert(!stopped);
resize(true);doc.activeElement=get('input');win.visualViewport.height=480;
doc.listeners.focusin();flush();
assert(root.classList.contains('keyboard-open'));assert.equal(root.style.values['--mobile-viewport-h'],'480px');
doc.activeElement=null;doc.listeners.focusout();flush();assert(!root.classList.contains('keyboard-open'));
""", str(STATIC / "mobile.js"))


def test_mobile_approval_tray_reserves_scroll_space_without_affecting_desktop():
    node(HARNESS + r"""
header.offsetHeight=133;resize(true);get('pendingTray').offsetHeight=180;resizeObservers.forEach(fn=>fn());
assert.equal(root.style.values['--mobile-header-h'],'133px');
assert.equal(root.style.values['--mobile-tray-space'],'204px');
header.offsetHeight=107;resizeObservers.forEach(fn=>fn());
assert.equal(root.style.values['--mobile-header-h'],'107px');
get('pendingTray').offsetHeight=48;resizeObservers.forEach(fn=>fn());
assert.equal(root.style.values['--mobile-tray-space'],'72px');
get('pendingTray').classList.add('zero');resizeObservers.forEach(fn=>fn());
assert.equal(root.style.values['--mobile-tray-space'],'0px');
get('pendingTray').classList.remove('zero');resize(false);
assert.equal(root.style.values['--mobile-tray-space'],'0px');
assert.equal(root.style.values['--mobile-header-h'],'0px');
""", str(STATIC / "mobile.js"))
