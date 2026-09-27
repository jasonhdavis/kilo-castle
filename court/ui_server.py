"""Zero-dependency localhost web console for the court pipeline.

Reads: .court quest files, git worktrees, the local kilo session DB (read-only),
and the live process table. Mutating endpoints: /api/reap (terminates a process
whose parent is a verified kilo process) and /api/mcp (flips the enabled flag of
an inventoried MCP server in its own config file, with a .bak backup).
"""

import json
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COURT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KILO_DB = os.path.expanduser("~/.local/share/kilo/kilo.db")
CHILD_PATTERNS = ("mcp", "npx", "npm", "chromium", "chrome", "pytest", "playwright")
MCP_GLOBAL = os.path.expanduser("~/.config/kilo/kilo.jsonc")
MCP_PB_APP = "/Users/scrummage/Python/pb-app"
MCP_RUNNERS = {"npx", "npm", "node", "uvx", "uv", "pipx", "bun", "bunx", "docker",
               "python", "python3", "exec", "run", "x", "start", "install", "i"}
MCP_FLAG_VALUES = {"-e", "-v", "-w", "--env", "--volume", "--workdir"}
STATUS_ORDER = [
    "WORKING", "TRIBUTE_READY", "GATE", "READY_TO_RAZE", "PLANNED", "OPEN",
    "PUNISHED", "ASHES",
]

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Court Console</title>
<style>
:root{
 --bg:#0d1117; --surface:#161b22; --surface-2:#1c2129; --edge:#2d333b;
 --edge-soft:rgba(240,246,252,.08); --ink:#e6edf3; --dim:#8b949e; --faint:#6e7681;
 --primary:#ffb300; --primary-ink:#1a1205; --blue:#58a6ff; --green:#3fb950;
 --red:#f85149; --amber:#d29922;
 --sh-1:0 1px 2px rgba(0,0,0,.5); --sh-2:0 8px 24px rgba(0,0,0,.45);
 --r-lg:12px; --r-md:8px; --r-sm:6px;
}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{background:var(--bg);color:var(--ink);
 font:400 13px/1.5 "Inter","Roboto",-apple-system,"Segoe UI",sans-serif;
 display:grid;grid-template-rows:auto 1fr;height:100vh;overflow:hidden}
::-webkit-scrollbar{width:8px;height:8px}
::-webkit-scrollbar-thumb{background:var(--edge);border-radius:4px}
::-webkit-scrollbar-track{background:transparent}

header{display:flex;align-items:center;gap:14px;padding:0 20px;height:56px;
 background:var(--surface);border-bottom:1px solid var(--edge);box-shadow:var(--sh-1);z-index:2}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;font-size:14px;
 letter-spacing:.12em;text-transform:uppercase}
