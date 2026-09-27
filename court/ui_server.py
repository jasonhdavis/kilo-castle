"""Zero-dependency localhost web console for the court pipeline.

Reads: .court quest files, git worktrees, the local kilo session DB (read-only),
and the live process table. Mutating endpoints: /api/reap (terminates a process
whose parent is a verified kilo process) and /api/mcp (flips the enabled flag of
an inventoried MCP server in its own config file, with a .bak backup).
"""

import glob
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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

PAGE = r"""<!doctype html>
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
.app{cursor:pointer;transition:all .12s ease}
.appswitch .app,.vtabs .app{flex:1;text-align:center;padding:7px 4px;border-radius:var(--r-md);
 background:var(--surface-2);border:1px solid var(--edge);font-size:11px;font-weight:600;
 letter-spacing:.08em;text-transform:uppercase;color:var(--dim)}
.app:hover{border-color:var(--blue);color:var(--ink)}
.app.on{background:rgba(255,179,0,.12);border-color:var(--primary);color:var(--primary)}
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
.tab{display:inline-flex;align-items:center;gap:6px;padding:3px 12px;border:1px solid var(--edge);
 border-radius:999px;cursor:pointer;font-size:11px;color:var(--dim);background:var(--surface-2);
 max-width:200px;white-space:nowrap;overflow:hidden;transition:all .12s ease}
.tab .tl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis}
.tab .tx{opacity:0;color:var(--red);font-weight:700;cursor:pointer;padding:0 2px;flex:none}
.tab:hover .tx{opacity:1}
.tab .tx:hover{color:var(--ink)}
#composer .send.stop{background:var(--red);color:#fff;flex:none}
#composer .send.stop:hover{background:#ff7875;color:#fff}
.dots i{display:inline-block;width:4px;height:4px;border-radius:50%;background:var(--primary);
 margin:0 1px;vertical-align:middle;animation:dotp 1.2s infinite ease-in-out}
.dots i:nth-child(2){animation-delay:.15s}
.dots i:nth-child(3){animation-delay:.3s}
@keyframes dotp{0%,80%,100%{transform:scale(.6);opacity:.35}40%{transform:scale(1);opacity:1}}
.livedot{width:7px;height:7px;border-radius:50%;background:var(--green);display:inline-block;
 animation:pulse 1.1s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
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
.msg.md{white-space:normal}
.msg.md p{margin:3px 0}
.msg.md p:first-child{margin-top:0}
.msg.md p:last-child{margin-bottom:0}
.msg.md h3,.msg.md h4,.msg.md h5{margin:8px 0 4px;color:var(--ink)}
.msg.md h3{font-size:14px}.msg.md h4{font-size:13px}.msg.md h5{font-size:12.5px}
.msg.md ul,.msg.md ol{margin:4px 0;padding-left:20px}
.msg.md li{margin:2px 0}
.msg.md blockquote{border-left:3px solid var(--edge);padding:2px 10px;color:var(--dim);margin:4px 0}
.msg.md hr{border:none;border-top:1px solid var(--edge);margin:8px 0}
.msg.md table{margin:6px 0;width:auto;min-width:40%;font-size:12px}
.msg.md th,.msg.md td{border:1px solid var(--edge);padding:4px 10px}
.codebox{background:var(--bg);border:1px solid var(--edge);border-radius:var(--r-md);margin:6px 0;overflow:hidden}
.codebar{display:flex;justify-content:space-between;align-items:center;background:var(--surface-2);
 padding:3px 10px;font-size:10px;color:var(--faint);text-transform:uppercase;letter-spacing:.08em}
.codebar .cpy{cursor:pointer;color:var(--blue);text-transform:lowercase;letter-spacing:0;font-weight:600}
.codebar .cpy:hover{color:var(--ink)}
.codebox pre{margin:0;padding:10px 12px;overflow-x:auto}
.codebox code{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;color:var(--ink)}
code.ic{font-family:ui-monospace,Menlo,monospace;font-size:11.5px;background:var(--surface-2);
 border:1px solid var(--edge);border-radius:4px;padding:0 5px}
.msg.thinking .who{display:block}
.msg.thinking{cursor:pointer}
.msg.thinking.folded{max-height:120px;overflow:hidden;cursor:pointer;position:relative}
.msg.thinking.folded::after{content:"⌄ expand";position:absolute;bottom:4px;right:8px;font-size:9.5px;
 color:var(--blue);background:var(--surface-2);border:1px solid var(--edge);border-radius:6px;
 padding:0 6px;font-style:normal}
.vtabs{display:flex;gap:5px}
.vtabs .app{flex:none;padding:6px 14px}
#board{display:none;flex-direction:column;overflow:hidden;background:var(--bg)}
.bfil{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.chipx{display:inline-flex;align-items:center;padding:4px 12px;border-radius:999px;
 border:1px solid var(--edge);background:var(--surface-2);color:var(--dim);
 font-size:11px;font-weight:600;cursor:pointer;letter-spacing:.04em;transition:all .12s ease}
.chipx:hover{border-color:var(--blue);color:var(--ink)}
.chipx.on{background:rgba(255,179,0,.12);border-color:var(--primary);color:var(--primary)}
#boardcols{flex:1;overflow-x:auto;overflow-y:hidden;display:flex;gap:10px;
 padding:14px 16px;align-items:flex-start}
.bcol{flex:0 0 232px;max-height:100%;overflow-y:auto;background:var(--surface);
 border:1px solid var(--edge);border-radius:var(--r-lg);padding:8px}
.bcol h3{font-size:10px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
 color:var(--primary);margin:2px 4px 8px}
.bcard{background:var(--surface-2);border:1px solid var(--edge-soft);border-radius:var(--r-md);
 padding:8px 10px;margin-bottom:6px;transition:border-color .12s ease}
.bcard.attn{border-color:rgba(248,81,73,.45);background:rgba(248,81,73,.05)}
.bcard.attn .bid{color:var(--red)}
.bops{display:flex;gap:4px;margin-top:6px;flex-wrap:wrap}
.bops button{padding:2px 8px;font-size:9px}
.bops button.go{border-color:rgba(63,185,80,.4);color:var(--green)}
.bops button.go:hover{border-color:var(--green);color:var(--green);background:rgba(63,185,80,.08)}
.bops button.warn{border-color:rgba(248,81,73,.4);color:var(--red)}
.bchips{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}
.bchip{font-size:9px;padding:1px 6px;border-radius:4px;background:var(--surface);
 border:1px solid var(--edge-soft);color:var(--dim)}
.bchip.ok{color:var(--green);border-color:rgba(63,185,80,.3)}
.bchip.bad{color:var(--red);border-color:rgba(248,81,73,.35)}
.bchip.warn{color:var(--amber);border-color:rgba(210,153,34,.35)}
#jobout{font-family:ui-monospace,Menlo,monospace;font-size:11px;white-space:pre-wrap;
 word-break:break-word;background:var(--bg);border:1px solid var(--edge);
 border-radius:var(--r-md);padding:10px 12px;max-height:52vh;overflow-y:auto;margin-top:10px}
#jobout .err{color:var(--red)}
.turnrow{display:flex;gap:8px;align-items:baseline;padding:5px 4px;border-bottom:1px solid var(--edge-soft);
 font-size:11px;cursor:pointer}
.turnrow:hover{background:var(--surface-2)}
.turnrow .tmono{font-family:ui-monospace,Menlo,monospace;font-size:10px;color:var(--faint);
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.turnrow.fail .tex{color:var(--red)}
.srow{display:flex;gap:8px;align-items:baseline;padding:6px 4px;border-bottom:1px solid var(--edge-soft);
 font-size:11.5px;cursor:pointer}
.srow:hover{background:var(--surface-2)}
@media (prefers-reduced-motion:reduce){.livedot,.dots i{animation:none}}
.bcard.click{cursor:pointer}
.bcard:hover{border-color:var(--blue)}
.bid{font-family:ui-monospace,Menlo,monospace;font-size:10.5px;color:var(--blue);
 display:flex;align-items:center;gap:6px}
.bt{font-size:12px;margin:3px 0 0;color:var(--ink)}
.brow{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-top:5px}
.brow .chip{font-size:9.5px;padding:1px 8px}
.bmono{font-family:ui-monospace,Menlo,monospace;font-size:10px;color:var(--faint);
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:150px}
.bempty{color:var(--faint);font-size:11px;text-align:center;padding:6px 0}
.q .livedot{flex:none}
#composer{position:relative}
#cmdlist{position:absolute;bottom:100%;left:20px;display:none;min-width:340px;max-width:560px;
 max-height:230px;overflow-y:auto;background:var(--surface);border:1px solid var(--edge);
 border-radius:var(--r-md);box-shadow:var(--sh-2);z-index:10}
.cmdrow{display:flex;gap:8px;align-items:baseline;padding:6px 12px;cursor:pointer}
.cmdrow.on,.cmdrow:hover{background:var(--surface-2)}
.cmdname{color:var(--primary);font-family:ui-monospace,Menlo,monospace;font-size:12px;flex:none}
.cmddesc{color:var(--dim);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sessmeta{color:var(--faint);font-size:10.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.sessmeta b{color:var(--blue);font-weight:600}
</style></head><body>
<header><div class="brand"><span class="glyph">♜</span>COURT <em>CONSOLE</em></div>
<div class="vtabs"><div class="app on" id="v_chat" onclick="setView('chat')">chat</div>
<div class="app" id="v_board" onclick="setView('board')">board</div></div>
<div class="vdiv"></div><div id="totals" style="display:flex;gap:8px"></div>
<div class="spacer"></div><span class="chip" id="t_today" title="sessions active since local midnight — cost / tokens"></span>
<div class="chip rss">kilo RSS <b id="t_rss">—</b></div>
<div class="iconbtn" title="recent turns" onclick="openTurns()">≡</div>
<div class="iconbtn" title="search sessions" onclick="openSearch()">⌕</div>
<div id="clock"></div></header>
<main><nav id="nav"></nav>
<section id="chat">
  <div id="chatbar">
   <span class="wt" id="wt_label">select a worktree</span>
   <span id="wt_badge"></span>
   <span id="live_chip" class="chip" style="display:none"><span class="livedot"></span>&nbsp;agent working</span>
   <span id="sess_meta" class="sessmeta"></span>
   <div class="iconbtn" title="MCP servers" onclick="openMcp()">⚙</div>
   <div class="iconbtn" id="sess_del" title="delete this session" style="display:none" onclick="delSess(selSess)">✕</div>
   <div class="tabs" id="sess_tabs"></div>
  </div>
 <div id="transcript"><div class="notice">select a branch, then a session — the chat loads here</div></div>
 <div id="composer">
  <div id="cmdlist"></div>
  <div class="cont" id="c_cont">new session — pick a worktree, or click a session tab to continue it</div>
  <div class="row">
   <textarea id="c_prompt" placeholder="message the agent… (Enter to send, Shift+Enter for newline)"></textarea>
   <input id="c_model" list="model_dl" value="openrouter/z-ai/glm-5.3-flash" spellcheck="false"
    autocomplete="off" placeholder="model" title="model id — type to filter (kilo/provider/model or provider/model)"
    style="width:240px;height:46px;background:var(--bg);border:1px solid var(--edge);color:var(--ink);
    border-radius:8px;padding:0 10px;font:11.5px ui-monospace,Menlo,monospace">
   <datalist id="model_dl"></datalist>
   <select id="c_agent" style="height:46px;background:var(--bg);border:1px solid var(--edge);color:var(--ink);border-radius:8px;padding:0 8px"></select>
   <button class="send stop" id="c_stop" style="display:none" onclick="stopTurn()">STOP</button>
   <button class="send" id="c_send" onclick="sendComposer()">SEND</button>
  </div>
 </div>
</section>
<section id="board">
 <div id="boardbar" style="padding:10px 20px;border-bottom:1px solid var(--edge);background:var(--surface)"></div>
 <div id="boardcols"></div>
</section></main>
<div id="modal"><div class="box"><div class="hd"><b>MCP SERVERS — merged inventory</b>
<span onclick="closeMcp()">CLOSE ✕</span></div><div class="bd" id="modal_bd"></div></div></div>
<script>
let S=null, selRepo=null, selWt=null, selSess=null, msgs=[], TURN=null, turns=[];
let view='chat', boardApp='all', boardKey='', CMDS=[], cmdIdx=0;
const TURNSEQ=[0];
const foldMemo={};
const foldKey=t=>'F'+t.length+':'+t.slice(0,80);
const CTXLIM=[[/(gemini|1m)/i,1e6],[/(gpt-5|gpt-4\.1|o[34]|grok)/i,4e5],
 [/(qwen|kimi|llama)/i,256e3],[/(claude|glm|deepseek|mistral|mini)/i,2e5]];
function ctxLim(m){if(!m)return 2e5;for(const[r,l]of CTXLIM)if(r.test(m))return l;return 2e5;}
const ktop=t=>{t=t||0;return t>=1e6?(t/1e6).toFixed(1)+'M':Math.round(t/1e3)+'k';};
const esc=s=>String(s??'').replace(/[&<>"'\0]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;','\0':''}[c]));
const mb=r=>r==null||r===undefined?'—':(r/1048576).toFixed(0)+' MB';
const ago=ts=>{if(!ts)return'';const d=(Date.now()-ts)/1000;
 return d<60?`${d|0}s`:(d<3600?`${d/60|0}m`:(d<86400?`${d/3600|0}h`:`${d/86400|0}d`))};
const $=id=>document.getElementById(id);
const TRUNKS=['main','castle','master','trunk'];
const STATUS_ORDER=__STATUS_ORDER__;
function repo(){return S.repos.find(r=>r.key===selRepo)||S.repos[0]}
function hashState(){
  const h=location.hash.replace(/^#/,'');const p={};
  for(const kv of h.split('&')){const i=kv.indexOf('=');
   if(i>0){try{p[kv.slice(0,i)]=decodeURIComponent(kv.slice(i+1));}catch(e){}}
  }
  return p;
}
function syncHash(){
 const p=[];
 if(selRepo)p.push('app='+encodeURIComponent(selRepo));
 if(selWt)p.push('wt='+encodeURIComponent(selWt));
 if(selSess)p.push('sess='+encodeURIComponent(selSess));
 history.replaceState(null,'','#'+p.join('&'));
}
function applyHash(){
 const p=hashState();let hit=false;
 if(p.app&&S.repos.some(r=>r.key===p.app)){selRepo=p.app;hit=true;}
 if(p.wt){
  const r=repo();
  const w=(r.worktrees||[]).find(x=>x.path===p.wt)||(r.worktrees||[]).find(x=>x.branch===p.wt);
  if(w){selWt=w.path;hit=true;
   const wi=(repo().worktrees||[]).find(x=>x.path===w.path);
   $('wt_label').innerHTML=esc(wi&&wi.branch||w.path.split('/').pop())+
    ` <span class="dim">· ${esc(w.path.replace('/Users/scrummage/Python/',''))}</span>`;
   $('wt_badge').innerHTML=wi&&wi.dirty?'<span class="badge">dirty</span>':'';
   renderNav();renderSessionsBar();
    if(p.sess&&/^[\w-]+$/.test(p.sess))openSess(p.sess);
    else{const s=sessionsFor(selWt);if(s.length)openSess(s[0].id);else newSess();}
   }
  }
  loadCmds();
  return hit;
}

 async function poll(){
 try{S=await (await fetch('/api/state')).json();}catch(e){return;}
 if(!selRepo&&S.repos.length){
  selRepo=(S.repos.find(r=>r.key==='app')||S.repos[0]).key;
  applyHash();renderNav();
 }else{renderNav();if(selWt)renderSessionsBar();}
  $('totals').innerHTML=`<span class="chip">sessions <b>${S.sessions.length}</b></span>
  <span class="chip">worktrees <b>${S.worktrees.length}</b></span>`+
  ((S.processes||[]).length?`<span class="chip" style="border-color:rgba(248,81,73,.4);cursor:pointer" onclick="openProcs()"><b style="color:var(--red)">${S.processes.length} flagged</b></span>`:'');
  $('t_rss').textContent=mb(S.proc_total_rss);
  const ty=S.today||{};
  $('t_today').innerHTML=`$ <b>${(ty.cost||0).toFixed(2)}</b> today`;
  $('clock').textContent=new Date().toLocaleTimeString();
  syncComposer();syncSessMeta();
  if(view==='board'){
   const key=boardApp+'|'+(S.quests||[]).map(q=>q.id+q.status+(q.dirty?'d':'')+(q.app||'')).join(',');
   if(key!==boardKey){boardKey=key;renderBoard();}}
}
function activeDirs(){const s=new Set();for(const a of (S.active||[]))if(a.dir)s.add(a.dir);return s;}
function renderNav(){
  const r=repo(); if(!r){$('nav').innerHTML='';return;}
  const act=activeDirs();
  let h=`<div class="appswitch">`+S.repos.map(x=>
   `<div class="app ${x.key===selRepo?'on':''}" data-app="${esc(x.key)}">${x.key}</div>`).join('')+`</div>`;
  h+=`<div class="repohead">${esc(r.name)} · ${r.worktrees.length} worktrees</div>`;
  const trunk=r.worktrees.find(w=>w.branch&&(w.branch===TRUNKS.find(t=>t===w.branch)||TRUNKS.includes(w.branch.split('/').pop())));
  if(trunk)h+=`<div class="castle ${selWt===trunk.path?'sel':''}" data-wt="${esc(trunk.path)}">
   <div class="name"><span class="dot"></span>${esc(r.name)} trunk${act.has(trunk.path)?' <span class="livedot" title="agent working"></span>':''}</div>
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
    const working=w&&act.has(w.path);
    const mark=(working?'<span class="livedot" title="agent working"></span>':'')+
     (w?(w.dirty?'<span class="badge">dirty</span>':''):'<span class="st">no wt</span>');
    h+=`<div class="q ${selWt&&w&&selWt===w.path?'sel':''}"${w?` data-wt="${esc(w.path)}"`:''} data-branch="${esc(b)}">
    <div class="row1">${esc(b.includes('/')?b.slice(b.indexOf('/')+1):b)}${mark}</div>
    <div class="row2">${esc(b)}</div></div>`;
   }
   if(secs[ns].length>30)h+=`<div class="q dim" style="cursor:default">… ${secs[ns].length-30} more</div>`;
  }
  $('nav').innerHTML=h;
}
$('nav').addEventListener('click',e=>{
  const app=e.target.closest('[data-app]');
  if(app){switchApp(app.dataset.app);return;}
  const wt=e.target.closest('[data-wt]');
  if(wt){pickWt(wt.dataset.wt||null,wt.dataset.branch||null);}
});
function switchApp(key){selRepo=key;selWt=null;selSess=null;msgs=[];
 $('wt_label').textContent='select a worktree';$('wt_badge').innerHTML='';
 $('c_cont').innerHTML='new session — pick a worktree, or click a session tab to continue it';
 CMDS=[];renderCmdList();
 renderNav();renderTranscript();syncHash();syncComposer();}
let cmdSeq=0;
async function loadCmds(){
 const seq=++cmdSeq;
 if(!selWt){CMDS=[];renderCmdList();return;}
 let c=[];
 try{c=await (await fetch('/api/commands?dir='+encodeURIComponent(selWt))).json();}
 catch(e){}
 if(seq===cmdSeq){CMDS=c;renderCmdList();}
}
function pickWt(path,branch){
 selWt=path;selSess=null;msgs=[];
 if(path){
  const w=(repo().worktrees||[]).find(x=>x.path===path);
  $('wt_label').innerHTML=esc(branch||w&&w.branch||path.split('/').pop())+
   ` <span class="dim">· ${esc(path.replace('/Users/scrummage/Python/',''))}</span>`;
  $('wt_badge').innerHTML=w&&w.dirty?'<span class="badge">dirty</span>':'';
 }else{
  $('wt_label').innerHTML=esc(branch||'?')+' <span class="dim">· no worktree</span>';
  $('wt_badge').innerHTML='';
 }
 renderNav();renderSessionsBar();renderTranscript();syncComposer();syncSessMeta();
 $('c_cont').innerHTML='new session — pick a worktree, or click a session tab to continue it';
 const sess=sessionsFor(path);
 if(sess.length)openSess(sess[0].id);
 else{selSess=null;renderTranscript();
  $('c_cont').innerHTML='new session — no sessions in this worktree yet';}
 loadCmds();
 syncHash();
}
function sessionsFor(path){
 return S.sessions.filter(s=>s.directory&&(s.directory===path||s.directory.startsWith(path+'/')));
}
function sessMeta(s){
 return `tok ${ktop((s.tokens_input||0)+(s.tokens_output||0))} · $${(s.cost||0).toFixed(2)}`;
}
function renderSessionsBar(){
  const sess=sessionsFor(selWt).slice(0,8);
  $('sess_tabs').innerHTML=
   sess.map(s=>`<div class="tab ${selSess===s.id?'on':''}" data-sid="${esc(s.id)}"
    title="${esc(s.title||s.id)} · ${sessMeta(s)}"><span class="tl">${esc(s.agent||'?')}: ${esc((s.title||s.id).slice(0,26))}</span>`+
    `<span class="tx" title="delete session" data-act="del" data-sid="${esc(s.id)}">✕</span></div>`).join('')+
    `<div class="tab newtab ${selSess===null?'on':''}" onclick="newSess()" title="start a fresh session">＋ new</div>`;
}
$('sess_tabs').addEventListener('click',e=>{
  const del=e.target.closest('[data-act="del"]');
  if(del){delSess(del.dataset.sid);return;}
  const tab=e.target.closest('[data-sid]');
  if(tab)openSess(tab.dataset.sid);
});
function newSess(){selSess=null;msgs=[];
 $('c_cont').innerHTML='new session in <b>'+esc(selWt?selWt.replace('/Users/scrummage/Python/',''):'?')+'</b> <span class="dim">— agent replies as a fresh session</span>';
 renderSessionsBar();renderTranscript();syncHash();syncComposer();syncSessMeta();}
let sessOpenSeq=0;
async function openSess(id){
 const seq=++sessOpenSeq;
 selSess=id;msgs=[];
 $('c_cont').innerHTML=`continuing <b>${esc(id.slice(0,24))}…</b> <span class="x" onclick="newSess()">start new instead</span>`;
 renderSessionsBar();renderTranscript();syncHash();syncComposer();syncSessMeta();
 $('transcript').innerHTML='<div class="notice">loading…</div>';
 let m=[];
 try{m=await (await fetch('/api/session?id='+id)).json();}catch(e){}
 if(seq!==sessOpenSeq||selSess!==id)return;
 msgs=m;
 renderTranscript();
 syncSessMeta();
}
let sessMetaSeq=0;
async function syncSessMeta(){
 const sid=selSess;const seq=++sessMetaSeq;
 if(!sid){$('sess_meta').innerHTML='';$('sess_del').style.display='none';return;}
 $('sess_del').style.display='';
 let m={};
 try{m=await (await fetch('/api/session/meta?id='+sid)).json();}catch(e){}
 if(seq!==sessMetaSeq||selSess!==sid)return;
 const lim=m.ctx_limit||ctxLim(m.model||'');
 const pct=m.ctx?` (${Math.min(100,Math.round(100*m.ctx/lim))}%)`:'';
 $('sess_meta').innerHTML=`ctx <b>${ktop(m.ctx||0)}</b>/${ktop(lim)}${pct} · $${(m.cost||0).toFixed(2)}`;
}
function msgHTML(m){
  if(!m.text)return '';
  let cls,content;
  if(m.role==='user'){cls='user';content=esc(m.text);}
  else if(m.role==='reasoning'){cls='thinking';content=mdRender(m.text);}
  else if(m.role==='assistant'){cls='assistant';content=mdRender(m.text);}
  else{cls='tool';content=esc(m.text);}
  const md=cls==='assistant'||cls==='thinking';
  const k=cls==='thinking'?foldKey(m.text):null;
  const fold=k&&foldMemo[k]===true?' folded':'';
  return `<div class="msg ${cls}${md?' md':''}${fold}"${k?` data-fk="${esc(k)}"`:''}><div class="who">${esc(m.role)}</div>${content}</div>`;
}
function renderTranscript(){
  const t=$('transcript');
  delete t.dataset.turn;
  if(!msgs.length){t.innerHTML='<div class="empty-note">no messages yet — say something below</div>';return;}
  t.innerHTML=msgs.map(msgHTML).join('')||'<div class="empty-note">no text messages in this session yet</div>';
  t.scrollTop=t.scrollHeight;
}
function turnForView(){
  for(let i=turns.length-1;i>=0;i--){
    const T=turns[i];
    if(T.wt!==selWt)continue;
    if(T.sess){if(selSess===T.sess)return T;continue;}
    if(selSess===null||selSess===T.sid)return T;
  }
  return null;
}
function blockHTML(b){
  const md=b.cls==='assistant'||b.cls==='thinking';
  const k=b.cls==='thinking'?foldKey(b.text):null;
  const fold=k&&foldMemo[k]===true?' folded':'';
  return `<div class="msg ${b.cls}${md?' md':''}${fold}"${k?` data-fk="${esc(k)}"`:''}>`+
   `<div class="who">${b.cls==='thinking'?'reasoning':b.cls}</div>`+
   (md?mdRender(b.text):esc(b.text))+'</div>';
}
function renderLive(T0){
  const T=T0||turnForView();if(!T)return;
  const t=$('transcript');
  const near=t.scrollHeight-t.scrollTop-t.clientHeight<90;
  if(t.dataset.turn!==String(T.id)){
   t.dataset.turn=String(T.id);
   t.innerHTML=T.histHTML||'';
   T.shown=0;T.spinEl=null;
  }
  if(!T.spinEl){
   const d=document.createElement('div');
   d.className='msg notice spinline';
   d.innerHTML='working… <span class="dots"><i></i><i></i><i></i></span> <span class="spint">0</span>s';
   t.appendChild(d);T.spinEl=d;
  }
  for(let i=T.shown;i<T.blocks.length;i++){
   const b=T.blocks[i];
   T.shown=i+1;
   if(b.cls==='spin'||!b.text)continue;
   T.spinEl.insertAdjacentHTML('beforebegin',blockHTML(b));
  }
  const s=((Date.now()-T.t0)/1000)|0;
  T.spinEl.innerHTML=(T.blocks.length<=1?'dispatching…':'working…')+
   ' <span class="dots"><i></i><i></i><i></i></span> <span class="spint">'+s+'</span>s';
  if(near)t.scrollTop=t.scrollHeight;
}
function mdInline(s){
 s=s.replace(/`([^`\n]+)`/g,(m,c)=>'<code class="ic">'+c+'</code>');
 s=s.replace(/\*\*([^*\n]+)\*\*/g,'<b>$1</b>');
 s=s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\*)/g,'$1<i>$2</i>');
 s=s.replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
 return s;
}
function mdTable(rows){
 const cells=r=>r.replace(/^\s*\|/,'').replace(/\|\s*$/,'')
  .split(/(?<!\\)\|/).map(c=>mdInline(c.trim().replace(/\\\|/g,'|')));
 let body=rows.slice(1);
 if(body.length&&/^[\s:|-]+$/.test(body[0]))body=body.slice(1);
 return '<table><thead><tr>'+cells(rows[0]).map(c=>'<th>'+c+'</th>').join('')+'</tr></thead><tbody>'+
  body.map(r=>'<tr>'+cells(r).map(c=>'<td>'+c+'</td>').join('')+'</tr>').join('')+'</tbody></table>';
}
function codeHTML(b){
 return `<div class="codebox"><div class="codebar"><span>${esc(b.lang||'code')}</span>`+
  `<span class="cpy" onclick="copyCode(this)">copy</span></div>`+
  `<pre><code>${b.code.replace(/\n$/,'')}</code></pre></div>`;
}
function copyCode(el){
 const c=el.closest('.codebox').querySelector('code').textContent;
 navigator.clipboard.writeText(c).then(()=>{el.textContent='copied';
  setTimeout(()=>{el.textContent='copy';},1200);});
}
function mdRender(src){
 src=esc(src);
 const blocks=[];
 src=src.replace(/```([^\n`]*)\n?([\s\S]*?)```/g,(m,lang,code)=>{
  blocks.push({lang:lang.trim(),code});return '\x00B'+(blocks.length-1)+'\x00';});
 const lines=src.split('\n');
 const out=[];let para=[],list=null,quote=null,rows=null;
 const closeList=()=>{if(list){out.push('<'+list.tag+'>'+list.items.map(x=>'<li>'+mdInline(x)+'</li>').join('')+'</'+list.tag+'>');list=null;}};
 const flush=()=>{closeList();
  if(para.length){out.push('<p>'+mdInline(para.join('<br>'))+'</p>');para=[];}
  if(quote!==null){out.push('<blockquote>'+mdInline(quote.join('<br>'))+'</blockquote>');quote=null;}
  if(rows){out.push(mdTable(rows));rows=null;}};
 for(let i=0;i<lines.length;i++){
  const ln=lines[i];
   const bm=ln.match(/^\x00B(\d+)\x00\s*$/);
   if(bm&&blocks[+bm[1]]){flush();out.push(codeHTML(blocks[+bm[1]]));continue;}
  if(/^\s*$/.test(ln)){flush();continue;}
  if(quote!==null||/^(&gt;|>)\s?/.test(ln)){
   if(quote===null)quote=[];
   quote.push(ln.replace(/^(&gt;|>)\s?/,''));continue;}
  if(/^\s*(?:[-*_]\s*){3,}$/.test(ln)){flush();out.push('<hr>');continue;}
  const hm=ln.match(/^(#{1,4})\s+(.*)$/);
  if(hm){flush();const lv=hm[1].length+2;out.push('<h'+lv+'>'+mdInline(hm[2])+'</h'+lv+'>');continue;}
  if(rows===null&&ln.includes('|')&&i+1<lines.length&&
     lines[i+1].includes('-')&&/^\s*\|?[\s:|-]+\|?\s*$/.test(lines[i+1])){
   flush();rows=[ln];i++;rows.push(lines[i]);continue;}
  if(rows!==null&&ln.includes('|')){rows.push(ln);continue;}
  if(rows!==null)flush();
  const lm=ln.match(/^\s*[-*+]\s+(.+)$/);
  const om=ln.match(/^\s*\d+[.)]\s+(.+)$/);
  if(lm||om){
   if(para.length||quote!==null)flush();
   const tag=lm?'ul':'ol';
   if(list&&list.tag!==tag)closeList();
   if(!list)list={tag,items:[]};
   list.items.push((lm||om)[1]);continue;}
  closeList();para.push(ln);
 }
 flush();
 return out.join('');
}
$('transcript').addEventListener('click',e=>{
  const m=e.target.closest('.msg.thinking');
  if(!m||e.target.closest('.codebox'))return;
  m.classList.toggle('folded');
  const k=m.dataset.fk;if(k)foldMemo[k]=m.classList.contains('folded');});
function syncComposer(){
  const t=turnForView();
  const running=!!t;
  $('c_send').textContent=running?'QUEUE':'SEND';
  $('c_stop').style.display=running&&t&&t.job?'':'none';
  $('live_chip').style.display=running?'':'none';
}
function sendComposer(){
  const ta=$('c_prompt');
  const txt=ta.value.trim();
  if(!txt)return;
  const t=turnForView();
  if(t){
   if(t.queue){
    t.blocks.push({cls:'notice',text:'already queued — one queued message per turn'});
    if(turnForView()===t)renderLive();
    return;}
   t.queue={wt:t.wt,agent:$('c_agent').value,model:$('c_model').value.trim(),prompt:txt};
   ta.value='';autosizeTa();
   t.blocks.push({cls:'notice',
    text:'queued next: '+txt.slice(0,80)+(txt.length>80?'…':'')});
   if(turnForView()===t)renderLive();
   return;}
  if(!selWt){
   $('c_cont').innerHTML='<span style="color:var(--red)">select a worktree with sessions (left) before sending — cannot dispatch without a directory</span>';
   return;}
  ta.value='';autosizeTa();
  dispatch(selWt,selSess,$('c_agent').value,$('c_model').value.trim(),txt);
}
function autosizeTa(){
 const ta=$('c_prompt');
 ta.style.height='auto';
 ta.style.height=Math.min(140,Math.max(46,ta.scrollHeight))+'px';
}
async function dispatch(wt,sess,agent,model,prompt){
  const T={id:++TURNSEQ[0],wt,sess,sid:null,job:null,evs:0,ran:false,done:false,
   t0:Date.now(),blocks:[{cls:'user',text:prompt}],queue:null,
   histHTML:msgs.map(msgHTML).join(''),stopping:false,stopped:false,
   shown:0,spinEl:null};
  TURN=T;turns.push(T);
  syncComposer();
  try{
   if(typeof Notification!=='undefined'&&Notification.permission==='default')
    Notification.requestPermission();
  }catch(e){}
  if(turnForView()===T)renderLive();
  try{
   const r=await fetch('/api/send',{method:'POST',body:JSON.stringify({
    dir:wt,agent,prompt,session_id:sess||null,model})});
   if(!r.ok){
    T.blocks.push({cls:'error',text:'refused: '+await r.text()});
    await finishTurn(T,null,null);return;}
   T.job=(await r.json()).job;T.ran=true;
  }catch(e){
   T.blocks.push({cls:'error',text:'dispatch failed: '+e});
   await finishTurn(T,null,null);return;}
  let err=null,lastSt=null;
  try{
   while(true){
    try{lastSt=await (await fetch('/api/send/'+T.job)).json();}
    catch(e){err=e;break;}
    if(lastSt.error){
     T.blocks.push({cls:'error',text:lastSt.error});lastSt=null;break;}
    const evs=(lastSt.events||[]).slice(T.evs);
    T.evs=(lastSt.events||[]).length;
    for(const e of evs){
     if(e.type==='text'&&e.text)T.blocks.push({cls:'assistant',text:e.text});
     else if(e.type==='reasoning'&&e.text)T.blocks.push({cls:'thinking',text:e.text});
     else if(e.type==='status')T.blocks.push({cls:'notice',text:e.text});
     else if(e.type==='step')T.blocks.push({cls:'notice',text:e.text});
     else if(e.type==='step_finish')T.blocks.push({cls:'notice',text:e.text||'step done'});
     else if(e.type==='tool')T.blocks.push({cls:'tool',
      text:'tool · '+e.tool+' '+(e.brief||'')});
     else if(e.type==='error')T.blocks.push({cls:'error',text:e.text||'unknown error'});
    }
    if(lastSt.sid)T.sid=lastSt.sid;
    if(turnForView()===T)renderLive();
    if(lastSt.done)break;
    await new Promise(res=>setTimeout(res,700));
   }
  }catch(loopErr){
   T.blocks.push({cls:'error',text:'console error: '+loopErr});lastSt=null;}
  await finishTurn(T,err,lastSt);
}
async function finishTurn(T,err,lastSt){
  const wasViewing=turnForView()===T;
  T.done=true;
  turns=turns.filter(x=>x!==T);
  const exit=lastSt&&typeof lastSt.exit==='number'?lastSt.exit:null;
  if(err)T.blocks.push({cls:'error',text:'stream failed: '+err});
  else if(T.stopped)T.blocks.push({cls:'notice',text:'stopped by user'});
  else if(exit&&exit!==0)T.blocks.push({cls:'error',
   text:'turn process exited with code '+exit+' — see notices above'});
  if(wasViewing)renderLive(T);
  if(T.spinEl){T.spinEl.remove();T.spinEl=null;}
  syncComposer();
  if(!wasViewing||document.hidden){
   const secs=((Date.now()-T.t0)/1000)|0;
   const failed=!!err||(!!exit&&exit!==0);
   const ok=!failed&&!T.stopped;
   document.title=(ok?'✓':'✕')+' turn '+(ok?'done':'failed')+' — Court Console';
   if(window.notifyTO)clearTimeout(window.notifyTO);
   window.notifyTO=setTimeout(()=>{document.title='Court Console';},8000);
   try{
    if(typeof Notification!=='undefined'&&Notification.permission==='granted')
     new Notification(ok?'Agent turn done':'Agent turn failed',
      {body:(T.wt||'').split('/').pop()+' · '+secs+'s'});
   }catch(e){}
  }
  window.addEventListener('focus',()=>{document.title='Court Console';},{once:true});
  const q=(T.ran&&T.queue)?T.queue:null;
  if(wasViewing&&T.ran&&(T.sid||T.sess)){
   await openSess(T.sid||T.sess);
   const t=$('transcript');
   const secs=((Date.now()-T.t0)/1000)|0;
   const msg=err?'stream failed · '+secs+'s'
    :(exit&&exit!==0&&!T.stopped)?'turn failed (exit '+exit+') · '+secs+'s'
    :(T.stopped?'stopped':'turn complete')+' · '+secs+'s';
   t.insertAdjacentHTML('beforeend','<div class="msg notice">'+esc(msg)+'</div>');
   t.scrollTop=t.scrollHeight;
  }else{poll();}
  if(q){await dispatch(q.wt,T.sid||T.sess,q.agent,q.model,q.prompt);return;}
}
async function stopTurn(){
  const T=turnForView();
  if(!T||!T.job||T.stopping)return;
  T.stopping=true;
  T.blocks.push({cls:'notice',text:'stop requested…'});
  if(turnForView()===T)renderLive();
  try{await fetch('/api/stop',{method:'POST',body:JSON.stringify({job:T.job})});
   T.stopped=true;}
  catch(e){T.stopping=false;}
}
async function delSess(id){
 if(!id)return;
 const s=(S.sessions||[]).find(x=>x.id===id);
 const label=s&&s.title?s.title.slice(0,40):id;
 if(!confirm('Delete session "'+label+'"? This permanently removes its messages from kilo.db.'))return;
 try{
  const r=await fetch('/api/session/delete',{method:'POST',
   body:JSON.stringify({id})});
  if(!r.ok){alert('refused: '+(await r.text()));return;}
 }catch(e){alert('delete failed: '+e);return;}
 if(S)S.sessions=(S.sessions||[]).filter(x=>x.id!==id);
 if(selSess===id){
  selSess=null;msgs=[];
  const next=sessionsFor(selWt).find(x=>x.id!==id);
  if(next)await openSess(next.id);else newSess();
 }
 poll();
}
function cmdMatches(){
 const v=$('c_prompt').value;
 if(!v.startsWith('/'))return[];
 const word=v.slice(1).split(/\s/)[0].toLowerCase();
 if(v.includes(' '))return[];
 return CMDS.filter(c=>c.name.toLowerCase().startsWith(word));
}
function renderCmdList(){
 const el=$('cmdlist');
 if(view!=='chat'){el.style.display='none';return;}
 const m=cmdMatches();
 if(!m.length){el.style.display='none';return;}
 if(cmdIdx>=m.length||cmdIdx<0)cmdIdx=0;
 el.innerHTML=m.map((c,i)=>`<div class="cmdrow ${i===cmdIdx?'on':''}"
  data-name="${esc(c.name)}"><span class="cmdname">/${esc(c.name)}</span>`+
  `<span class="cmddesc">${esc(c.desc||'')}${c.agent?' · '+esc(c.agent):''}</span></div>`).join('');
 el.style.display='';
}
function completeCmd(name){
 const ta=$('c_prompt');
 ta.value='/'+name+' ';ta.focus();
 renderCmdList();
}
function setView(v){
 view=v;
 $('chat').style.display=v==='chat'?'':'none';
 $('board').style.display=v==='board'?'flex':'none';
 $('v_chat').classList.toggle('on',v==='chat');
 $('v_board').classList.toggle('on',v==='board');
 renderCmdList();
 renderNav();
 if(v==='board'){boardKey='';renderBoard();}
}
function boardFilter(a){boardApp=a;renderBoard();}
function openQuestWt(p){
 const r=(S.repos||[]).find(r=>(r.worktrees||[]).some(w=>w.path===p));
 if(r)selRepo=r.key;
 setView('chat');
 if(p&&((repo().worktrees||[]).some(w=>w.path===p)))pickWt(p);
 else poll();
}
const BOARD_OPS={WORKING:[["goad","goad","go"]],
 TRIBUTE_READY:[["coin","coin","go"],["advance","advance","GATE",""]],
 GATE:[["collect","collect","go"]],
 READY_TO_RAZE:[["raze","raze","warn"]],
 PLANNED:[["dispatch","dispatch","go"]]};
function renderBoard(){
 if(view!=='board')return;
 const qs=S.quests||[];
 const apps={};for(const q of qs){const a=q.app||q.repo||'other';apps[a]=(apps[a]||0)+1;}
 if(boardApp!=='all'&&!(boardApp in apps))boardApp='all';
 const act=activeDirs();
 const attn=q=>{const a=q.audit;return a&&(a.violations>0||a.pending_audience||a.forced_transition);};
 const nAttn=qs.filter(attn).length;
 let h='<div class="bfil"><span class="chipx '+(boardApp==='all'?'on':'')+'" data-app="all">all · '+qs.length+'</span>'+
  Object.keys(apps).sort().map(a=>`<span class="chipx ${a===boardApp?'on':''}" data-app="${esc(a)}">${esc(a)} · ${apps[a]}</span>`).join('')+
  (nAttn?`<span class="chipx" style="border-color:rgba(248,81,73,.5);color:var(--red)">${nAttn} need attention</span>`:'')+'</div>';
 $('boardbar').innerHTML=h;
 let cols='';
 for(const st of STATUS_ORDER){
  const items=qs.filter(q=>q.status===st&&(boardApp==='all'||(q.app||q.repo)===boardApp));
  cols+=`<div class="bcol"><h3>${esc(st.toLowerCase())} · ${items.length}</h3>`;
  for(const q of items){
   const on=!!(q.worktree&&act.has(q.worktree));
   const a=q.audit;
   const ops=BOARD_OPS[q.status]||[];
   let chips='';
   if(a){
    if(a.tasks_total)chips+=`<span class="bchip ${a.tasks_pct>=100?'ok':''}">${a.tasks_done}/${a.tasks_total} tasks</span>`;
    chips+=a.tribute_present?'<span class="bchip ok">tribute</span>':'';
    if(a.violations)chips+=`<span class="bchip bad">${a.violations} viol</span>`;
    if(a.pending_audience)chips+='<span class="bchip warn">audience</span>';
    if(a.forced_transition)chips+='<span class="bchip warn">forced</span>';
    if(a.commutation_done)chips+='<span class="bchip ok">commuted</span>';
   }
   cols+=`<div class="bcard ${q.worktree?'click':''} ${attn(q)?'attn':''}" ${q.worktree?`data-wt="${esc(q.worktree)}"`:''}>
    <div class="bid">${esc(q.id)}${q.dirty?' <span class="badge">dirty</span>':''}${on?' <span class="livedot" title="agent working"></span>':''}</div>
    <div class="bt">${esc(q.title||'')}</div>
    ${chips?`<div class="bchips">${chips}</div>`:''}
    ${ops.length?`<div class="bops">${ops.map(o=>
     `<button class="${o[2]==='warn'?'warn':'go'}" data-op="${o[0]}" data-id="${esc(q.id)}"${o[0]==='advance'?` data-status="${o[3]}"`:''}>${esc(o[1])}</button>`).join('')}</div>`:''}
    <div class="brow"><span class="chip">${esc(q.app||q.repo||'—')}</span>`+
    `<span class="bmono">${esc(q.branch||'')}</span></div></div>`;
  }
  cols+=items.length?'':'<div class="bempty">—</div>';
  cols+='</div>';
 }
 $('boardcols').innerHTML=cols;
}
$('boardbar').addEventListener('click',e=>{
 const c=e.target.closest('[data-app]');if(c)boardFilter(c.dataset.app);});
$('boardcols').addEventListener('click',e=>{
 const op=e.target.closest('[data-op]');
 if(op){courtOp(op.dataset.op,op.dataset.id,op.dataset.status||'');return;}
 const c=e.target.closest('[data-wt]');if(c)openQuestWt(c.dataset.wt);});
function courtOp(op,id,status){
 const verb={goad:'GOAD (spawns a serf turn in its worktree)',
  coin:'COIN (runs a Master-of-Coin audit session)',
  collect:'COLLECT (packs the GATE convoy)',
  raze:'RAZE (verify merge + queue for teardown)',
  dispatch:'DISPATCH --standup (creates worktree + starts serf)',
  advance:'ADVANCE to '+status}[op];
 if(!confirm(verb+'\n\n'+id+' — proceed?'))return;
 fetch('/api/court',{method:'POST',body:JSON.stringify({op,id,status})})
  .then(r=>r.json()).then(d=>{
   if(d.error){alert('refused: '+d.error);return;}
   watchJob(d.job,op.toUpperCase()+' '+id);}).catch(e=>alert('failed: '+e));
}
let JOBW=null;
function watchJob(job,title){
 openModal(esc(title||'court op'),'<div id="jobout">starting…</div>'+
  '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">'+
  '<button id="jobstop">STOP</button><button id="jobclose">CLOSE</button></div>');
 $('jobclose').onclick=()=>{closeModal();};
 $('jobstop').onclick=()=>{fetch('/api/stop',{method:'POST',body:JSON.stringify({job})});};
 if(JOBW)clearInterval(JOBW.iv);
 JOBW={job,shown:0,iv:setInterval(pollJob,700)};
 pollJob();
}
async function pollJob(){
 if(!JOBW)return;
 let st;
 try{st=await (await fetch('/api/send/'+JOBW.job+'?since='+JOBW.shown)).json();}
 catch(e){return;}
 const out=$('jobout');if(!out){clearInterval(JOBW.iv);JOBW=null;return;}
 const evs=st.events||[];
 JOBW.shown=st.total!=null?st.total:(JOBW.shown+evs.length);
 for(const ev of evs){
  if(!ev.text)continue;
  const d=document.createElement('div');
  if(ev.type==='error'){d.className='err';}
  d.textContent=ev.text;
  out.appendChild(d);
 }
 out.scrollTop=out.scrollHeight;
 if(st.done){
  clearInterval(JOBW.iv);JOBW=null;
  out.insertAdjacentHTML('beforeend',
   `<div class="${st.exit?'err':''}">— finished (exit ${st.exit==null?'?':st.exit}) —</div>`);
  poll();
 }
}
document.getElementById('c_prompt').addEventListener('input',()=>{cmdIdx=0;autosizeTa();renderCmdList();});
document.getElementById('c_prompt').addEventListener('keydown',e=>{
 const list=$('cmdlist');
 const listOpen=list.style.display!=='none';
 if(e.key==='Tab'&&listOpen){e.preventDefault();
  const m=cmdMatches();if(m.length)completeCmd(m[cmdIdx].name);return;}
 if(e.key==='ArrowDown'&&listOpen){e.preventDefault();
  const m=cmdMatches();if(!m.length)return;
  cmdIdx=(cmdIdx+1)%m.length;renderCmdList();return;}
 if(e.key==='ArrowUp'&&listOpen){e.preventDefault();
  const m=cmdMatches();if(!m.length)return;
  cmdIdx=(cmdIdx+m.length-1)%m.length;renderCmdList();return;}
 if(e.key==='Escape'&&listOpen){e.preventDefault();list.style.display='none';return;}
 if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){
  if(listOpen&&!$('c_prompt').value.includes(' ')){e.preventDefault();
   const m=cmdMatches();if(m.length)completeCmd(m[cmdIdx].name);return;}
  e.preventDefault();renderCmdList();sendComposer();}});
function openModal(title, body){
  document.querySelector('#modal .hd b').textContent=title;
  $('modal_bd').innerHTML=body;
  $('modal').classList.add('on');
}
function closeModal(){$('modal').classList.remove('on');}
function closeMcp(){closeModal();}
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
     :`<button title="takes effect for sessions started after the change" data-file="${esc(m.file)}" data-name="${esc(m.name)}">${m.enabled?'disable':'enable'}</button>`;
   return `<tr><td class="mono">${esc(m.name)}</td><td class="dim">${esc(m.scope)}</td><td class="dim">${esc(m.type)}</td>`+
    `<td>${m.enabled?'<span class="num" style="color:var(--green)">yes</span>':'<span class="dim">no</span>'}</td><td>${run}</td><td>${btn}</td></tr>`;
  }).join('')+'</tbody></table>'+
  '<div class="dim" style="padding-top:10px;font-size:11px">toggles edit the config file (a .bak copy is kept) and take effect for sessions started after the change</div>';
  openModal('MCP SERVERS — merged inventory', h);
}
$('modal_bd').addEventListener('click',e=>{
 const b=e.target.closest('[data-file]');
 if(b){mcpToggle(b.dataset.file,b.dataset.name);return;}
 const p=e.target.closest('[data-pid]');
 if(p){reapProc(p.dataset.pid);return;}
 const t=e.target.closest('[data-sid2]');
 if(t){jumpToSession(t.dataset.sid2);return;}
});
$('modal').addEventListener('click',e=>{
 if(e.target.id==='modal')closeModal();});
document.addEventListener('keydown',e=>{
 if(e.key==='Escape')closeModal();});
function reapProc(pid){
 if(!confirm('Terminate pid '+pid+'? (parent is a verified kilo process)'))return;
 fetch('/api/reap',{method:'POST',body:JSON.stringify({pid:parseInt(pid,10)})})
  .then(r=>r.json()).then(d=>{
   if(d.error){alert('refused: '+d.error);return;}
   poll();openProcs();}).catch(e=>alert('failed: '+e));
}
function openProcs(){
 const list=S.processes||[];
 let h=list.length?'<table><thead><tr><th>pid</th><th>rss</th><th>etime</th><th>args</th><th></th></tr></thead><tbody>'+
  list.map(p=>`<tr><td class="mono">${p.pid}</td><td class="num">${mb(p.rss)}</td><td class="mono">${esc(p.etime||'')}</td>`+
   `<td class="mono" style="max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.args.slice(0,140))}</td>`+
   `<td><button data-pid="${p.pid}">reap</button></td></tr>`).join('')+'</tbody></table>'
  :'<div class="empty-note">no flagged orphan processes right now</div>';
 openModal('ORPHANED PROCESSES — kilo children', h);
}
function openTurns(){
 openModal('RECENT TURNS — console + court ops', '<div id="turnlist">loading…</div>');
 fetch('/api/turns?limit=80').then(r=>r.json()).then(rows=>{
  const el=$('turnlist');
  el.innerHTML=rows.length?rows.map(t=>{
   const fail=t.exit&&t.exit!==0;
   const label=t.source==='court'?`${t.op} ${t.id||''}`:`${t.agent||'?'}`;
   const when=new Date(t.ts||0).toLocaleTimeString();
   return `<div class="turnrow ${fail?'fail':''}" ${t.sid?`data-sid2="${esc(t.sid)}"`:''}>
    <span class="tex" style="flex:none;font-weight:600">${fail?'✕':'✓'} ${esc(String(t.exit==null?'?':t.exit))}</span>
    <span style="flex:none">${esc(label)}</span>
    <span class="tmono">${esc((t.dir||'').split('/').slice(-2).join('/'))}</span>
    <span class="tmono" style="flex:none">${esc(when)} · ${t.duration_s==null?'?':t.duration_s}s</span>
    <span class="tmono" style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc((t.prompt_head||t.error_tail||'').slice(0,70))}</span>
   </div>`;}).join('')
  :'<div class="empty-note">no turns journaled yet</div>';
 }).catch(()=>{$('turnlist').textContent='failed to load';});
}
function openSearch(){
 openModal('SEARCH SESSIONS — kilo.db titles',
  '<input id="sq" placeholder="title / directory / agent — Enter to search" style="width:100%;background:var(--bg);border:1px solid var(--edge);color:var(--ink);border-radius:8px;padding:8px 10px;font-size:12.5px">'+
  '<div id="sres" style="margin-top:8px;max-height:50vh;overflow-y:auto"></div>');
 const inp=$('sq');
 inp.focus();
 inp.addEventListener('keydown',e=>{
  if(e.key==='Enter')doSearch(inp.value.trim());});
}
async function doSearch(q){
 const el=$('sres');if(!el)return;
 if(!q){el.innerHTML='';return;}
 el.innerHTML='<div class="dim">searching…</div>';
 let rows=[];
 try{rows=await (await fetch('/api/sessions?q='+encodeURIComponent(q))).json();}
 catch(e){el.textContent='search failed';return;}
 el.innerHTML=rows.length?rows.map(s=>
  `<div class="srow" data-sid2="${esc(s.id)}">
   <span style="flex:none;font-weight:600">${esc(s.agent||'?')}</span>
   <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc((s.title||s.id).slice(0,70))}</span>
   <span class="tmono">${esc((s.directory||'').split('/').slice(-2).join('/'))}</span>
   <span class="tmono" style="flex:none">$${(s.cost||0).toFixed(2)}</span>
  </div>`).join('')
 :'<div class="empty-note">no matches</div>';
}
function jumpToSession(sid){
 const s=(S.sessions||[]).find(x=>x.id===sid);
 closeModal();
 if(!s){alert('session not in recent list; use search');return;}
 const r=(S.repos||[]).find(r=>s.directory&&s.directory.startsWith(r.root));
 if(r)selRepo=r.key;
 if(s.directory){
  const w=(r&&r.worktrees||[]).find(w=>s.directory.startsWith(w.path));
  if(w){selWt=w.path;
   $('wt_label').innerHTML=esc(w.branch||w.path.split('/').pop())+
    ` <span class="dim">· ${esc(w.path.replace('/Users/scrummage/Python/',''))}</span>`;
  }
 }
 selSess=sid;
 setView('chat');
 openSess(sid);
 loadCmds();
 syncHash();
 poll();
}
async function mcpToggle(file,name){
 const m=(S.mcp||[]).find(x=>x.file===file&&x.name===name);
 if(!m)return;
 const r=await fetch('/api/mcp',{method:'POST',body:JSON.stringify({file,name,enabled:!m.enabled})});
 if(!r.ok)alert('refused: '+(await r.text()));else poll();
}
(async()=>{try{META=await (await fetch('/api/compose-meta')).json();
 $('c_agent').innerHTML=META.agents.map(x=>`<option>${x}</option>`).join('');
 if(META.models&&META.models.length)
  $('model_dl').innerHTML=META.models.map(m=>`<option value="${esc(m)}"></option>`).join('');
}catch(e){}})();
poll();setInterval(poll,5000);
</script></body></html>
"""
PAGE = PAGE.replace("__STATUS_ORDER__", json.dumps(STATUS_ORDER))


