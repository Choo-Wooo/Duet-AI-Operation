"""Reconnects must neither discard user input nor revive revoked access."""
from duet.tests.test_markdown import STATIC, node


def test_disconnected_message_and_question_remain_in_editor():
    node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8'),nodes={
 '#input':{value:'작성 중인 요청'},'#target':{value:'explorer'},
 '#askInput':{value:'작성 중인 질문'},'#askRole':{value:'critic'},
};
const notices=[],sent=[];
const ctx={$:s=>nodes[s],send:m=>false,toast:m=>notices.push(m)};
vm.createContext(ctx);
vm.runInContext(html.slice(html.indexOf('function sendInput('),html.indexOf('$("#send").onclick')),ctx);
vm.runInContext(html.slice(html.indexOf('function sendAsk()'),html.indexOf('// ---------------- 모달')),ctx);
ctx.sendInput();ctx.sendAsk();
assert.equal(nodes['#input'].value,'작성 중인 요청');
assert.equal(nodes['#askInput'].value,'작성 중인 질문');assert.equal(notices.length,2);
ctx.send=m=>{sent.push(m);return true;};ctx.sendInput();
assert.equal(nodes['#input'].value,'');assert.equal(sent[0].text,'작성 중인 요청');
""", str(STATIC / 'index.html'))


def test_socket_waits_for_cookie_confirmation_and_stops_after_revocation():
    node(r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(process.argv[1],'utf8'),nodes={},sockets=[],timers=[];
let release,checks=0,forgotten=0;
const ctx={
 $:s=>nodes[s]||(nodes[s]={classList:{add(){},remove(){},contains(){return false;}},replaceChildren(){}}),
 auth:{authorize(){checks++;return new Promise(r=>release=r);},socketURL(){return 'ws://duet/ws';},forget(){forgotten++;}},
 WebSocket:class {constructor(url){this.url=url;this.readyState=0;sockets.push(this);}close(){this.readyState=3;}},
 setTimeout(fn,ms){timers.push([fn,ms]);return timers.length;},clearTimeout(){},
 conventionEditor:{},onMessage(){},loadDevices(){},
};
vm.createContext(ctx);
vm.runInContext('let ws, connecting=false, retryTimer=null, retryDelay=1500, authBlocked=false;'+html.slice(html.indexOf('function authNotice('),html.indexOf('function onMessage(')),ctx);
(async()=>{
 const connection=ctx.connect();await Promise.resolve();assert.equal(sockets.length,0);
 await ctx.connect();assert.equal(checks,1); // no duplicate sockets while auth is pending
 release({trusted:true});await connection;
 const socket=sockets[0];assert.equal(socket.url,'ws://duet/ws');socket.readyState=1;socket.onopen();
 socket.readyState=3;socket.onclose({code:4001});
 assert.equal(forgotten,1);assert.equal(timers.length,0);
 await ctx.connect();assert.equal(checks,1);
 assert.equal(nodes['#authNotice'].hidden,false);assert(nodes['#authNoticeText'].textContent.includes('해제'));
})().catch(e=>{console.error(e);process.exitCode=1;});
""", str(STATIC / 'index.html'))