.brand .glyph{width:28px;height:28px;border-radius:var(--r-md);display:grid;place-items:center;
 background:linear-gradient(135deg,#ffb300,#d29922);color:#1a1205;font-size:14px;
 box-shadow:var(--sh-1)}
.brand em{color:var(--primary);font-style:normal}
.vdiv{width:1px;height:24px;background:var(--edge)}
.chip{display:inline-flex;align-items:center;gap:5px;padding:2px 10px;border-radius:999px;
 font-size:11px;font-weight:500;background:var(--surface-2);border:1px solid var(--edge);
 color:var(--dim);white-space:nowrap}
.chip b{color:var(--ink);font-weight:600}
.chip.rss{border-color:rgba(88,166,255,.35)} .chip.rss b{color:var(--blue)}
header .spacer{flex:1}
#clock{color:var(--faint);font-size:11px;font-variant-numeric:tabular-nums}

main{display:grid;grid-template-columns:300px 1fr;overflow:hidden}
nav{overflow-y:auto;background:var(--surface);border-right:1px solid var(--edge);padding:14px 12px}
.castle{position:relative;background:linear-gradient(160deg,#1f242c,#181d24);
 border:1px solid rgba(255,179,0,.25);border-radius:var(--r-lg);padding:12px 14px;
 margin-bottom:14px;cursor:pointer;transition:all .15s ease;box-shadow:var(--sh-1)}
.castle:hover{border-color:rgba(255,179,0,.55);transform:translateY(-1px);box-shadow:var(--sh-2)}
.castle.sel{border-color:var(--primary);box-shadow:0 0 0 1px var(--primary),var(--sh-1)}
.castle .name{font-weight:600;letter-spacing:.04em;display:flex;align-items:center;gap:8px}
.castle .name .dot{width:8px;height:8px;border-radius:50%;background:var(--green);
 box-shadow:0 0 6px var(--green)}
.castle .sub{color:var(--dim);font-size:11px;margin-top:2px}
nav h2{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.14em;
 color:var(--faint);margin:16px 8px 6px;display:flex;align-items:center;gap:8px}
nav h2::after{content:"";flex:1;height:1px;background:var(--edge-soft)}
.q{position:relative;padding:7px 10px 7px 14px;border-radius:var(--r-md);cursor:pointer;
 margin-bottom:2px;transition:background .12s ease}
.q:hover{background:var(--surface-2)}
.q .row1{display:flex;align-items:center;gap:7px;font-weight:500;font-size:12.5px}
.q .row2{color:var(--dim);font-size:11px;margin-top:1px;white-space:nowrap;overflow:hidden;
 text-overflow:ellipsis}
.st{display:inline-flex;align-items:center;padding:1px 8px;border-radius:999px;font-size:9.5px;
 font-weight:600;letter-spacing:.08em;text-transform:uppercase}
.st.WORKING{background:rgba(210,153,34,.15);color:var(--amber)}
.st.TRIBUTE_READY{background:rgba(63,185,80,.15);color:var(--green)}
.st.GATE{background:rgba(88,166,255,.15);color:var(--blue)}
.st.READY_TO_RAZE{background:rgba(248,81,73,.15);color:var(--red)}
.st.PLANNED,.st.OPEN{background:var(--surface-2);color:var(--dim)}
.st.PUNISHED{background:rgba(248,81,73,.25);color:var(--red)}
.badge{font-size:9.5px;padding:1px 7px;border-radius:999px;background:rgba(248,81,73,.12);
 color:var(--red);font-weight:600;letter-spacing:.06em;text-transform:uppercase}

#content{overflow-y:auto;padding:20px 24px;display:flex;flex-direction:column;gap:18px}
.card{background:var(--surface);border:1px solid var(--edge);border-radius:var(--r-lg);
 box-shadow:var(--sh-1);overflow:hidden}
.card .hd{display:flex;align-items:center;gap:10px;padding:14px 18px 10px}
.card .hd h3{font-size:13.5px;font-weight:600;letter-spacing:.02em}
.card .hd .branch{font-family:ui-monospace,Menlo,monospace;font-size:11px;color:var(--dim);
 background:var(--surface-2);border:1px solid var(--edge);border-radius:var(--r-sm);
 padding:2px 8px}
.card .meta{padding:0 18px 12px;color:var(--faint);font-size:11px}
.card .bd{padding:0 18px 16px}
.card.empty{padding:26px;text-align:center;color:var(--faint);border-style:dashed;
 background:transparent;box-shadow:none}

.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}
.tab{padding:4px 14px;border:1px solid var(--edge);border-radius:999px;cursor:pointer;
 font-size:11.5px;color:var(--dim);background:var(--surface-2);max-width:260px;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:all .12s ease}
.tab:hover{border-color:var(--blue);color:var(--ink)}
.tab.on{background:var(--blue);border-color:var(--blue);color:#0d1117;font-weight:600}

table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:6px 10px;color:var(--faint);font-size:10px;font-weight:600;
 text-transform:uppercase;letter-spacing:.1em;border-bottom:1px solid var(--edge)}
td{padding:8px 10px;border-bottom:1px solid var(--edge-soft)}
tbody tr{cursor:pointer;transition:background .1s ease}
tbody tr:hover{background:var(--surface-2)}
tbody tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:11px}
.dim{color:var(--dim)} .blue{color:var(--blue)} .num{font-variant-numeric:tabular-nums}

button{background:var(--surface-2);border:1px solid var(--edge);color:var(--dim);
 border-radius:var(--r-sm);padding:3px 12px;cursor:pointer;font-size:10.5px;font-weight:600;
 letter-spacing:.06em;text-transform:uppercase;transition:all .12s ease}
button:hover{border-color:var(--red);color:var(--red);background:rgba(248,81,73,.08)}

#drawer{position:fixed;right:0;top:0;bottom:0;width:48%;max-width:720px;background:var(--surface);
 border-left:1px solid var(--edge);display:none;flex-direction:column;z-index:10;
 box-shadow:var(--sh-2)}

.dupwarn{display:flex;align-items:center;gap:8px;background:rgba(248,81,73,.1);color:var(--red);
 border:1px solid rgba(248,81,73,.35);border-radius:var(--r-sm);padding:6px 12px;
 margin-bottom:10px;font-size:11.5px;font-weight:500}
#drawer.on{display:flex}
#drawer .hd{padding:16px 20px;border-bottom:1px solid var(--edge);display:flex;
 justify-content:space-between;align-items:center}
#drawer .hd b{color:var(--primary);font-size:13px;letter-spacing:.02em}
#drawer .hd span{cursor:pointer;color:var(--dim);font-size:12px;padding:4px 10px;
 border-radius:var(--r-sm)}
#drawer .hd span:hover{background:var(--surface-2);color:var(--ink)}
#drawer .body{overflow-y:auto;padding:14px 20px}
.msg{margin-bottom:10px;padding:10px 14px;border-radius:var(--r-md);background:var(--bg);
 border:1px solid var(--edge-soft);white-space:pre-wrap;word-break:break-word;font-size:12.5px}
.msg.user{border-left:3px solid var(--primary)}
.msg.assistant{border-left:3px solid var(--blue)}
.msg .who{color:var(--faint);font-size:10px;text-transform:uppercase;letter-spacing:.1em;
 margin-bottom:4px;font-weight:600}