_DIRTY_CACHE = {}


def _wt_dirty(path, ttl=30):
    """TTL-cached dirtiness check for a single worktree path."""
    now = time.time()
    hit = _DIRTY_CACHE.get(path)
    if hit and now - hit[0] < ttl:
        return hit[1]
    dirty = False
    if path and os.path.isdir(path):
        try:
            r = subprocess.run(
                ["git", "-C", path, "status", "--porcelain"],
                capture_output=True, text=True, timeout=5)
            dirty = bool(r.stdout.strip())
        except Exception:
            pass
    _DIRTY_CACHE[path] = (now, dirty)
    return dirty


_QUESTS_CACHE = {}


def _quests(root, ttl=20):
    """Read <root>/.court/{quests,epics}/*.md frontmatter into card dicts."""
    now = time.time()
    hit = _QUESTS_CACHE.get(root)
    if hit and now - hit[0] < ttl:
        return hit[1]
    quests = []
    base = os.path.join(root, ".court")
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
            except OSError:
                continue
            qid = fm.get("id", fn[:-3])
            app = fm.get("app", "") or (qid.split("-")[1] if "-" in qid else "")
            wt = fm.get("worktree", "")
            if wt and not os.path.isabs(wt):
                wt = os.path.join(root, wt)
            quests.append({
                "id": qid, "title": fm.get("title", ""),
                "status": fm.get("status", "OPEN"), "app": app,
                "branch": fm.get("branch", ""), "worktree": wt,
                "epic": fm.get("parent_epic", ""),
                "dirty": _wt_dirty(wt),
            })
    quests.sort(key=lambda q: (
        STATUS_ORDER.index(q["status"]) if q["status"] in STATUS_ORDER else 99,
        q["id"]))
    with ThreadPoolExecutor(max_workers=8) as ex:
        for q, d in zip(quests, ex.map(_wt_dirty, [q["worktree"]
                                                   for q in quests])):
            q["dirty"] = d
    _QUESTS_CACHE[root] = (now, quests)
    return quests


