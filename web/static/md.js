(function (root) {
  'use strict';
  const escape = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  // Reject ambiguous URLs before generating any attribute. Entity-looking input
  // is never interpreted as a URL; escaped text remains visible to the reader.
  function safeURL(url) {
    if (!url || /[\s\u0000-\u001f\u007f<>"'\\]/.test(url) || /&(?:#\w+|\w+);/.test(url)) return false;
    if (/^https?:\/\//i.test(url)) return true;
    return !/^(?:[a-z][a-z\d+.-]*:|\/)/i.test(url);
  }
  function inline(raw) {
    // Escape all source text first. Only this function's fixed markup survives.
    let text = escape(raw), stored = [];
    const token = html => { stored.push(html); return '\u0000' + (stored.length-1) + '\u0000'; };
    text = text.replace(/\u0000/g, '');
    text = text.replace(/(`+)([\s\S]*?)\1/g, (_, fence, body) => token('<code>'+body+'</code>'));
    text = text.replace(/\[([^\]\n]+)\]\(([^\n]*?)\)/g, (all, label, encoded) => {
      const url = encoded.replace(/&quot;/g,'"').replace(/&#39;/g,"'").replace(/&lt;/g,'<').replace(/&gt;/g,'>').replace(/&amp;/g,'&');
      return safeURL(url) ? token('<a href="'+escape(url)+'">'+label+'</a>') : label;
    });
    text = text.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>').replace(/\*([^*]+)\*/g,'<em>$1</em>');
    // Stored fragments may reference earlier code tokens in link labels.
    const restore = t => t.replace(/\u0000(\d+)\u0000/g, (_, n) => restore(stored[+n] || ''));
    return restore(text);
  }
  function renderMarkdown(source) {
    const lines = String(source).replace(/\r\n?/g,'\n').split('\n');
    let out = [], i = 0;
    const fence = s => /^ {0,3}(`{3,}|~{3,})(.*)$/.exec(s);
    const list = s => /^( *)([-+*]|\d+\.)\s+(.+)$/.exec(s);
    const cells = s => s.trim().replace(/^\||\|$/g,'').split('|').map(x=>x.trim());
    while (i < lines.length) {
      let line = lines[i], m;
      if (!line.trim()) { i++; continue; }
      if ((m=fence(line))) {
        const marker=m[1], body=[]; i++;
        while(i<lines.length) {
          const close=fence(lines[i]);
          if(close && close[1][0]===marker[0] && close[1].length>=marker.length && !close[2].trim()) { i++; break; }
          body.push(lines[i++]);
        }
        out.push('<pre><code>'+escape(body.join('\n'))+'</code></pre>'); continue;
      }
      if ((m=/^ {0,3}(#{1,6})\s+(.+)$/.exec(line))) { out.push('<h'+m[1].length+'>'+inline(m[2])+'</h'+m[1].length+'>'); i++; continue; }
      if (/^\s*(?:---+|\*\*\*+|___+)\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
      if (/^\s*>/.test(line)) {
        const q=[]; while(i<lines.length && /^\s*>/.test(lines[i])) q.push(lines[i++].replace(/^\s*> ?/,''));
        out.push('<blockquote>'+renderMarkdown(q.join('\n'))+'</blockquote>'); continue;
      }
      if (list(line)) {
        function renderList(base, depth) {
          const first=list(lines[i]), tag=/\d/.test(first[2])?'ol':'ul';
          let result='<'+tag+'>', open=false;
          while(i<lines.length) {
            const item=list(lines[i]); if(!item || item[1].length<base) break;
            if(item[1].length>base && depth<1 && open) { result+=renderList(item[1].length, depth+1); continue; }
            if(item[1].length===base && (/\d/.test(item[2])?'ol':'ul')!==tag) break;
            if(open) result+='</li>';
            result+='<li>'+inline(item[3]); open=true; i++;
          }
          return result+(open?'</li>':'')+'</'+tag+'>';
        }
        out.push(renderList(list(line)[1].length,0)); continue;
      }
      if(line.includes('|') && i+1<lines.length && cells(lines[i+1]).every(x=>/^:?-{3,}:?$/.test(x))) {
        const headers=cells(line); i+=2;
        let table='<table><thead><tr>'+headers.map(x=>'<th>'+inline(x)+'</th>').join('')+'</tr></thead><tbody>';
        while(i<lines.length && lines[i].includes('|') && lines[i].trim()) table+='<tr>'+cells(lines[i++]).map(x=>'<td>'+inline(x)+'</td>').join('')+'</tr>';
        out.push('<div class="tablewrap">'+table+'</tbody></table></div>'); continue;
      }
      const p=[line]; i++;
      while(i<lines.length && lines[i].trim() && !fence(lines[i]) && !list(lines[i]) && !/^\s*(?:#|>|---)/.test(lines[i])) p.push(lines[i++]);
      out.push('<p>'+inline(p.join('\n'))+'</p>');
    }
    return out.join('\n');
  }
  renderMarkdown.safeURL = safeURL;
  renderMarkdown.createViewer = function(options) {
    let rows=[], query='', selected=null, stamp=null, generation=0, opened=false, timer=null, listGeneration=0;
    const group=p=>p==='DIALOGUE.md'?0:p.startsWith('docs/')?1:p.startsWith('.duet/memory/')?2:3;
    const paint=()=>options.onList(rows.filter(x=>x.path.toLowerCase().includes(query)).slice().sort((a,b)=>group(a.path)-group(b.path)||b.mtime-a.mtime||a.path.localeCompare(b.path)));
    async function select(path) {
      selected=path;const ticket=++generation;
      try {
        const doc=await options.fetch('/api/doc?path='+encodeURIComponent(path));
        if(ticket!==generation)return;
        stamp=doc.mtime;options.onDocument(doc);
      } catch(e) { if(ticket===generation)options.onError(e); }
    }
    async function refresh(force=false) {
      const ticket=++listGeneration;
      try {
        const fresh=await options.fetch('/api/docs');
        if(ticket!==listGeneration)return;
        rows=fresh;paint();
        if(selected) {
          const found=rows.find(x=>x.path===selected);
          if(!found) { generation++;stamp=null;options.onError(new Error('문서가 삭제되었거나 접근할 수 없습니다')); }
          else if(force||found.mtime!==stamp)await select(selected);
        }
      } catch(e) { if(ticket===listGeneration)options.onError(e); }
    }
    function relative(href) {
      if(!safeURL(href)||/^[a-z][a-z\d+.-]*:/i.test(href))return null;
      let decoded;
      try { decoded=decodeURIComponent(href.split(/[?#]/)[0]); } catch(e) { return null; }
      if(!decoded.endsWith('.md')||decoded.startsWith('/')||decoded.includes('\\'))return null;
      const parts=(selected||'').split('/').slice(0,-1);
      for(const part of decoded.split('/')) {
        if(part==='..') { if(!parts.length)return null;parts.pop(); }
        else if(part&&part!=='.')parts.push(part);
      }
      return parts.join('/');
    }
    return {select,refresh,relative,filter(q){query=q.toLowerCase();paint();},async setOpen(value){
      if(value===opened)return;opened=value;
      if(timer!==null){options.clearInterval(timer);timer=null;}
      if(opened){timer=options.setInterval(()=>opened?refresh():Promise.resolve(),10000);await refresh();}
      else {listGeneration++;generation++;}
    }};
  };
  if (typeof module === 'object' && module.exports) module.exports=renderMarkdown;
  if (root) root.renderMarkdown=renderMarkdown;
})(typeof window !== 'undefined' ? window : null);
