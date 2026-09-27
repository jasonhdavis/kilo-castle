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
header{display:flex;align-items:center;gap:14px;padding:0 20px;height:52px;
 background:var(--surface);border-bottom:1px solid var(--edge);box-shadow:var(--sh-1);z-index:2}
.brand{display:flex;align-items:center;gap:10px;font-weight:600;font-size:13px;
 letter-spacing:.12em;text-transform:uppercase}
.brand .glyph{width:26px;height:26px;border-radius:var(--r-md);display:grid;place-items:center;
 background:linear-gradient(135deg,#ffb300,#d29922);color:#1a1205;font-size:13px}
.brand em{color:var(--primary);font-style:normal}
.chip{display:inline-flex;align-items:center;gap:5px;padding:2px 10px;border-radius:999px;
 font-size:11px;font-weight:500;background:var(--surface-2);border:1px solid var(--edge);
 color:var(--dim);white-space:nowrap}
.chip b{color:var(--ink);font-weight:600}
.chip.rss{border-color:rgba(88,166,255,.35)} .chip.rss b{color:var(--blue)}
header .spacer{flex:1}
#clock{color:var(--faint);font-size:11px;font-variant-numeric:tabular-nums}
.vdiv{width:1px;height:22px;background:var(--edge)}

main{display:grid;grid-template-columns:300px 1fr;overflow:hidden}
nav{overflow-y:auto;background:var(--surface);border-right:1px solid var(--edge);padding:12px 12px 20px}
.appswitch{display:flex;gap:6px;margin-bottom:12px}
.appswitch .app{flex:1;text-align:center;padding:7px 4px;border-radius:var(--r-md);cursor:pointer;
 background:var(--surface-2);border:1px solid var(--edge);font-size:11px;font-weight:600;
 letter-spacing:.08em;text-transform:uppercase;color:var(--dim);transition:all .12s ease}
.appswitch .app:hover{border-color:var(--blue);color:var(--ink)}
.appswitch .app.on{background:rgba(255,179,0,.12);border-color:var(--primary);color:var(--primary)}
.repohead{font-size:10px;font-weight:700;letter-spacing:.16em;color:var(--primary);
 margin:14px 6px 6px;display:flex;align-items:center;gap:8px}
.repohead::after{content:"";flex:1;height:1px;background:rgba(255,179,0,.25)}
.castle{position:relative;background:linear-gradient(160deg,#1f242c,#181d24);
 border:1px solid rgba(255,179,0,.25);border-radius:var(--r-lg);padding:10px 14px;
 margin-bottom:12px;cursor:pointer;transition:all .15s ease;box-shadow:var(--sh-1)}
.castle:hover{border-color:rgba(255,179,0,.55)}
.castle.sel{border-color:var(--primary);box-shadow:0 0 0 1px var(--primary),var(--sh-1)}
.castle .name{font-weight:600;letter-spacing:.04em;display:flex;align-items:center;gap:8px}
.castle .name .dot{width:8px;height:8px;border-radius:50%;background:var(--green);
 box-shadow:0 0 6px var(--green)}
.castle .sub{color:var(--dim);font-size:11px;margin-top:2px}
nav h2{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.14em;
 color:var(--faint);margin:14px 8px 5px;display:flex;align-items:center;gap:8px}
nav h2::after{content:"";flex:1;height:1px;background:var(--edge-soft)}
.q{position:relative;padding:6px 10px 6px 14px;border-radius:var(--r-md);cursor:pointer;
 margin-bottom:2px;transition:background .12s ease}
.q:hover{background:var(--surface-2)}
.q.sel{background:var(--surface-2);box-shadow:inset 2px 0 0 var(--primary)}
.q .row1{display:flex;align-items:center;gap:7px;font-weight:500;font-size:12.5px}
.q .row2{color:var(--dim);font-size:11px;margin-top:1px;white-space:nowrap;overflow:hidden;
 text-overflow:ellipsis}
.badge{font-size:9.5px;padding:1px 7px;border-radius:999px;background:rgba(248,81,73,.12);
 color:var(--red);font-weight:600;letter-spacing:.06em;text-transform:uppercase}
.st{display:inline-flex;align-items:center;padding:1px 8px;border-radius:999px;font-size:9.5px;
 font-weight:600;letter-spacing:.08em;text-transform:uppercase;background:var(--surface-2);color:var(--dim)}

#chat{display:flex;flex-direction:column;overflow:hidden}
#chatbar{display:flex;align-items:center;gap:10px;padding:10px 20px;
 background:var(--surface);border-bottom:1px solid var(--edge);min-height:52px}
#chatbar .wt{font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--ink)}
#chatbar .wt .dim{color:var(--dim)}
.iconbtn{width:30px;height:30px;border-radius:var(--r-md);display:grid;place-items:center;
 cursor:pointer;background:var(--surface-2);border:1px solid var(--edge);color:var(--dim);
 transition:all .12s ease;font-size:14px}
.iconbtn:hover{border-color:var(--primary);color:var(--primary)}
.tabs{display:flex;gap:5px;flex-wrap:wrap;margin-left:auto;max-width:55%}
.tab{padding:3px 12px;border:1px solid var(--edge);border-radius:999px;cursor:pointer;
 font-size:11px;color:var(--dim);background:var(--surface-2);max-width:200px;
 white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:all .12s ease}
.tab:hover{border-color:var(--blue);color:var(--ink)}
.tab.on{background:var(--blue);border-color:var(--blue);color:#0d1117;font-weight:600}
.tab.newtab{border-style:dashed}

#transcript{flex:1;overflow-y:auto;padding:18px 26px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:80%;padding:10px 14px;border-radius:var(--r-lg);background:var(--surface);
 border:1px solid var(--edge-soft);white-space:pre-wrap;word-break:break-word;font-size:13px}
.msg.user{align-self:flex-end;background:rgba(255,179,0,.08);border:1px solid rgba(255,179,0,.25)}
.msg.assistant{align-self:flex-start;border-left:3px solid var(--blue)}
.msg.notice{align-self:center;font-size:11px;color:var(--faint);
 background:transparent;border:none;padding:2px}
.msg.thinking{align-self:flex-start;font-size:12px;color:var(--dim);font-style:italic;
 background:var(--surface-2);border:1px dashed var(--edge);max-width:80%}
.msg.tool{align-self:center;font-size:11px;color:var(--dim);background:var(--surface-2);
 border:1px dashed var(--edge);padding:4px 12px;max-width:90%}
.msg.error{align-self:center;border-left:3px solid var(--red);color:var(--red);font-size:12px}
.msg .who{color:var(--faint);font-size:9.5px;text-transform:uppercase;letter-spacing:.1em;
 margin-bottom:3px;font-weight:600;display:none}
#transcript .notice{align-self:center;color:var(--faint);font-size:11.5px;padding:6px 0}

#composer{border-top:1px solid var(--edge);background:var(--surface);padding:12px 20px 14px}
#composer .cont{font-size:11px;color:var(--dim);margin-bottom:6px;display:flex;gap:8px;align-items:center}
#composer .cont b{color:var(--blue);font-weight:600;font-family:ui-monospace,Menlo,monospace}
#composer .cont .x{cursor:pointer;color:var(--red);font-weight:700}
#composer .row{display:flex;gap:10px;align-items:flex-end}
#composer textarea{flex:1;background:var(--bg);border:1px solid var(--edge);color:var(--ink);
 border-radius:var(--r-md);font:13px/1.45 "Inter",sans-serif;padding:10px 12px;resize:none;
 height:46px;max-height:140px}
#composer textarea:focus{outline:none;border-color:var(--blue)}
#composer .send{background:var(--primary);border:none;color:var(--primary-ink);
 font-weight:700;padding:0 24px;border-radius:var(--r-md);cursor:pointer;font-size:12.5px;
 letter-spacing:.04em;height:46px}