.repohead{font-size:10px;font-weight:700;letter-spacing:.16em;color:var(--primary);
 margin:14px 6px 6px;display:flex;align-items:center;gap:8px}
.repohead:first-child{margin-top:0}
.repohead::after{content:"";flex:1;height:1px;background:rgba(255,179,0,.25)}

.empty-note{color:var(--faint);text-align:center;padding:30px;font-size:12px}

#composer{position:fixed;left:0;right:0;bottom:0;z-index:8;background:var(--surface);
 border-top:1px solid var(--edge);box-shadow:0 -4px 16px rgba(0,0,0,.35);
 padding:10px 20px;display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:flex-end}
#composer .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
#composer select,#composer textarea{background:var(--bg);border:1px solid var(--edge);
 color:var(--ink);border-radius:var(--r-sm);font:12px/1.4 "Inter",sans-serif;padding:6px 8px}
#composer select{cursor:pointer}
#composer textarea{width:100%;resize:none;height:44px;max-height:120px}
#composer textarea:focus,#composer select:focus{outline:none;border-color:var(--blue)}
#composer .send{background:var(--primary);border:none;color:var(--primary-ink);
 font-weight:700;padding:9px 22px;border-radius:var(--r-sm);cursor:pointer;
 font-size:12px;letter-spacing:.04em}
#composer .send:disabled{opacity:.45;cursor:default}
#composer .cont{font-size:10.5px;color:var(--dim)}
#composer .cont b{color:var(--blue);font-weight:600}
#content{overflow-y:auto;padding:20px 24px 110px;display:flex;flex-direction:column;gap:18px}
</style></head><body>
<header><div class="brand"><span class="glyph">♜</span>CASTLE <em>CONSOLE</em></div>
<div class="vdiv"></div><div id="totals" style="display:flex;gap:8px"></div>
<div class="spacer"></div><div id="clock"></div></header>
<main><nav id="nav"></nav><div id="content"></div></main>
<div id="composer">
 <div class="row">
  <select id="c_agent"></select>
  <select id="c_dir"></select>
  <span class="cont" id="c_cont">new session</span>
 </div>
 <textarea id="c_prompt" placeholder="message the agent… (Enter to send, Shift+Enter for newline)"></textarea>
 <button class="send" id="c_send" onclick="sendComposer()">SEND</button>
