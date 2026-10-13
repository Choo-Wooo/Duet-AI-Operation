"""Browser enrollment must survive reload without exposing credentials to storage."""
from duet.tests.test_markdown import STATIC, node


def test_updated_assets_keep_authenticated_older_server_connected_until_restart():
    node(r"""
const assert=require('assert'),{DuetAuth}=require(process.argv[1]);
(async()=>{
 for(const token of ['', '?t=legacy-token']) {
  const calls=[];
  const env={location:new URL('http://duet.test/'+token),history:{replaceState(){throw Error('must retain URL');}},
   fetch:async path=>{calls.push(path);return path==='/api/session'?{ok:false,status:404}:{ok:true};}};
  const auth=DuetAuth.create(env),session=await auth.authorize();
  assert.equal(session.legacy,true);assert.equal(session.trusted,false);
  assert.equal(calls[1],'/api/state'+token);assert.equal(auth.socketURL(),'ws://duet.test/ws'+token);
  env.fetch=async path=>({ok:false,status:path==='/api/session'?404:403});
  await assert.rejects(auth.authorize(),e=>e.status===403);
 }
})().catch(e=>{console.error(e);process.exitCode=1});
""", str(STATIC / 'auth.js'))


def test_auth_token_is_removed_only_after_cookie_confirmation():
    node(r"""
const assert=require('assert'),{DuetAuth}=require(process.argv[1]);
(async()=>{
 const calls=[],edits=[];
 let release;
 const location=new URL('http://tailscale-host:8765/?t=secret&view=docs#section');
 const env={location,history:{state:{view:'docs'},replaceState:(...v)=>edits.push(v)},
  fetch:(path,options)=>{calls.push({path,options});return new Promise(r=>release=r);}};
 const auth=DuetAuth.create(env),pending=auth.authorize();
 assert(auth.socketURL().endsWith('/ws?t=secret'));
 assert.equal(edits.length,0);
 assert.equal(calls[0].path,'/api/session');
 assert.equal(calls[0].options.credentials,'same-origin');
 assert.equal(calls[0].options.cache,'no-store');
 release({ok:true,json:async()=>({authenticated:true,trusted:true,device:{id:'example'}})});
 await pending;
 assert.equal(auth.trusted,true);
 assert.equal(auth.socketURL(),'ws://tailscale-host:8765/ws');
 assert.deepEqual(edits,[[{view:'docs'},'', '/?view=docs#section']]);
 env.fetch=async(path,options)=>{calls.push({path,options});return {ok:true,json:async()=>({content:'doc'})};};
 await auth.json('/api/doc?path=README.md');
 assert.equal(calls.at(-1).path,'/api/doc?path=README.md');
})().catch(e=>{console.error(e);process.exitCode=1});
""", str(STATIC / "auth.js"))


def test_cookie_blocking_keeps_original_token_for_reload_and_requests():
    node(r"""
const assert=require('assert'),{DuetAuth}=require(process.argv[1]);
(async()=>{
 const calls=[],edits=[];
 const env={location:new URL('https://duet.example/?t=a%2Bb&view=live'),
  history:{replaceState:(...v)=>edits.push(v)},fetch:async(path,options)=>{
    calls.push({path,options});
    return path==='/api/session'?{ok:false,status:403}:{ok:true,json:async()=>({})};
  }};
 const auth=DuetAuth.create(env),result=await auth.authorize();
 assert.equal(result.authenticated,true);assert.equal(result.trusted,false);
 assert.equal(auth.trusted,false);assert.equal(edits.length,0);
 assert.deepEqual(calls.map(x=>x.path),['/api/session','/api/state?t=a%2Bb']);
 assert.equal(auth.socketURL(),'wss://duet.example/ws?t=a%2Bb');
 await auth.json('/api/doc?path=docs%2Fexample.md');
 const last=new URL(calls.at(-1).path,env.location);
 assert.equal(last.searchParams.get('path'),'docs/example.md');
 assert.equal(last.searchParams.get('t'),'a+b');
 auth.forget();assert.equal(auth.socketURL(),'wss://duet.example/ws');
})().catch(e=>{console.error(e);process.exitCode=1});
""", str(STATIC / "auth.js"))


def test_revoked_or_unavailable_session_does_not_silently_discard_link():
    node(r"""
const assert=require('assert'),{DuetAuth}=require(process.argv[1]);
(async()=>{
 for (const scenario of [
  {status:403,token:'',expected:403},
  {status:503,token:'secret',expected:503},
  {status:200,token:'secret',expected:503,body:{authenticated:true,trusted:false}},
  {status:403,token:'secret',expected:403},
 ]) {
  const calls=[],edits=[];
  const env={location:new URL('http://duet.test/'+(scenario.token?'?t='+scenario.token:'')),
   history:{replaceState:(...v)=>edits.push(v)},fetch:async path=>{
    calls.push(path);return {ok:scenario.status===200,status:scenario.status,json:async()=>scenario.body};
   }};
  const auth=DuetAuth.create(env);
  await assert.rejects(auth.authorize(),e=>e instanceof DuetAuth.AuthError&&e.status===scenario.expected);
  assert.equal(edits.length,0);assert.equal(auth.trusted,false);
  assert.equal(calls.length,scenario.status===403&&scenario.token?2:1);
  if(scenario.token)assert(auth.socketURL().includes('?t=secret'));
 }
})().catch(e=>{console.error(e);process.exitCode=1});
""", str(STATIC / "auth.js"))


def test_authenticated_fetch_failure_is_explicit_and_preserves_post_options():
    node(r"""
const assert=require('assert'),{DuetAuth}=require(process.argv[1]);
(async()=>{
 const calls=[];
 const env={location:new URL('https://duet.test/'),history:{replaceState(){}},fetch:async(path,options)=>{
  calls.push({path,options});return {ok:false,status:403};
 }};
 const auth=DuetAuth.create(env);
 await assert.rejects(auth.json('/api/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}),e=>e.status===403);
 assert.equal(calls[0].options.method,'POST');assert.equal(calls[0].options.body,'{}');
 assert.equal(calls[0].options.headers['Content-Type'],'application/json');
 assert.equal(calls[0].options.credentials,'same-origin');
 assert(calls[0].options.signal instanceof AbortSignal);
})().catch(e=>{console.error(e);process.exitCode=1});
""", str(STATIC / "auth.js"))