#composer .send:disabled{opacity:.45;cursor:default}

#modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;z-index:20;
 align-items:center;justify-content:center}
#modal.on{display:flex}
#modal .box{width:720px;max-width:92vw;max-height:80vh;overflow-y:auto;background:var(--surface);
 border:1px solid var(--edge);border-radius:var(--r-lg);box-shadow:var(--sh-2)}
#modal .hd{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;
 border-bottom:1px solid var(--edge)}
#modal .hd b{color:var(--primary);font-size:13px}
#modal .hd span{cursor:pointer;color:var(--dim);padding:4px 10px;border-radius:var(--r-sm)}
#modal .hd span:hover{background:var(--surface-2)}
#modal .bd{padding:14px 18px}
.dupwarn{display:flex;align-items:center;gap:8px;background:rgba(248,81,73,.1);color:var(--red);
 border:1px solid rgba(248,81,73,.35);border-radius:var(--r-sm);padding:6px 12px;
 margin-bottom:10px;font-size:11.5px;font-weight:500}
table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:6px 10px;color:var(--faint);font-size:10px;font-weight:600;
 text-transform:uppercase;letter-spacing:.1em;border-bottom:1px solid var(--edge)}
td{padding:7px 10px;border-bottom:1px solid var(--edge-soft)}
tbody tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:11px}
.dim{color:var(--dim)} .blue{color:var(--blue)} .num{font-variant-numeric:tabular-nums}
button{background:var(--surface-2);border:1px solid var(--edge);color:var(--dim);
 border-radius:var(--r-sm);padding:3px 12px;cursor:pointer;font-size:10.5px;font-weight:600;
 letter-spacing:.06em;text-transform:uppercase;transition:all .12s ease}