</div>
<div id="drawer"><div class="hd"><b id="dtitle"></b><span onclick="closeDrawer()">CLOSE ✕</span></div>
<div class="body" id="dbody"></div></div>
<script>
let S=null, selWt=null, selSess=null;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const mb=r=>r==null?'—':(r/1048576).toFixed(0)+' MB';
async function poll(){try{S=await (await fetch('/api/state')).json();render()}catch(e){}}
function render(){
 const flagged=(S.processes||[]).length;
 document.getElementById('totals').innerHTML=
  `<span class="chip">sessions <b>${S.sessions.length}</b></span>
   <span class="chip">worktrees <b>${S.worktrees.length}</b></span>
   <span class="chip rss">kilo RSS <b>${mb(S.proc_total_rss)}</b></span>`+
  (flagged?`<span class="chip" style="border-color:rgba(248,81,73,.4)"><b style="color:var(--red)">${flagged} flagged</b></span>`:'');
 document.getElementById('clock').textContent=new Date().toLocaleTimeString();
 const TRUNKS=['main','castle','master','trunk'];
 const nav=document.getElementById('nav');
 let h='';
 for(const repo of S.repos){
  const wts=repo.worktrees;
  const trunk=wts.find(w=>w.branch&&TRUNKS.some(t=>w.branch.endsWith('/'+t)||w.branch===t));
  const rname=repo.name.toUpperCase();
  h+=`<div class="repohead">${rname}</div>`;
  if(trunk){
   h+=`<div class="castle ${selWt===trunk.path?'sel':''}" onclick="pick('${esc(trunk.path)}')">
   <div class="name"><span class="dot"></span>${rname}</div>
   <div class="sub">${esc(trunk.branch||'trunk')} · ${wts.length} worktree${wts.length===1?'':'s'}</div></div>`;
  }
  const wtreeByBranch={}; for(const w of wts){if(w.branch)wtreeByBranch[w.branch]=w}
  const secs={};
  for(const b of repo.branches){
   const ns=b.includes('/')?b.split('/')[0]:'(root)';
   if(TRUNKS.includes(b)||TRUNKS.some(t=>t===ns))continue;
   (secs[ns]??=[]).push(b);
  }
  for(const ns of Object.keys(secs).sort((a,b)=>secs[b].length-secs[a].length)){
   h+=`<h2>${esc(ns)} · ${secs[ns].length}</h2>`;
   for(const b of secs[ns].slice(0,40)){
    const w=wtreeByBranch[b];
    const cls=w?(w.dirty?'<span class="badge">dirty</span>':''):'<span class="st PLANNED">no wt</span>';
    h+=`<div class="q" onclick="pick(${JSON.stringify(w?w.path:b).replace(/"/g,'&quot;')})">
    <div class="row1">${esc(b.includes('/')?b.slice(b.indexOf('/')+1):b)}${cls}</div>
    <div class="row2">${esc(b)}</div></div>`;
   }
   if(secs[ns].length>40)h+=`<div class="q dim" style="cursor:default">… ${secs[ns].length-40} more</div>`;
  }
 }
 nav.innerHTML=h;
 const c=document.getElementById('content');
 const active=S.repos.filter(r=>selWt===null||r.root===selWt||r.worktrees.some(w=>w.path===selWt));
 let cards='';
 for(const repo of (selWt?active:S.repos)){
  const wts=selWt?repo.worktrees.filter(w=>w.path===selWt):repo.worktrees;
  if(!wts.length)continue;
  if(selWt&&selWt.startsWith(repo.root)){cards+=wts.map(w=>{
   const sess=S.sessions.filter(s=>s.directory&&s.directory.startsWith(w.path));
   return cardWt(w,sess);}).join('');}
  else if(!selWt){const trunk=wts.find(w=>w.branch&&TRUNKS.some(t=>w.branch.endsWith('/'+t)||w.branch===t));
   if(trunk){const sess=S.sessions.filter(s=>s.directory&&s.directory.startsWith(trunk.path));
    cards+=cardWt(trunk,sess);}}
 }
 c.innerHTML=(cards||'<div class="card empty">select a branch on the left</div>')+cardMcp();
}
function cardMcp(){
 const list=S.mcp||[];
 let h='<div class="card"><div class="hd"><h3>MCP SERVERS</h3>'
  +'<span class="branch">merged from global + project configs</span></div><div class="bd">';
 for(const d of list.filter(m=>m.type==='local'&&m.enabled&&m.count>=2)){
  h+=`<div class="dupwarn">duplicate MCP spawns detected: ${esc(d.name)} x${d.count} (~${mb(d.rss)} each)</div>`;
 }
 h+='<table><tr><th>name</th><th>scope</th><th>type</th><th>enabled</th><th>running</th><th></th></tr>'+
  list.map(m=>{
   const run=m.running?`<span class="num" style="color:var(--green)">yes</span> <span class="num blue">${mb(m.rss)}</span>`
    :'<span class="dim">no</span>';
   const btn=`<button title="takes effect for sessions started after the change" onclick="mcpToggle(${
    JSON.stringify(m.file).replace(/"/g,'&quot;')},${JSON.stringify(m.name)})">${m.enabled?'disable':'enable'}</button>`;
   return `<tr><td class="mono">${esc(m.name)}</td><td class="dim">${esc(m.scope)}</td><td class="dim">${esc(m.type)}</td>`+
    `<td>${m.enabled?'<span style="color:var(--green)">yes</span>':'no'}</td><td>${run}</td><td>${btn}</td></tr>`;
  }).join('')+'</table>'+
  `<div class="meta" style="padding-top:10px">toggles edit the config file (a .bak copy is kept) and take effect for sessions started after the change</div></div></div>`;
 return h;
}
async function mcpToggle(file,name){
 const m=(S.mcp||[]).find(x=>x.file===file&&x.name===name);
 if(!m)return;
 const r=await fetch('/api/mcp',{method:'POST',body:JSON.stringify({file,name,enabled:!m.enabled})});
 if(!r.ok)alert('refused: '+(await r.text()));else poll();
}
function cardWt(w,sess){
 let h=`<div class="card"><div class="hd"><h3>${esc(w.branch||'trunk')}</h3>
 <span class="branch">${esc(w.path)}</span>${w.dirty?'<span class="badge">dirty</span>':''}</div>`;
 if(!sess.length)return h+'<div class="bd empty-note">no sessions</div></div>';
 h+='<div class="bd"><div class="tabs">'+sess.map((s,i)=>
  `<div class="tab ${selSess===s.id?'on':''}" onclick="openSess('${s.id}')">${esc(s.agent||'?')} · ${esc(s.title||s.id)}</div>`).join('')+'</div>';
 h+='<table><thead><tr><th>session</th><th>agent</th><th>model</th><th>tokens in / out</th><th>updated</th></tr></thead><tbody>'+
  sess.map(s=>`<tr onclick="openSess('${s.id}')"><td>${esc((s.title||s.id).slice(0,42))}</td><td>${esc(s.agent||'')}</td><td class="dim mono">${esc((s.model||'').replace('openrouter/',''))}</td><td class="num">${s.tokens_input??'—'} / ${s.tokens_output??'—'}</td><td class="dim num">${ago(s.time_updated)}</td></tr>`).join('')+'</tbody></table></div></div>';
 return h;
}
function ago(ts){if(!ts)return'';const d=(Date.now()-ts)/1000;
 return d<60?`${d|0}s`:(d<3600?`${d/60|0}m`:(d<86400?`${d/3600|0}h`:`${d/86400|0}d`));}
