(function(root) {
  'use strict';
  const LABELS=Object.freeze(['처리 중','응답 대기','멈춤 의심','승인 대기','턴 중단','대기']);
  const SPIN='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏';
  function clock(server, received) { return now => server + Math.max(0, now-received); }
  function describe(a, now) {
    a=a||{state:'idle'};
    const age=Math.max(0,Math.floor(now-(a.last_event_at == null ? now : a.last_event_at)));
    const elapsed=Math.max(0,Math.floor(now-(a.turn_started_at == null ? now : a.turn_started_at)));
    const resting=['idle','done','error'].includes(a.state);
    let label, color, icon;
    if(a.state==='usage_wait') { label='사용량 한도 대기'; color='yellow'; icon='◷'; }
    else if(a.state==='awaiting_approval') { label='승인 대기'; color='purple'; icon='◆'; }
    else if(a.state==='error') { label='턴 중단'; color='red'; icon='✕'; }
    else if(resting) { label='대기'; color='muted'; icon='·'; }
    else if(age>=300) { label='멈춤 의심'; color='red'; icon='!'; }
    else if(age>=60) { label='응답 대기'; color='yellow'; icon='…'; }
    else { label='처리 중'; color='green'; icon=SPIN[elapsed%10]; }
    const shown=resting?null:elapsed;
    const detail=a.detail?String(a.detail):null;
    const text=label+(shown==null?'':` · ${shown}초`);
    const title=[text,!resting&&age>=60?`마지막 활동 ${age}초 전`:null,detail].filter(Boolean).join(' · ');
    return {icon,label,color,elapsed:shown,detail,title,text};
  }
  function chipParts(role, d) {
    return {text:`${d.icon} ${role} · ${d.text}`, title:`${role} · ${d.title}`};
  }
  const api={clock,describe,chipParts,LABELS};
  if(typeof module==='object'&&module.exports)module.exports=api;
  if(root)root.DuetActivity=api;
})(typeof window!=='undefined'?window:null);