button:hover{border-color:var(--red);color:var(--red);background:rgba(248,81,73,.08)}
.empty-note{color:var(--faint);text-align:center;padding:40px;font-size:12.5px}
</style></head><body>
<header><div class="brand"><span class="glyph">♜</span>COURT <em>CONSOLE</em></div>
<div class="vdiv"></div><div id="totals" style="display:flex;gap:8px"></div>
<div class="spacer"></div><div class="chip rss">kilo RSS <b id="t_rss">—</b></div>
<div id="clock"></div></header>
<main><nav id="nav"></nav>
<section id="chat">
 <div id="chatbar">
  <span class="wt" id="wt_label">select a worktree</span>
  <span id="wt_badge"></span>
  <div class="iconbtn" title="MCP servers" onclick="openMcp()">⚙</div>
  <div class="tabs" id="sess_tabs"></div>
 </div>
 <div id="transcript"><div class="notice">select a branch, then a session — the chat loads here</div></div>
 <div id="composer">
  <div class="cont" id="c_cont">new session — pick a worktree, or click a session tab to continue it</div>
  <div class="row">
   <textarea id="c_prompt" placeholder="message the agent… (Enter to send, Shift+Enter for newline)"></textarea>
   <select id="c_agent" style="height:46px;background:var(--bg);border:1px solid var(--edge);color:var(--ink);border-radius:8px;padding:0 8px"></select>
   <button class="send" id="c_send" onclick="sendComposer()">SEND</button>
  </div>
 </div>
</section></main>
<div id="modal"><div class="box"><div class="hd"><b>MCP SERVERS — merged inventory</b>
<span onclick="closeMcp()">CLOSE ✕</span></div><div class="bd" id="modal_bd"></div></div></div>
<script>
let S=null, selRepo=null, selWt=null, selSess=null, msgs=[], composing=false;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const mb=r=>r==null||r===undefined?'—':(r/1048576).toFixed(0)+' MB';
const ago=ts=>{if(!ts)return'';const d=(Date.now()-ts)/1000;
 return d<60?`${d|0}s`:(d<3600?`${d/60|0}m`:(d<86400?`${d/3600|0}h`:`${d/86400|0}d`))};
const $=id=>document.getElementById(id);
const TRUNKS=['main','castle','master','trunk'];
function repo(){return S.repos.find(r=>r.key===selRepo)||S.repos[0]}