function pick(wt){selWt=wt===''?null:wt;selSess=null;render()}
async function openSess(id){selSess=id;setCompose(id);render();
 document.getElementById('drawer').classList.add('on');
 document.getElementById('dtitle').textContent='session '+id;
 document.getElementById('dbody').textContent='loading…';
 const msgs=await (await fetch('/api/session?id='+id)).json();
 document.getElementById('dbody').innerHTML=msgs.map(m=>
  `<div class="msg ${esc(m.role)}"><div class="who">${esc(m.role)} · ${ago(m.time_created)} ago</div>${esc(m.text).slice(0,2000)}</div>`).join('')
  ||'<div class="meta">no messages</div>';}
function closeDrawer(){document.getElementById('drawer').classList.remove('on')}
async function reap(pid){
 if(!confirm(`terminate pid ${pid}?`))return;
 const r=await fetch('/api/reap',{method:'POST',body:JSON.stringify({pid})});
 if(!r.ok)alert('refused: '+(await r.text())); else poll();}
let META=null, composing=false, COMPOSE_SID=null;
async function loadMeta(){
 if(META)return;
 try{META=await (await fetch('/api/compose-meta')).json();}catch(e){return;}
 const a=document.getElementById('c_agent'), d=document.getElementById('c_dir');
 a.innerHTML=META.agents.map(x=>`<option>${x}</option>`).join('');
 d.innerHTML=META.dirs.map(x=>`<option value="${esc(x)}">${esc(x.replace('/Users/scrummage/Python/',''))}</option>`).join('');
}
function setCompose(sess){
 loadMeta();
 if(!sess){COMPOSE_SID=null;
  document.getElementById('c_cont').textContent='new session';return;}
 const s=(S.sessions||[]).find(x=>x.id===sess);
 COMPOSE_SID=sess;
 document.getElementById('c_cont').innerHTML=`continuing <b>${esc(sess.slice(0,22))}…</b>`;
 if(s&&s.directory)document.getElementById('c_dir').value=s.directory;
}
async function sendComposer(){
 if(composing)return;
 const ta=document.getElementById('c_prompt');
 const prompt=ta.value.trim();
 if(!prompt)return;
 ta.value='';composing=true;
 document.getElementById('c_send').disabled=true;
 document.getElementById('dtitle').textContent='composer — '+(COMPOSE_SID?'continue':'new session');
 document.getElementById('dbody').innerHTML='<div class="empty-note">dispatching…</div>';
 document.getElementById('drawer').classList.add('on');
 const r=await fetch('/api/send',{method:'POST',body:JSON.stringify({
  dir:document.getElementById('c_dir').value,
  agent:document.getElementById('c_agent').value,
  prompt, session_id:COMPOSE_SID||null})});
 if(!r.ok){alert('refused: '+(await r.text()));composing=false;
  document.getElementById('c_send').disabled=false;return;}
 const {job}=await r.json();
 let seen=0;
 while(true){
  const st=await (await fetch('/api/send/'+job)).json();
  const evs=st.events.slice(seen);seen=st.events.length;
  const body=document.getElementById('dbody');
  if(evs.length){
   if(body.querySelector('.empty-note'))body.innerHTML='';
   for(const e of evs){
    if(e.type==='text')body.insertAdjacentHTML('beforeend',
     `<div class="msg assistant"><div class="who">assistant</div>${esc(e.text)}</div>`);
    else if(e.type==='tool')body.insertAdjacentHTML('beforeend',
     `<div class="msg"><div class="who">tool · ${esc(e.tool)}</div><span class="mono dim">${esc(e.brief)}</span></div>`);
    else if(e.type==='error')body.insertAdjacentHTML('beforeend',
     `<div class="msg" style="border-left:3px solid var(--red)"><div class="who">error</div>${esc(e.text)}</div>`);
   }
   body.scrollTop=body.scrollHeight;
  }
  if(st.done){
   composing=false;document.getElementById('c_send').disabled=false;
   COMPOSE_SID=null;document.getElementById('c_cont').textContent='new session';
   body.insertAdjacentHTML('beforeend',
    `<div class="empty-note">turn complete (exit ${st.exit??'?'} )</div>`);
   poll();
   break;}
  await new Promise(res=>setTimeout(res,700));
 }
}
document.getElementById('c_prompt').addEventListener('keydown',e=>{
 if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendComposer();}});
