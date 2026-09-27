(function(root) {
  'use strict';
  function clock(server, received) { return now => server + Math.max(0, now-received); }
  function describe(a, now) {
    a=a||{state:'idle'};
    const age=Math.max(0,Math.floor(now-(a.last_event_at == null ? now : a.last_event_at)));
    const elapsed=Math.max(0,Math.floor(now-(a.turn_started_at == null ? now : a.turn_started_at)));
    let text, color;
    if(a.state==='awaiting_approval') { text='승인 대기'; color='purple'; }
    else if(['idle','done','error'].includes(a.state)) { text={idle:'대기',done:'완료',error:'오류'}[a.state];color=a.state==='error'?'red':'muted'; }
    else if(age>=300) { text='멈춘 것 같음 — /stop 또는 재시작'; color='red'; }
    else if(age>=60) { text=`응답 대기 중 (마지막 활동 ${age}초 전)`;color='yellow'; }
    else { text='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏'[elapsed%10]+' 처리 중';color='green'; }
    if(!['idle','done','error'].includes(a.state)) text+=` · ${elapsed}초`;
    if(a.detail) text+=' · '+a.detail;
    return {text,color};
  }
  const api={clock,describe};
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.DuetActivity=api;
})(typeof window!=='undefined'?window:null);