def _all_quests():
    out = []
    for r in _repos():
        audit = _court_audit(r["root"])
        for q in _quests(r["root"]):
            a = audit.get(q["id"])
            if a:
                out.append(dict(q, repo=r["key"], audit=a))
            else:
                out.append(dict(q, repo=r["key"]))
    return out


_AUDIT_CACHE = {}  # repo root -> {"t": ts, "data": {quest_id: audit}, "busy": bool}
_AUDIT_LOCK = threading.Lock()


def _court_audit(root, ttl=120):
    """`court status --json` per repo, background-refreshed; returns the last
    good {quest_id: audit-subset} map (empty map until the first refresh)."""
    now = time.time()
    hit = _AUDIT_CACHE.get(root)
    if hit and now - hit["t"] < ttl and not hit.get("busy"):
        return hit["data"]
    with _AUDIT_LOCK:
        hit = _AUDIT_CACHE.get(root)
        if hit and (now - hit["t"] < ttl or hit.get("busy")):
            return hit["data"]
        entry = _AUDIT_CACHE.setdefault(root, {"t": 0.0, "data": {}})
        entry["busy"] = True

    def _bg():
        try:
            r = subprocess.run(
                ["python3", "-m", "court.cli", "status", "--json"],
                capture_output=True, text=True, timeout=120, cwd=root)
            data = json.loads(r.stdout or "{}")
            subset = {}
            for q in data.get("quests", []):
                tp = q.get("task_progress") or {}
                subset[q["id"]] = {
                    "tasks_done": tp.get("checked", 0),
                    "tasks_total": tp.get("total", 0),
                    "tasks_pct": tp.get("percent", 0),
                    "tribute_present": bool(q.get("tribute_present")),
                    "violations": len(q.get("violations") or []),
                    "warnings": len(q.get("warnings") or []),
                    "pending_audience": bool(q.get("pending_audience")),
                    "forced_transition": bool(q.get("forced_transition")),
                    "commutation_done": bool(q.get("commutation_done")),
                    "serf_session_id": q.get("serf_session_id", ""),
                }
            entry["t"] = time.time()
            entry["data"] = subset
        except Exception:
            entry["t"] = time.time()  # back off until next TTL window
        finally:
            entry["busy"] = False

    threading.Thread(target=_bg, daemon=True).start()
    return entry["data"]


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
    with lock:
        hit = _WT_CACHE.get(repo)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        return _compute_worktrees(repo)