async function poll(){
 try{S=await (await fetch('/api/state')).json();}catch(e){return;}
 if(!selRepo&&S.repos.length)selRepo=S.repos.find(r=>r.key==='app')? 'app':S.repos[0].key;
 $('totals').innerHTML=`<span class="chip">sessions <b>${S.sessions.length}</b></span>
  <span class="chip">worktrees <b>${S.worktrees.length}</b></span>`+
  ((S.processes||[]).length?`<span class="chip" style="border-color:rgba(248,81,73,.4)"><b style="color:var(--red)">${S.processes.length} flagged</b></span>`:'');
 $('t_rss').textContent=mb(S.proc_total_rss);
 $('clock').textContent=new Date().toLocaleTimeString();
 renderNav();
 if(selWt)renderSessionsBar();
}
function renderNav(){
 const r=repo(); if(!r){$('nav').innerHTML='';return;}
 let h=`<div class="appswitch">`+S.repos.map(x=>
  `<div class="app ${x.key===selRepo?'on':''}" onclick="switchApp('${x.key}')">${x.key}</div>`).join('')+`</div>`;
 h+=`<div class="repohead">${esc(r.name)} · ${r.worktrees.length} worktrees</div>`;
 const trunk=r.worktrees.find(w=>w.branch&&(w.branch===TRUNKS.find(t=>t===w.branch)||TRUNKS.includes(w.branch.split('/').pop())));
 if(trunk)h+=`<div class="castle ${selWt===trunk.path?'sel':''}" onclick="pickWt('${esc(trunk.path)}')">
  <div class="name"><span class="dot"></span>${esc(r.name)} trunk</div>
  <div class="sub">${esc(trunk.branch||'?')}${trunk.dirty?' · dirty':''}</div></div>`;
 const byBranch={}; for(const w of r.worktrees){if(w.branch)byBranch[w.branch]=w}
 const secs={};
 for(const b of r.branches){
  const ns=b.includes('/')?b.split('/')[0]:'(root)';
  if(TRUNKS.includes(b))continue;
  (secs[ns]??=[]).push(b);
 }
 for(const ns of Object.keys(secs).sort((a,b)=>secs[b].length-secs[a].length)){
  h+=`<h2>${esc(ns)} · ${secs[ns].length}</h2>`;
  for(const b of secs[ns].slice(0,30)){
   const w=byBranch[b];
   const mark=w?(w.dirty?'<span class="badge">dirty</span>':''):'<span class="st">no wt</span>';
   h+=`<div class="q ${selWt&&w&&selWt===w.path?'sel':''}" onclick="pickWt(${w?`'${esc(w.path)}'`:'null'},'${esc(b)}')">
   <div class="row1">${esc(b.includes('/')?b.slice(b.indexOf('/')+1):b)}${mark}</div>
   <div class="row2">${esc(b)}</div></div>`;
  }
  if(secs[ns].length>30)h+=`<div class="q dim" style="cursor:default">… ${secs[ns].length-30} more</div>`;
 }
 $('nav').innerHTML=h;
}
function switchApp(key){selRepo=key;selWt=null;selSess=null;msgs=[];
 $('wt_label').textContent='select a worktree';$('wt_badge').innerHTML='';
 renderNav();}
function pickWt(path,branch){
 selWt=path;selSess=null;msgs=[];
 const w=(repo().worktrees||[]).find(x=>x.path===path);
 $('wt_label').innerHTML=esc(branch||w&&w.branch||path.split('/').pop())+
  ` <span class="dim">· ${esc(path.replace('/Users/scrummage/Python/',''))}</span>`;
 $('wt_badge').innerHTML=w&&w.dirty?'<span class="badge">dirty</span>':'';
 renderNav();renderSessionsBar();
 const sess=sessionsFor(path);
 if(sess.length)openSess(sess[0].id);
 else{selSess=null;renderTranscript();
  $('c_cont').innerHTML='new session — no sessions in this worktree yet';}
}
function sessionsFor(path){
 return S.sessions.filter(s=>s.directory&&(s.directory===path||s.directory.startsWith(path+'/')));
}
function renderSessionsBar(){
 const sess=sessionsFor(selWt).slice(0,8);
 $('sess_tabs').innerHTML=
  sess.map(s=>`<div class="tab ${selSess===s.id?'on':''}" onclick="openSess('${s.id}')"
   title="${esc(s.title||s.id)}">${esc(s.agent||'?')}: ${esc((s.title||s.id).slice(0,26))}</div>`).join('')+
  `<div class="tab newtab ${selSess===null?'on':''}" onclick="newSess()" title="start a fresh session">＋ new</div>`;
}
function newSess(){selSess=null;msgs=[];
 $('c_cont').innerHTML='new session in <b>'+esc(selWt?selWt.replace('/Users/scrummage/Python/',''):'?')+'</b> <span class="dim">— agent replies as a fresh session</span>';
 renderSessionsBar();renderTranscript();}