poll();setInterval(poll,5000);loadMeta();
</script></body></html>"""
PAGE = PAGE.replace("${json.dumps(STATUS_ORDER)}", json.dumps(STATUS_ORDER))


def _quests():
    quests = []
    base = os.path.join(COURT_DIR, ".court")
    for sub in ("quests", "epics"):
        d = os.path.join(base, sub)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(d, fn)
            fm = {}
            try:
                with open(path) as f:
                    lines = f.read().splitlines()
                if lines and lines[0].strip() == "---":
                    for ln in lines[1:]:
                        if ln.strip() == "---":
                            break
                        if ":" in ln:
                            k, v = ln.split(":", 1)
                            fm[k.strip()] = v.strip()
                body = "\n".join(lines)
            except OSError:
                continue
            wt = fm.get("worktree", "")
            dirty = False
            if wt and os.path.isdir(wt):
                try:
                    r = subprocess.run(
                        ["git", "-C", wt, "status", "--porcelain"],
                        capture_output=True, text=True, timeout=5)
                    dirty = bool(r.stdout.strip())
                except Exception:
                    pass
            quests.append({
                "id": fm.get("id", fn[:-3]), "title": fm.get("title", ""),
                "status": fm.get("status", "OPEN"), "app": fm.get("app", ""),
                "branch": fm.get("branch", ""), "worktree": wt,
                "epic": fm.get("parent_epic", ""), "dirty": dirty,
            })
    quests.sort(key=lambda q: (
        STATUS_ORDER.index(q["status"]) if q["status"] in STATUS_ORDER else 99,
        q["id"]))
    return quests


def _repos():
    repos = [COURT_DIR]
    pb = os.path.join(os.path.dirname(COURT_DIR), "pb-app")
    if os.path.isdir(pb):
        repos.append(pb)
    return repos


def _branches(repo):
    try:
        r = subprocess.run(
            ["git", "-C", repo, "branch", "--format=%(refname:short)"],
            capture_output=True, text=True, timeout=5)
        return sorted(b for b in r.stdout.splitlines() if b.strip())
    except Exception:
        return []


def _worktrees(repo=None):
    repo = repo or COURT_DIR
    try:
        r = subprocess.run(
            ["git", "-C", repo, "worktree", "list", "--porcelain"],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    out, cur = [], {}
    for ln in r.stdout.splitlines():
        if ln.startswith("worktree "):
            cur = {"path": ln[9:]}
        elif ln.startswith("HEAD "):
            pass
        elif ln.startswith("branch "):
            cur["branch"] = ln[7:].replace("refs/heads/", "")
        elif ln.startswith("bare") or ln.startswith("detached"):
            cur.setdefault("branch", "(detached)")
        elif not ln and cur:
            out.append(cur)
            cur = {}
    if cur:
        out.append(cur)
    for w in out:
        try:
            r2 = subprocess.run(
                ["git", "-C", w["path"], "status", "--porcelain"],
                capture_output=True, text=True, timeout=5)
            w["dirty"] = bool(r2.stdout.strip())
        except Exception:
            w["dirty"] = False
    return out


def _sessions(limit=200):
    if not os.path.exists(KILO_DB):
        return []
    try:
        db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=3)
        db.execute("pragma query_only=1")
        rows = db.execute(
            "select id, title, agent, model, directory, time_updated,"
            " tokens_input, tokens_output from session"
            " order by time_updated desc limit ?", (limit,)).fetchall()
        db.close()
    except Exception:
        return []
    out = []
    for sid, title, agent, model, directory, tu, ti, to in rows:
        m = ""
        if model:
            try:
                m = json.loads(model).get("id", "") or str(model)
            except Exception:
                m = str(model)
        out.append({
            "id": sid, "title": title or "", "agent": agent or "",
            "model": m.replace("openrouter/", ""), "directory": directory or "",
            "time_updated": tu,
            "tokens_input": ti, "tokens_output": to,
        })
    return out


def _ps_procs():
    try:
        r = subprocess.run(
            ["ps", "axo", "pid=,ppid=,rss=,etime=,args="],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    procs = []
    for ln in r.stdout.splitlines():
        parts = ln.strip().split(None, 4)
        if len(parts) < 5:
            continue
        procs.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                      "rss": int(parts[2]) * 1024, "etime": parts[3],
                      "args": parts[4]})
    return procs


def _processes(procs=None):
    if procs is None:
        procs = _ps_procs()
    total, by_pid = 0, {p["pid"]: p for p in procs}
    for p in procs:
        low = p["args"].lower()
        if "kilo" in p["args"] and ("serve" in p["args"] or "run" in p["args"]):
            total += p["rss"]
        parent = by_pid.get(p["ppid"])
        p["kilo_child"] = bool(parent and "kilo" in parent["args"])
        p["flag"] = p["kilo_child"] and any(pat in low for pat in CHILD_PATTERNS)
    flagged = [p for p in procs if p["flag"]]
    return flagged, total


def _strip_jsonc(text):
    out, i, n = [], 0, len(text)
    in_str = esc = False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif c == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _json_indent(raw):
    indents = [len(m) for m in re.findall(r"(?m)^([ ]+)[\"{\[]", raw)]
    return min(indents) if indents else 2


def _mcp_files():
    specs = []
    if os.path.isfile(MCP_GLOBAL):
        specs.append(("global", MCP_GLOBAL, "mcp"))
    proj = os.path.join(COURT_DIR, "kilo.json")
    if os.path.isfile(proj):
        specs.append(("kilo-castle", proj, "mcp"))
    if os.path.isdir(MCP_PB_APP):
        for rel, key, scope in (("kilo.json", "mcp", "pb-app"),
                                (".kilocode/mcp.json", "mcpServers", "pb-app-legacy"),
                                (".roo/mcp.json", "mcpServers", "pb-app-legacy")):
            p = os.path.join(MCP_PB_APP, rel)
            if os.path.isfile(p):
                specs.append((scope, p, key))
    return specs


def _mcp_argv(cfg):
    if isinstance(cfg.get("command"), list):
        return [str(a) for a in cfg["command"]]
    if cfg.get("command"):
        return [str(cfg["command"])] + [str(a) for a in cfg.get("args", [])]
    return []


def _mcp_package_token(cfg):
    argv = _mcp_argv(cfg)
    skip_val = False
    for item in argv:
        low = item.lower()
        if skip_val:
            skip_val = False
            continue
        if item.startswith("-"):
            skip_val = item in MCP_FLAG_VALUES
            continue
        if low in MCP_RUNNERS:
            continue
        m = re.match(r"^(.+?)@(\d[\w.-]*|latest)$", item)
        return m.group(1) if m else item
    return None


def _mcp_inventory(procs=None):
    if procs is None:
        procs = _ps_procs()
    out = []
    for scope, path, key in _mcp_files():
        try:
            with open(path) as f:
                raw = f.read()
            data = json.loads(_strip_jsonc(raw) if path.endswith(".jsonc") else raw)
        except Exception:
            continue
        servers = data.get(key)
        if not isinstance(servers, dict):
            continue
        for name, cfg in servers.items():
            if not isinstance(cfg, dict):
                continue
            t = str(cfg.get("type", "")).lower()
            if t in ("remote", "sse", "streamable-http"):
                typ = "remote"
            elif t in ("local", "stdio"):
                typ = "local"
            else:
                typ = "remote" if cfg.get("url") else "local"
            enabled = bool(cfg.get("enabled", not cfg.get("disabled", False)))
            token = _mcp_package_token(cfg) if typ == "local" else None
            matches = [p for p in procs if token and token in p["args"]]
            mids = {m["pid"] for m in matches}
            tops = [p for p in matches if p["ppid"] not in mids]
            out.append({
                "scope": scope, "file": path, "key": key, "name": name,
                "type": typ, "enabled": enabled,
                "command": cfg.get("url", "") if typ == "remote"
                           else _mcp_argv(cfg)[:2],
                "running": bool(matches),
                "rss": max((m["rss"] for m in matches), default=0),
                "count": len(tops),
            })
    return out


def _mcp_toggle_write(file, name, enabled):
    """Flip one MCP server's enabled flag; returns (http_code, payload)."""
    if file.endswith(".jsonc"):
        return 400, {"error": "JSONC config (comments would be lost by rewrite); edit manually"}
    entry = next((e for e in _mcp_inventory()
                  if e["file"] == file and e["name"] == name), None)
    if entry is None:
        return 403, {"error": "unknown mcp config file or server; refused"}
    try:
        with open(file) as f:
            raw = f.read()
        data = json.loads(_strip_jsonc(raw) if file.endswith(".jsonc") else raw)
        servers = data.get(entry["key"])
        if not isinstance(servers, dict) or name not in servers \
                or not isinstance(servers[name], dict):
            return 403, {"error": "server not found in config; refused"}
        indent = _json_indent(raw)
        with open(file + ".bak", "w") as f:
            f.write(raw)
        servers[name]["enabled"] = enabled
        with open(file, "w") as f:
            f.write(json.dumps(data, indent=indent) + "\n")
    except Exception as exc:
        return 500, {"error": f"mcp config write failed: {exc}"}
    return 200, {"ok": True}