def _compute_worktrees(repo):
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
            " tokens_input, tokens_output, tokens_cache_read,"
            " tokens_cache_write, cost from session"
            " order by time_updated desc limit ?", (limit,)).fetchall()
        db.close()
    except Exception:
        return []
    out = []
    for sid, title, agent, model, directory, tu, ti, to, tcr, tcw, cost in rows:
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
            "tokens_context": ti + tcr + tcw + to,
            "cost": round(cost or 0.0, 4),
        })
    return out


def _running_agents(procs=None):
    """All live `kilo run` agents from the process table, mapped to their
    worktree dir. Covers console-dispatched AND court-CLI (dispatch/goad/coin)
    spawns — the ps scan is the single source of truth."""
    if procs is None:
        procs = _ps_procs()
    out = []
    for p in procs:
        a = p["args"]
        if "kilo" not in a or " run " not in a and not a.endswith(" run"):
            continue
        m = re.search(r"--dir\s+(\S+)", a)
        if not m:
            continue
        am = re.search(r"--agent\s+(\S+)", a)
        out.append({"dir": m.group(1),
                    "agent": am.group(1) if am else "?",
                    "pid": p["pid"], "etime": p["etime"], "source": "ps"})
    return out


def _sessions_search(q, limit=40):
    q = (q or "").strip()
    if not os.path.exists(KILO_DB) or not q:
        return []
    like = f"%{q[:120]}%"
    try:
        db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=3)
        db.execute("pragma query_only=1")
        rows = db.execute(
            "select id, title, agent, model, directory, time_updated,"
            " cost from session where title like ? or directory like ?"
            " or agent like ? order by time_updated desc limit ?",
            (like, like, like, limit)).fetchall()
        db.close()
    except Exception:
        return []
    return [{"id": sid, "title": title or "", "agent": agent or "",
             "directory": directory or "", "time_updated": tu,
             "cost": round(cost or 0.0, 4)}
            for sid, title, agent, _model, directory, tu, cost in rows]