async function openSess(id){
 selSess=id;msgs=[];
 $('c_cont').innerHTML=`continuing <b>${esc(id.slice(0,24))}…</b> <span class="x" onclick="newSess()">start new instead</span>`;
 renderSessionsBar();renderTranscript();
 $('transcript').innerHTML='<div class="notice">loading…</div>';
 try{msgs=await (await fetch('/api/session?id='+id)).json();}catch(e){msgs=[];}
 renderTranscript();
}
function renderTranscript(){
 const t=$('transcript');
 if(!msgs.length){t.innerHTML='<div class="empty-note">no messages yet — say something below</div>';return;}
 t.innerHTML=msgs.map(m=>{
  if(!m.text)return '';
  const cls=m.role==='user'?'user':(m.role==='assistant'?'assistant':(m.role==='reasoning'?'thinking':'tool'));
  return `<div class="msg ${cls}"><div class="who">${esc(m.role)}</div>${esc(m.text)}</div>`;
 }).join('')||'<div class="empty-note">no text messages in this session yet</div>';
 t.scrollTop=t.scrollHeight;
}
function chatAppend(cls,text){
 const t=$('transcript');
 if(t.querySelector('.empty-note'))t.innerHTML='';
 t.insertAdjacentHTML('beforeend',`<div class="msg ${cls}">${esc(text)}</div>`);
 t.scrollTop=t.scrollHeight;
}
async function sendComposer(){
 if(composing)return;
 const ta=$('c_prompt');
 const prompt=ta.value.trim();
 if(!prompt||!selWt)return;
 ta.value='';composing=true;$('c_send').disabled=true;
 chatAppend('user',prompt);
 chatAppend('notice','dispatching…');
 let job;
 try{
  const r=await fetch('/api/send',{method:'POST',body:JSON.stringify({
   dir:selWt, agent:$('c_agent').value, prompt, session_id:selSess||null})});
  if(!r.ok){chatAppend('error','refused: '+await r.text());composing=false;$('c_send').disabled=false;return;}
  job=(await r.json()).job;
 }catch(e){chatAppend('error','dispatch failed: '+e);composing=false;$('c_send').disabled=false;return;}
 const nEl=[...document.querySelectorAll('#transcript .notice')].pop();
 let seen=0, err=null;
 while(true){
  let st;
  try{st=await (await fetch('/api/send/'+job)).json();}
  catch(e){err=e;break;}
  const evs=st.events.slice(seen);seen=st.events.length;
  for(const e of evs){
   if(nEl)nEl.remove();
   if(e.type==='text'&&e.text)chatAppend('assistant',e.text);
   else if(e.type==='reasoning'&&e.text)chatAppend('thinking',e.text);
   else if(e.type==='status')chatAppend('notice',e.text);
   else if(e.type==='step')chatAppend('notice',e.text);
   else if(e.type==='step_finish')chatAppend('notice',e.text||'step done');
   else if(e.type==='tool')chatAppend('tool',`tool · ${e.tool} ${e.brief||''}`);
   else if(e.type==='error')chatAppend('error',e.text||'unknown error');
  }
  if(nEl&&!st.done)nEl.textContent=`working… ${((Date.now()-t0)/1000)|0}s`;
  if(st.done)break;
  await new Promise(res=>setTimeout(res,700));
 }
 if(err)chatAppend('error','stream failed: '+err);
 else chatAppend('notice','turn complete');
 composing=false;$('c_send').disabled=false;
 poll();
}
document.getElementById('c_prompt').addEventListener('keydown',e=>{
 if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendComposer();}});