def _session_messages(sid, limit=60):
    if not os.path.exists(KILO_DB) or not re.fullmatch(r"[\w-]+", sid):
        return []
    try:
        db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=3)
        db.execute("pragma query_only=1")
        rows = db.execute(
            "select id, json_extract(data,'$.role'), time_created from message"
            " where session_id=? order by time_created desc limit ?",
            (sid, limit)).fetchall()
        mids = [r[0] for r in rows]
        texts = {}
        if mids:
            q = ",".join("?" * len(mids))
            for mid, pdata in db.execute(
                    f"select message_id, data from part where message_id in ({q})"
                    " order by time_created", mids):
                try:
                    p = json.loads(pdata)
                except Exception:
                    continue
                if p.get("type") == "text" and p.get("text"):
                    texts[mid] = texts.get(mid, "") + p["text"] + "\n"
        db.close()
    except Exception:
        return []
    out = []
    for mid, role, tc in reversed(rows):
        out.append({"role": role or "system",
                    "time_created": tc,
                    "text": texts.get(mid, "")[:2000]})
    return out


_ALLOWED_AGENTS = ("steward", "code", "serf", "scout", "artist")
KILO_BIN = os.path.expanduser(
    "~/.vscode/extensions/kilocode.kilo-code-7.8.1-darwin-arm64/bin/kilo")
_JOBS = {}
_JOB_SEQ = [0]


def _known_dirs():
    dirs = set(_repos())
    for repo in _repos():
        for w in _worktrees(repo):
            dirs.add(w["path"])
    for s in _sessions(limit=200):
        if s["directory"] and os.path.isdir(s["directory"]):
            dirs.add(s["directory"])
    return sorted(dirs)