def _today_totals():
    """Aggregate cost/tokens for sessions active since local midnight."""
    if not os.path.exists(KILO_DB):
        return {"sessions": 0, "cost": 0.0, "tokens": 0}
    lt = time.localtime()
    midnight_ms = (time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                0, 0, 0, 0, 0, -1))) * 1000
    try:
        db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=3)
        db.execute("pragma query_only=1")
        n, cost, toks = db.execute(
            "select count(*), coalesce(sum(cost),0),"
            " coalesce(sum(tokens_input+tokens_output),0) from session"
            " where time_updated >= ?", (midnight_ms,)).fetchone()
        db.close()
    except Exception:
        return {"sessions": 0, "cost": 0.0, "tokens": 0}
    return {"sessions": n, "cost": round(cost or 0.0, 2), "tokens": toks or 0}


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
        try:
            procs.append({"pid": int(parts[0]), "ppid": int(parts[1]),
                          "rss": int(parts[2]) * 1024, "etime": parts[3],
                          "args": parts[4]})
        except ValueError:
            continue
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


_MCP_LOCK = threading.Lock()


def _mcp_toggle_write(file, name, enabled):
    """Flip one MCP server's enabled flag; returns (http_code, payload)."""
    if file.endswith(".jsonc"):
        return 400, {"error": "JSONC config (comments would be lost by rewrite); edit manually"}
    entry = next((e for e in _mcp_inventory()
                  if e["file"] == file and e["name"] == name), None)
    if entry is None:
        return 403, {"error": "unknown mcp config file or server; refused"}
    with _MCP_LOCK:
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
            srv = servers[name]
            if "disabled" in srv:
                if enabled:
                    srv.pop("disabled", None)
                    srv["enabled"] = True
                else:
                    srv["disabled"] = True
                    srv.pop("enabled", None)
            else:
                srv["enabled"] = enabled
            tmp = file + ".tmp"
            with open(tmp, "w") as f:
                f.write(json.dumps(data, indent=indent) + "\n")
            os.replace(tmp, file)
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
        parts = {}
        if mids:
            q = ",".join("?" * len(mids))
            for mid, pdata in db.execute(
                    f"select message_id, data from part where message_id in ({q})"
                    " order by time_created", mids):
                try:
                    p = json.loads(pdata)
                except Exception:
                    continue
                t = p.get("type")
                if t in ("text", "reasoning") and p.get("text"):
                    parts.setdefault(mid, []).append((t, p["text"]))
        db.close()
    except Exception:
        return []
    out = []
    for mid, role, tc in reversed(rows):
        for ptype, text in parts.get(mid, []):
            r = "reasoning" if ptype == "reasoning" else (role or "system")
            out.append({"role": r,
                        "time_created": tc,
                        "text": text[:4000]})
    return out