function openMcp(){
 const list=S.mcp||[];
 let h='';
 for(const d of list.filter(m=>m.type==='local'&&m.enabled&&m.count>=2)){
  h+=`<div class="dupwarn">duplicate MCP spawns detected: ${esc(d.name)} x${d.count} (~${mb(d.rss)} each)</div>`;}
 h+='<table><thead><tr><th>name</th><th>scope</th><th>type</th><th>enabled</th><th>running</th><th></th></tr></thead><tbody>'+
  list.map(m=>{
   const run=m.running?`<span class="num" style="color:var(--green)">yes</span> <span class="num blue">${mb(m.rss)}</span>`
    :'<span class="dim">no</span>';
   const btn=m.file.endsWith('.jsonc')?'<span class="dim">manual</span>'
    :`<button title="takes effect for sessions started after the change" onclick="mcpToggle('${esc(m.file)}','${esc(m.name)}')">${m.enabled?'disable':'enable'}</button>`;
   return `<tr><td class="mono">${esc(m.name)}</td><td class="dim">${esc(m.scope)}</td><td class="dim">${esc(m.type)}</td>`+
    `<td>${m.enabled?'<span class="num" style="color:var(--green)">yes</span>':'<span class="dim">no</span>'}</td><td>${run}</td><td>${btn}</td></tr>`;
  }).join('')+'</tbody></table>'+
  '<div class="dim" style="padding-top:10px;font-size:11px">toggles edit the config file (a .bak copy is kept) and take effect for sessions started after the change</div>';
 $('modal_bd').innerHTML=h;
 $('modal').classList.add('on');
}
function closeMcp(){$('modal').classList.remove('on');}
async function mcpToggle(file,name){
 const m=(S.mcp||[]).find(x=>x.file===file&&x.name===name);
 if(!m)return;
 const r=await fetch('/api/mcp',{method:'POST',body:JSON.stringify({file,name,enabled:!m.enabled})});
 if(!r.ok)alert('refused: '+(await r.text()));else poll();
}
(async()=>{try{META=await (await fetch('/api/compose-meta')).json();
 $('c_agent').innerHTML=META.agents.map(x=>`<option>${x}</option>`).join('');}catch(e){}})();