def _start_run(job, directory, agent, prompt, session_id):
    cmd = [KILO_BIN, "run", "--dir", directory, "--agent", agent,
           "--format", "json", "--title", prompt.strip()[:60] or "console turn"]
    if session_id:
        cmd += ["--session", session_id]
    else:
        cmd += ["--model", "openrouter/z-ai/glm-5.3-flash"]
    cmd.append(prompt[:20000])
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    except Exception as exc:
        job["events"].append({"type": "error", "text": str(exc)})
        job["done"] = True
        return
    job["pid"] = proc.pid
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        kind = ev.get("type")
        if kind == "text":
            job["events"].append({"type": "text", "text": ev["part"].get("text", "")})
        elif kind == "tool":
            part = ev.get("part", {})
            state = part.get("state") or {}
            inp = state.get("input") if isinstance(state, dict) else {}
            brief = json.dumps(inp)[:160] if inp else ""
            job["events"].append({"type": "tool", "tool": part.get("tool", "?"),
                                  "brief": brief})
        elif kind == "step_finish":
            job["events"].append({"type": "step_finish"})
        elif kind == "error":
            job["events"].append({"type": "error",
                                  "text": str(ev.get("part", ev))[:300]})
    rc = proc.wait()
    job["exit"] = rc
    job["session_id"] = session_id or job.get("events") and None
    job["done"] = True


def _validate_send(directory, agent):
    if agent not in _ALLOWED_AGENTS:
        return "agent not allowed"
    if directory not in _known_dirs():
        return "directory not a known worktree or repo; refused"
    return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/send/"):
            job = _JOBS.get(self.path.rsplit("/", 1)[1])
            if not job:
                self._json({"error": "unknown job"}, 404)
            else:
                self._json({"events": job["events"][-80:], "done": job["done"],
                            "exit": job.get("exit")})
        elif self.path == "/api/compose-meta":
            self._json({"agents": list(_ALLOWED_AGENTS),
                        "dirs": _known_dirs()})
        elif self.path == "/api/state":
            procs = _ps_procs()
            flagged, total = _processes(procs)
            self._json({
                "repo_name": os.path.basename(COURT_DIR),
                "repo_root": COURT_DIR,
                "repos": [
                    {"name": os.path.basename(p), "root": p,
                     "branches": _branches(p), "worktrees": _worktrees(p)}
                    for p in _repos()],
                "quests": _quests(),
                "worktrees": [w for p in _repos() for w in _worktrees(p)],
                "sessions": _sessions(),
                "mcp": _mcp_inventory(procs),
                "processes": [
                    {k: p[k] for k in ("pid", "ppid", "rss", "etime", "args")}
                    for p in flagged],
                "proc_total_rss": total,
            })
        elif self.path.startswith("/api/session"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            self._json(_session_messages(qs.get("id", [""])[0]))
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/api/reap":
            self._reap()
        elif self.path == "/api/mcp":
            self._mcp_toggle()
        elif self.path == "/api/send":
            self._send()
        else:
            self.send_error(404)

    def _send(self):
        try:
            body = self._read_body()
        except Exception:
            self._json({"error": "bad request"}, 400)
            return
        directory = body.get("dir", "")
        agent = body.get("agent", "")
        prompt = str(body.get("prompt", "")).strip()
        session_id = body.get("session_id") or None
        if not prompt:
            self._json({"error": "empty prompt"}, 400)
            return
        err = _validate_send(directory, agent)
        if err:
            self._json({"error": err}, 403)
            return
        if session_id and not re.fullmatch(r"[\w-]+", session_id):
            self._json({"error": "bad session id"}, 400)
            return
        _JOB_SEQ[0] += 1
        job = {"events": [], "done": False, "started": time.time()}
        _JOBS[str(_JOB_SEQ[0])] = job
        threading.Thread(
            target=_start_run,
            args=(job, directory, agent, prompt, session_id), daemon=True).start()
        self._json({"ok": True, "job": str(_JOB_SEQ[0])})

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def _mcp_toggle(self):
        try:
            req = self._read_body()
            file, name, enabled = req["file"], req["name"], bool(req["enabled"])
        except Exception:
            self._json({"error": "bad request"}, 400)
            return
        if not isinstance(file, str) or not isinstance(name, str):
            self._json({"error": "bad request"}, 400)
            return
        code, payload = _mcp_toggle_write(file, name, enabled)
        self._json(payload, code)

    def _reap(self):
        try:
            pid = self._read_body().get("pid")
        except Exception:
            self._json({"error": "bad request"}, 400)
            return
        try:
            r = subprocess.run(
                ["ps", "-o", "ppid=,args=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5)
        except Exception:
            self._json({"error": "lookup failed"}, 500)
            return
        line = r.stdout.strip()
        if not line:
            self._json({"error": "no such process"}, 404)
            return
        ppid_s, args = line.split(None, 1)
        parent = subprocess.run(
            ["ps", "-o", "args=", "-p", ppid_s.strip()],
            capture_output=True, text=True).stdout.strip()
        if "kilo" not in parent:
            self._json({"error": f"parent {ppid_s.strip()} is not a kilo process; refused"}, 403)
            return
        os.kill(pid, signal.SIGTERM)
        time.sleep(1.0)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._json({"ok": True, "reaped": pid})


def serve(port=8300):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"court ui: http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