def _session_meta(sid):
    """True context-window usage (last step-finish tokens.total) + session cost."""
    if not os.path.exists(KILO_DB) or not re.fullmatch(r"[\w-]+", sid):
        return {}
    _ctx_limits_bg()
    out = {}
    try:
        db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=3)
        db.execute("pragma query_only=1")
        srow = db.execute(
            "select tokens_input, tokens_output, cost, model from session"
            " where id=?", (sid,)).fetchone()
        db.close()
    except Exception:
        return {}
    if srow:
        ti, to, cost, model = srow
        out = {"tokens_input": ti or 0, "tokens_output": to or 0,
               "cost": round(cost or 0.0, 4)}
        try:
            out["model"] = (json.loads(model) or {}).get("id", "") or str(model)
        except Exception:
            out["model"] = str(model or "")
        out["ctx_limit"] = _model_ctx_limit(out["model"])
    # bounded scan: newest 30 messages' parts only — never a full-session
    # json_extract pass over the 100GB DB
    try:
        db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=3)
        db.execute("pragma query_only=1")
        mids = [r[0] for r in db.execute(
            "select id from message where session_id=?"
            " order by time_created desc limit 30", (sid,))]
        if mids:
            q = ",".join("?" * len(mids))
            for (pdata,) in db.execute(
                    f"select data from part where message_id in ({q})"
                    " order by time_created desc", mids):
                try:
                    d = json.loads(pdata)
                except Exception:
                    continue
                if d.get("type") == "step-finish":
                    out["ctx"] = (d.get("tokens") or {}).get("total") or 0
                    break
        db.close()
    except Exception:
        pass
    return out