poll();setInterval(poll,5000);
</script></body></html>
"""
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
    base = os.path.dirname(COURT_DIR)
    out = []
    for key, name, path in (
        ("app", "pb-app", os.path.join(base, "pb-app")),
        ("balloon", "pb-balloon", os.path.join(base, "pb-balloon")),
        ("custom", "pb-custom", os.path.join(base, "pb-custom")),
        ("castle", "kilo-castle", COURT_DIR),
    ):
        if os.path.isdir(os.path.join(path, ".git")) or os.path.isdir(path):
            out.append({"key": key, "name": name, "root": path})
    return out


_WT_CACHE = {}
_WT_LOCKS = {}
_BR_CACHE = {}


def _branches(repo, ttl=30):
    now = time.time()
    hit = _BR_CACHE.get(repo)
    if hit and now - hit[0] < ttl:
        return hit[1]
    try:
        r = subprocess.run(
            ["git", "-C", repo, "branch", "--format=%(refname:short)"],
            capture_output=True, text=True, timeout=5)
        out = sorted(b for b in r.stdout.splitlines() if b.strip())
    except Exception:
        out = []
    _BR_CACHE[repo] = (now, out)
    return out


def _worktrees(repo=None, ttl=60):
    repo = repo or COURT_DIR
    now = time.time()
    hit = _WT_CACHE.get(repo)
    if hit and now - hit[0] < ttl:
        return hit[1]
    lock = _WT_LOCKS.setdefault(repo, threading.Lock())
    if not lock.acquire(blocking=False):
        return hit[1] if hit else []
    try:
        return _compute_worktrees(repo)
    finally:
        lock.release()


def _compute_worktrees(repo):
    from concurrent.futures import ThreadPoolExecutor
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

    def _dirty(w):
        try:
            r2 = subprocess.run(
                ["git", "-C", w["path"], "status", "--porcelain"],
                capture_output=True, text=True, timeout=10)
            w["dirty"] = bool(r2.stdout.strip())
        except Exception:
            w["dirty"] = False
        return w

    with ThreadPoolExecutor(max_workers=8) as ex:
        out = list(ex.map(_dirty, out))
    _WT_CACHE[repo] = (time.time(), out)
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
    dirs = {r["root"] for r in _repos()}
    for r in _repos():
        for w in _worktrees(r["root"]):
            dirs.add(w["path"])
    for s in _sessions(limit=200):
        if s["directory"] and os.path.isdir(s["directory"]):
            dirs.add(s["directory"])
    return sorted(dirs)


def _start_run(job, directory, agent, prompt, session_id):
    mode = f"continue {session_id[:18]}..." if session_id else "new session"
    job["events"].append({"type": "status", "text": (
        f"spawning agent - {agent} - {mode} - "
        f"{directory.replace('/Users/scrummage/Python/', '')}")})
    cmd = [KILO_BIN, "run", "--dir", directory, "--agent", agent,
           "--format", "json", "--title", prompt.strip()[:60] or "console turn"]
    if session_id:
        cmd += ["--session", session_id]
    else:
        cmd += ["--model", "openrouter/z-ai/glm-5.3-flash"]
    cmd.append(prompt[:20000])
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True)
    except Exception as exc:
        job["events"].append({"type": "error", "text": str(exc)})
        job["done"] = True
        return

    def _reader():
        connected = False
        raw_tail = ""
        for line in proc.stdout:
            raw_tail = line[-300:]
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if not connected:
                connected = True
                job["events"].append({"type": "status",
                                      "text": "agent connected - streaming"})
            if ev.get("sessionID") and not job.get("sid"):
                job["sid"] = ev["sessionID"]
                job["events"].append({"type": "status",
                                      "text": f"session {ev['sessionID'][:22]}..."})
            kind = ev.get("type")
            part = ev.get("part") or {}
            if kind == "text":
                job["events"].append({"type": "text", "text": part.get("text", "")})
            elif kind == "reasoning":
                txt = (part.get("text") or "").strip()
                if txt:
                    job["events"].append({"type": "reasoning", "text": txt[:1500]})
            elif kind == "step_start":
                job["events"].append({"type": "step", "text": "thinking..."})
            elif kind == "tool":
                state = part.get("state") or {}
                inp = state.get("input") if isinstance(state, dict) else {}
                brief = json.dumps(inp)[:160] if inp else ""
                job["events"].append({"type": "tool", "tool": part.get("tool", "?"),
                                      "brief": brief})
            elif kind == "step_finish":
                toks = part.get("tokens") or part.get("metrics") or {}
                job["events"].append({
                    "type": "step_finish",
                    "text": f"step done - {json.dumps(toks)[:120]}" if toks else "step done"})
            elif kind == "error":
                job["events"].append({"type": "error",
                                      "text": str(part or ev)[:300]})
        if not connected and raw_tail:
            job["events"].append({"type": "error",
                                  "text": "agent exited before responding: " + raw_tail})

    reader = threading.Thread(target=_reader, daemon=True)
    reader.start()
    rc = proc.wait()
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    reader.join(timeout=3)
    job["events"].append({"type": "status",
                          "text": f"turn process exited ({rc})"})
    job["exit"] = rc
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
            repos = [
                {"key": r["key"], "name": r["name"], "root": r["root"],
                 "branches": _branches(r["root"]),
                 "worktrees": _worktrees(r["root"])}
                for r in _repos()]
            self._json({
                "repos": repos,
                "worktrees": [w for r in repos for w in r["worktrees"]],
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