def _kilo_bin():
    """Latest kilo binary across installed extension versions (auto-update
    proof), falling back to PATH."""
    cands = glob.glob(os.path.expanduser(
        "~/.vscode/extensions/kilocode.kilo-code-*/bin/kilo"))
    if cands:
        return max(cands, key=lambda p: os.path.getmtime(os.path.dirname(
            os.path.dirname(p))))
    return shutil.which("kilo") or "kilo"


_CTX_FALLBACKS = [
    (re.compile(r"gemini", re.I), 1_000_000),
    (re.compile(r"gpt-5|gpt-4\.1|o[34]|grok", re.I), 400_000),
    (re.compile(r"qwen|kimi|llama", re.I), 256_000),
    (re.compile(r"claude|glm|deepseek|mistral|mini", re.I), 200_000),
]


def _ctx_fallback(model):
    for rx, lim in _CTX_FALLBACKS:
        if rx.search(model or ""):
            return lim
    return 200_000


def _model_ctx_limit(model):
    """Resolve a model id -> real context length (OpenRouter catalog,
    background-fetched and cached 24h); family heuristic as fallback."""
    limits = _CTX_LIMITS["map"]
    norm = (model or "").lower()
    for pre in ("openrouter/", "kilo/", "~"):
        if norm.startswith(pre):
            norm = norm[len(pre):]
    if norm in limits:
        return limits[norm]
    # alias tolerance: claude-sonnet-latest -> claude-sonnet-4 etc.
    base = re.sub(r"-latest$", "", norm)
    for mid, lim in limits.items():
        if mid == base or mid.startswith(base) or base.startswith(mid):
            return lim
    return _ctx_fallback(norm)


_CTX_LIMITS = {"t": 0.0, "map": {}, "busy": False}


def _ctx_limits_bg(ttl=86400):
    now = time.time()
    if _CTX_LIMITS["map"] and now - _CTX_LIMITS["t"] < ttl:
        return
    if _CTX_LIMITS["busy"]:
        return
    _CTX_LIMITS["busy"] = True

    def _fetch():
        try:
            import urllib.request
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/models",
                headers={"User-Agent": "court-console/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            out = {}
            for m in data.get("data", []):
                cl = m.get("context_length") or m.get("top_provider", {}).get(
                    "context_length")
                if m.get("id") and cl:
                    out[str(m["id"]).lower()] = int(cl)
            if out:
                _CTX_LIMITS["map"] = out
                _CTX_LIMITS["t"] = time.time()
        except Exception:
            pass
        finally:
            _CTX_LIMITS["busy"] = False

    threading.Thread(target=_fetch, daemon=True).start()


_ALLOWED_AGENTS = ("steward", "code", "serf", "scout", "artist")

TURN_JOURNAL = os.path.expanduser(
    "~/.local/share/kilo-castle/console_turns.jsonl")


def _journal_append(rec):
    try:
        os.makedirs(os.path.dirname(TURN_JOURNAL), exist_ok=True)
        try:
            if os.path.getsize(TURN_JOURNAL) > 2_000_000:
                os.replace(TURN_JOURNAL, TURN_JOURNAL + ".1")
        except OSError:
            pass
        with open(TURN_JOURNAL, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def _journal_tail(limit=100):
    out = []
    try:
        with open(TURN_JOURNAL) as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    out.append(ln)
    except OSError:
        return []
    out = out[-limit:]
    out.reverse()
    res = []
    for ln in out:
        try:
            res.append(json.loads(ln))
        except Exception:
            continue
    return res


_COURT_OP_ARITY = {
    "goad": ("goad", "{id}"),
    "coin": ("coin", "{id}"),
    "collect": ("collect",),
    "raze": ("raze", "{id}"),
    "dispatch": ("dispatch", "{id}", "--standup"),
}


def _find_quest_repo(qid):
    qid = (qid or "").strip()
    if not re.fullmatch(r"[QqEeSs]?\d[\w-]{0,60}", qid):
        return None
    matches = [q for q in _all_quests()
               if q["id"].lower() == qid.lower()
               or q["id"].lower().startswith(qid.lower())]
    if len(matches) != 1:
        return None
    for r in _repos():
        if r["key"] == matches[0]["repo"]:
            return {"quest": matches[0], "root": r["root"]}
    return None


def _court_op(job, op, qid, status, note):
    try:
        found = _find_quest_repo(qid)
        if not found and op == "collect":
            found = {"root": COURT_DIR}
        if not found:
            job["events"].append({"type": "error",
                                  "text": f"unknown or ambiguous quest id {qid!r}"})
            job["done"] = True
            job["ended"] = time.time()
            return
        root = found["root"]
        if op == "advance":
            if status not in STATUS_ORDER:
                job["events"].append({"type": "error",
                                      "text": f"status {status!r} not allowed"})
                job["done"] = True
                job["ended"] = time.time()
                return
            argv = ["python3", "-m", "court.cli", "advance", found["quest"]["id"],
                    status, "--note", note or "advanced via court console"]
        else:
            tmpl = _COURT_OP_ARITY[op]
            argv = ["python3", "-m", "court.cli"] + [
                a.replace("{id}", found["quest"]["id"] if found.get("quest") else "")
                for a in tmpl]
        job["events"].append({"type": "status", "text": "$ " + " ".join(argv) +
                              f"   (cwd {root})"})
        proc = subprocess.Popen(
            argv, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, start_new_session=True)
        job["proc"] = proc
        for ln in proc.stdout:
            ln = ln.rstrip()
            if ln:
                if len(ln) > 400:
                    ln = ln[:400] + " …"
                job["events"].append({"type": "text", "text": ln})
        # goad/coin/dispatch wrap a full agent turn — no timeout; stop via /api/stop
        long_op = op in ("goad", "coin", "dispatch")
        try:
            rc = proc.wait(None if long_op else 180)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            rc = -99
            job["events"].append({"type": "error", "text": "court op timed out"})
        job["exit"] = rc
        job["ended"] = time.time()
        job["done"] = True
        _journal_append({"ts": int(time.time() * 1000), "source": "court",
                         "op": op, "id": qid, "dir": root, "exit": rc,
                         "duration_s": round(job["ended"] - job["started"], 1)})
        _QUESTS_CACHE[root] = (0, {})  # force quest re-scan (status may have changed)
        _AUDIT_CACHE.pop(root, None)
    except Exception as exc:
        job["events"].append({"type": "error", "text": f"court op failed: {exc}"})
        job["exit"] = -98
        job["ended"] = time.time()
        job["done"] = True
_JOBS = {}
_JOB_SEQ = [0]


_MODEL_CACHE = {"t": 0.0, "list": []}


def _models(ttl=600):
    hit = _MODEL_CACHE
    if hit["list"] and time.time() - hit["t"] < ttl:
        return hit["list"]
    if not hit.get("refreshing"):
        hit["refreshing"] = True

        def _bg():
            try:
                r = subprocess.run(
                    [_kilo_bin(), "models"], capture_output=True, text=True,
                    timeout=15)
                out = sorted({ln.strip()
                              for ln in r.stdout.splitlines() if ln.strip()})
                if out:
                    hit["t"] = time.time()
                    hit["list"] = out
            except Exception:
                pass
            finally:
                hit["refreshing"] = False
        threading.Thread(target=_bg, daemon=True).start()
    return hit["list"]


def _known_dirs():
    dirs = {r["root"] for r in _repos()}
    for r in _repos():
        for w in _worktrees(r["root"]):
            dirs.add(w["path"])
    for s in _sessions(limit=200):
        if s["directory"] and os.path.isdir(s["directory"]):
            dirs.add(s["directory"])
    return sorted(dirs)


def _start_run(job, directory, agent, prompt, session_id, model=""):
    mode = f"continue {session_id[:18]}..." if session_id else "new session"
    job["sess"] = session_id
    job["events"].append({"type": "status", "text": (
        f"spawning agent - {agent} - {mode} - "
        f"{directory.replace('/Users/scrummage/Python/', '')}")})
    cmd = [_kilo_bin(), "run", "--dir", directory, "--agent", agent,
           "--format", "json", "--thinking", "--auto",
           "--title", prompt.strip()[:60] or "console turn"]
    if session_id:
        cmd += ["--session", session_id]
    if model:
        cmd += ["--model", model]
    elif not session_id:
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
    job["proc"] = proc

    def _reader():
        connected = False
        raw_tail = ""
        raw_last = []
        for line in proc.stdout:
            raw_tail = line[-300:]
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                raw_last = raw_last[-4:] + [line[-200:]]
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
                    if len(txt) > 4000:
                        txt = txt[:4000] + " …[truncated]"
                    job["events"].append({"type": "reasoning", "text": txt})
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
        if not connected:
            job["events"].append({"type": "error",
                                  "text": "agent exited before responding: " +
                                          (raw_tail or "(no output at all)")})
        elif raw_last:
            job["events"].append({"type": "error",
                                  "text": "process output tail: " +
                                          " | ".join(raw_last[-3:])[:400]})

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
    job["ended"] = time.time()
    job["done"] = True
    err_tail = ""
    for e in reversed(job["events"]):
        if e.get("type") == "error":
            err_tail = e.get("text", "")[:300]
            break
    _journal_append({
        "ts": int(time.time() * 1000), "source": "console",
        "dir": directory, "agent": agent, "model": model,
        "sid": job.get("sid"), "exit": rc,
        "duration_s": round(job["ended"] - job["started"], 1),
        "prompt_head": prompt[:200], "error_tail": err_tail})


def _validate_send(directory, agent):
    if agent not in _ALLOWED_AGENTS:
        return "agent not allowed"
    if directory not in _known_dirs():
        return "directory not a known worktree or repo; refused"
    return None


_CMD_DIRS = ("commands", "command")


def _command_files(directory):
    """Map command name -> md path; project dir wins over global config."""
    seen = {}
    roots = []
    if directory and os.path.isdir(directory):
        roots.append(os.path.join(directory, ".kilo"))
    roots.append(os.path.expanduser("~/.config/kilo"))
    for r in roots:
        for name in _CMD_DIRS:
            d = os.path.join(r, name)
            if not os.path.isdir(d):
                continue
            try:
                fns = sorted(os.listdir(d))
            except OSError:
                continue
            for fn in fns:
                if fn.endswith(".md"):
                    seen.setdefault(fn[:-3], os.path.join(d, fn))
    return seen


def _list_commands(directory):
    out = []
    for name, path in _command_files(directory).items():
        desc = agent = ""
        try:
            with open(path) as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        if lines and lines[0].strip() == "---":
            for ln in lines[1:]:
                if ln.strip() == "---":
                    break
                if ln.startswith("description:"):
                    desc = ln.split(":", 1)[1].strip()
                elif ln.startswith("agent:"):
                    agent = ln.split(":", 1)[1].strip()
        out.append({"name": name, "desc": desc, "agent": agent})
    return out


def _expand_command(prompt, directory):
    """Expand '/name args' into the command's template with $ARGUMENTS
    substituted. Returns ({"prompt": str, "agent": str|None}, error|None)."""
    m = re.match(r"^/([\w-]+)(?:\s+([\s\S]*))?$", prompt.strip())
    if not m:
        return None, "malformed command"
    name, rest = m.group(1), (m.group(2) or "").strip()
    path = _command_files(directory).get(name)
    if not path:
        return None, f"unknown command /{name}"
    try:
        with open(path) as f:
            text = f.read()
    except OSError as exc:
        return None, f"command read failed: {exc}"
    lines = text.splitlines()
    agent = ""
    if lines and lines[0].strip() == "---":
        end = -1
        for i, ln in enumerate(lines[1:], 1):
            if ln.strip() == "---":
                end = i
                break
            if ln.startswith("agent:"):
                agent = ln.split(":", 1)[1].strip()
        body = "\n".join(lines[end + 1:]).strip() if end >= 0 else text.strip()
    else:
        body = text.strip()
    if "$ARGUMENTS" in body:
        body = body.replace("$ARGUMENTS", rest)
    elif rest:
        body = body + "\n\n" + rest
    return {"prompt": body,
            "agent": agent if agent in _ALLOWED_AGENTS else None}, None


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
            from urllib.parse import parse_qs
            path, _, qstr = self.path.partition("?")
            job = _JOBS.get(path.rsplit("/", 1)[1])
            if not job:
                self._json({"error": "unknown job"}, 404)
            else:
                since = 0
                try:
                    since = int(parse_qs(qstr).get("since", ["0"])[0])
                except Exception:
                    since = 0
                evs = job["events"][since:] if since else job["events"]
                self._json({"events": evs, "total": len(job["events"]),
                            "done": job["done"],
                            "exit": job.get("exit"), "sid": job.get("sid")})
        elif self.path == "/api/compose-meta":
            self._json({"agents": list(_ALLOWED_AGENTS),
                        "dirs": _known_dirs(),
                        "models": _models()})
        elif self.path == "/api/state":
            procs = _ps_procs()
            flagged, total = _processes(procs)
            repos = [
                {"key": r["key"], "name": r["name"], "root": r["root"],
                 "branches": _branches(r["root"]),
                 "worktrees": _worktrees(r["root"])}
                for r in _repos()]
            active = {a["dir"]: a for a in _running_agents(procs)}
            for k, j in _JOBS.items():
                d = j.get("dir", "")
                if not j.get("done") and d and d not in active:
                    active[d] = {"dir": d, "agent": j.get("agent", "?"),
                                 "job": k, "source": "console",
                                 "started": j.get("started", 0)}
            # prune finished jobs after 10 minutes so _JOBS cannot grow unbounded
            cutoff = time.time() - 600
            for k in [k for k, j in _JOBS.items()
                      if j.get("done") and j.get("ended", 0) < cutoff]:
                _JOBS.pop(k, None)
            self._json({
                "repos": repos,
                "worktrees": [w for r in repos for w in r["worktrees"]],
                "sessions": _sessions(),
                "quests": _all_quests(),
                "today": _today_totals(),
                "active": sorted(active.values(),
                                 key=lambda a: a.get("started") or 0),
                "mcp": _mcp_inventory(procs),
                "processes": [
                    {k: p[k] for k in ("pid", "ppid", "rss", "etime", "args")}
                    for p in flagged],
                "proc_total_rss": total,
            })
        elif self.path.startswith("/api/commands"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            d = qs.get("dir", [""])[0]
            if d not in _known_dirs():
                self._json({"error": "unknown directory"}, 403)
            else:
                self._json(_list_commands(d))
        elif self.path.startswith("/api/session/meta"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            self._json(_session_meta(qs.get("id", [""])[0]))
        elif self.path.startswith("/api/turns"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            try:
                lim = min(int(qs.get("limit", ["60"])[0]), 200)
            except Exception:
                lim = 60
            self._json(_journal_tail(lim))
        elif self.path.startswith("/api/sessions"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            self._json(_sessions_search(qs.get("q", [""])[0]))
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
        elif self.path == "/api/stop":
            self._stop()
        elif self.path == "/api/court":
            self._court()
        elif self.path == "/api/session/delete":
            self._session_delete()
        else:
            self.send_error(404)

    def _court(self):
        try:
            body = self._read_body()
            op = str(body.get("op", ""))
            qid = str(body.get("id", ""))
            status = str(body.get("status", ""))
            note = str(body.get("note", ""))[:200]
        except Exception:
            self._json({"error": "bad request"}, 400)
            return
        if op not in _COURT_OP_ARITY and op != "advance":
            self._json({"error": "op not allowed"}, 403)
            return
        if op == "advance" and status not in STATUS_ORDER:
            self._json({"error": "status not allowed"}, 403)
            return
        _JOB_SEQ[0] += 1
        jid = _JOB_SEQ[0]
        job = {"events": [], "done": False, "started": time.time(),
               "dir": "", "agent": "court:" + op, "op": op, "qid": qid}
        _JOBS[str(jid)] = job
        threading.Thread(target=_court_op, daemon=True,
                         args=(job, op, qid, status, note)).start()
        self._json({"ok": True, "job": str(jid)})

    def _stop(self):
        try:
            jobid = self._read_body().get("job")
        except Exception:
            self._json({"error": "bad request"}, 400)
            return
        job = _JOBS.get(str(jobid))
        if not job:
            self._json({"error": "unknown job"}, 404)
            return
        if job.get("done"):
            self._json({"error": "turn already finished"}, 409)
            return
        proc = job.get("proc")
        if not proc:
            self._json({"error": "process gone"}, 409)
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        time.sleep(0.7)
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self._json({"ok": True, "stopped": str(jobid)})

    def _session_delete(self):
        try:
            sid = self._read_body().get("id")
        except Exception:
            self._json({"error": "bad request"}, 400)
            return
        if not isinstance(sid, str) or not re.fullmatch(r"[\w-]+", sid):
            self._json({"error": "bad session id"}, 400)
            return
        for j in _JOBS.values():
            if not j.get("done") and sid in (j.get("sess"), j.get("sid")):
                self._json(
                    {"error": "a turn is running in this session; stop it first"},
                    409)
                return
        try:
            r = subprocess.run([_kilo_bin(), "session", "delete", sid],
                               capture_output=True, text=True, timeout=120)
        except Exception as exc:
            self._json({"error": f"delete failed: {exc}"}, 500)
            return
        if r.returncode != 0:
            self._json(
                {"error": (r.stderr or r.stdout or "delete failed").strip()[:300]},
                500)
            return
        self._json({"ok": True, "deleted": sid})

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
        model = str(body.get("model") or "").strip()
        if model and (len(model) > 120 or not re.fullmatch(r"[\w.~@/-]+", model)):
            self._json({"error": "bad model id"}, 400)
            return
        if not prompt:
            self._json({"error": "empty prompt"}, 400)
            return
        err = _validate_send(directory, agent)
        if err:
            self._json({"error": err}, 403)
            return
        if prompt.startswith("/"):
            expanded, cerr = _expand_command(prompt, directory)
            if cerr:
                self._json({"error": cerr}, 400)
                return
            prompt = expanded["prompt"]
            if expanded["agent"]:
                agent = expanded["agent"]
        if session_id and not re.fullmatch(r"[\w-]+", session_id):
            self._json({"error": "bad session id"}, 400)
            return
        _JOB_SEQ[0] += 1
        jid = _JOB_SEQ[0]
        job = {"events": [], "done": False, "started": time.time(),
               "dir": directory, "agent": agent}
        _JOBS[str(jid)] = job
        threading.Thread(
            target=_start_run,
            args=(job, directory, agent, prompt, session_id, model),
            daemon=True).start()
        self._json({"ok": True, "job": str(jid)})

    def _read_body(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 1_000_000:
            raise ValueError("body too large")
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
            pid = int(pid)
        except (TypeError, ValueError):
            self._json({"error": "bad pid"}, 400)
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
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        time.sleep(1.0)
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self._json({"ok": True, "reaped": pid})


def serve(port=8300):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"court ui: http://127.0.0.1:{port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()
