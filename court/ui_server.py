"""Zero-dependency localhost web console for the court pipeline.

Reads: .court quest files, git worktrees, the local kilo session DB (read-only),
and the live process table. Mutating endpoints: /api/reap (terminates a process
whose parent is a verified kilo process), /api/mcp (flips the enabled flag of
an inventoried MCP server in its own config file, with a .bak backup),
/api/settings (validated merge into .court/config.json — role models, model
presets/aliases, suite/harness/freshness commands, no_kilo_mode — atomic with
a .bak), and /api/annotation (appends one studio-annotation JSON line to the
target worktree's .kilo/studio-annotations.jsonl; served CORS-open for the
managed studio browser).
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
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from court import git_ops

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
# Roles carried by the manifest's models map (mirrors court.config).
KNOWN_ROLE_MODELS = ("serf", "master_of_coin", "gatekeeper", "steward",
                     "artist", "scout")

PAGE = r"""<!doctype html>
<html lang="en" data-theme="light"><head><meta charset="utf-8"><title>Castle</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 448 512'%3E%3Cpath fill='%23000' d='M32 192L32 48c0-8.8 7.2-16 16-16l64 0c8.8 0 16 7.2 16 16l0 40c0 4.4 3.6 8 8 8l32 0c4.4 0 8-3.6 8-8l0-40c0-8.8 7.2-16 16-16l64 0c8.8 0 16 7.2 16 16l0 40c0 4.4 3.6 8 8 8l32 0c4.4 0 8-3.6 8-8l0-40c0-8.8 7.2-16 16-16l64 0c8.8 0 16 7.2 16 16l0 144c0 10.1-4.7 19.6-12.8 25.6L352 256l16 144L80 400 96 256 44.8 217.6C36.7 211.6 32 202.1 32 192zm176 96l32 0c8.8 0 16-7.2 16-16l0-48c0-17.7-14.3-32-32-32s-32 14.3-32 32l0 48c0 8.8 7.2 16 16 16zM22.6 473.4L64 432l320 0 41.4 41.4c4.2 4.2 6.6 10 6.6 16c0 12.5-10.1 22.6-22.6 22.6L38.6 512C26.1 512 16 501.9 16 489.4c0-6 2.4-11.8 6.6-16z'/%3E%3C/svg%3E">
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
.brand .glyph{display:grid;place-items:center;color:var(--primary);line-height:1}
.brand .glyph svg{width:24px;height:27px;display:block}
.brand em{color:var(--primary);font-style:normal}
.chip{display:inline-flex;align-items:center;gap:5px;padding:2px 10px;border-radius:999px;
 font-size:11px;font-weight:500;background:var(--surface-2);border:1px solid var(--edge);
 color:var(--dim);white-space:nowrap}
.chip b{color:var(--ink);font-weight:600}
.chip.rss{border-color:rgba(88,166,255,.35)} .chip.rss b{color:var(--blue)}
header .spacer{flex:1}
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
.q{position:relative;padding:8px 10px 9px 14px;border-radius:var(--r-md);cursor:pointer;
 margin-bottom:8px;transition:background .12s ease}
.q:hover{background:var(--surface-2)}
.q.sel{background:var(--surface-2);box-shadow:inset 2px 0 0 var(--primary)}
.qtop{display:flex;align-items:center;gap:6px;min-height:16px}
.qtop .qgrow{flex:1}
.qtitle2{font-size:13px;font-weight:600;line-height:1.4;margin-top:4px;color:var(--ink);
 display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;
 overflow-wrap:break-word}
.qsub{color:var(--dim);font-size:11px;margin-top:2px;white-space:nowrap;overflow:hidden;
 text-overflow:ellipsis}
.qnum{font-family:ui-monospace,Menlo,monospace;font-size:14.5px;font-weight:700;
 color:var(--primary);flex:none;letter-spacing:.02em}
.qbadge{flex:none;font-size:8.5px;padding:1px 6px;border-radius:4px;font-weight:700;
 letter-spacing:.1em;text-transform:uppercase;background:rgba(88,166,255,.1);
 border:1px solid rgba(88,166,255,.35);color:var(--blue)}
.qbadge.app-shops{background:rgba(63,185,80,.1);border-color:rgba(63,185,80,.35);color:var(--green)}
.qbadge.app-orders{background:rgba(210,153,34,.12);border-color:rgba(210,153,34,.35);color:var(--amber)}
.qbadge.app-common,.qbadge.app-intelligence{background:rgba(188,140,255,.1);
 border-color:rgba(188,140,255,.35);color:#bc8cff}
.qbadge.app-inventory{background:rgba(248,81,73,.1);border-color:rgba(248,81,73,.3);color:var(--red)}
.qtitle{color:var(--ink);font-size:11.5px;flex:1;min-width:0;overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap}
.qmeta{display:flex;gap:4px;flex-wrap:wrap;margin:4px 2px 1px}
.qslug{color:var(--dim);font-size:11.5px;flex:1;min-width:0;overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap}
.navmore{color:var(--blue);font-size:10.5px;text-align:center;cursor:pointer;font-weight:600}
.badge{font-size:9.5px;padding:1px 7px;border-radius:999px;background:rgba(248,81,73,.12);
 color:var(--red);font-weight:600;letter-spacing:.06em;text-transform:uppercase}
.st{display:inline-flex;align-items:center;padding:1px 8px;border-radius:999px;font-size:9.5px;
 font-weight:600;letter-spacing:.08em;text-transform:uppercase;background:var(--surface-2);color:var(--dim)}

#chat{display:flex;flex-direction:column;overflow:hidden}
#chatbar{display:flex;align-items:center;gap:10px;padding:10px 20px;
 background:var(--surface);border-bottom:1px solid var(--edge);min-height:52px;position:relative}
#chatbar .wt{display:inline-flex;align-items:center;gap:7px;min-width:0;font-family:ui-monospace,Menlo,monospace;font-size:12px;color:var(--ink)}
#chatbar .wt .dim{color:var(--dim)}
#chatbar .qnum{font-size:15px}
#chatbar .qtitle{font-size:12.5px;font-weight:500}
#chatbar .qslug{color:var(--ink);font-size:12.5px;font-family:"Inter","Roboto",-apple-system,"Segoe UI",sans-serif}
.hgrp{margin-left:auto;display:flex;gap:6px;flex:none;align-items:center}
.iconbtn{width:30px;height:30px;border-radius:var(--r-md);display:grid;place-items:center;
 cursor:pointer;background:var(--surface-2);border:1px solid var(--edge);color:var(--dim);
 transition:all .12s ease;font-size:14px;position:relative;flex:none}
.iconbtn:hover{border-color:var(--primary);color:var(--primary)}
.iconbtn.on{border-color:var(--blue);color:var(--blue)}
.iconbtn .bcount{position:absolute;top:-5px;right:-6px;background:var(--blue);color:#0d1117;
 font-size:8.5px;font-weight:700;border-radius:999px;padding:0 4px;line-height:12px}
#sessmenu{display:none;position:absolute;top:calc(100% + 8px);right:14px;width:400px;
 max-height:calc(100vh - 150px);overflow-y:auto;background:var(--surface);
 border:1px solid var(--edge);border-radius:var(--r-lg);box-shadow:var(--sh-2);padding:10px;z-index:40}
#sessmenu.on{display:block}
.smenuhd{font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
 color:var(--faint);margin:0 2px 8px;display:flex;align-items:center;gap:8px}
.smenuhd .dim{font-weight:400;letter-spacing:0;text-transform:none;overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap}
.scard{position:relative;background:var(--surface-2);border:1px solid var(--edge-soft);
 border-radius:var(--r-md);padding:8px 11px;margin-bottom:6px;cursor:pointer;
 transition:border-color .12s ease}
.scard:hover{border-color:var(--blue)}
.scard.on{border-color:rgba(88,166,255,.55);background:rgba(88,166,255,.06)}
.scard .sc1{display:flex;align-items:center;gap:8px}
.scard .sagent{font-weight:700;font-size:10.5px;color:var(--primary);
 text-transform:uppercase;letter-spacing:.08em}
.scard .swhen{margin-left:auto;color:var(--faint);font-size:10px;
 font-variant-numeric:tabular-nums;white-space:nowrap}
.scard .ssnip{color:var(--dim);font-size:11px;margin-top:3px;display:-webkit-box;
 -webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.scard .smeta{color:var(--faint);font-size:9.5px;margin-top:3px;font-variant-numeric:tabular-nums}
.scard .sx{position:absolute;top:4px;right:7px;opacity:0;color:var(--red);font-weight:700;
 cursor:pointer;padding:0 3px;font-size:11px}
.scard:hover .sx{opacity:1}
.scard .sx:hover{color:var(--ink)}
.scard.snew{border-style:dashed;text-align:center;color:var(--blue);font-weight:600;font-size:11.5px}
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
 .readdot{color:var(--green);font-size:11px;font-weight:700;cursor:pointer;line-height:1;padding:0 2px}
 .stalledot{color:var(--amber);font-size:11px;font-weight:700;cursor:pointer;line-height:1;padding:0 2px}
 .idledot{width:7px;height:7px;border-radius:50%;border:1.5px solid var(--dim);display:inline-block;opacity:.7}
 .bread{font-size:9px;padding:1px 7px;border-radius:4px;background:var(--surface-2);
  border:1px solid var(--edge);color:var(--green);cursor:pointer;font-weight:600;flex:none}
 .bread:hover{border-color:rgba(63,185,80,.5)}

#transcript{flex:1;overflow-y:auto;padding:18px 26px;display:flex;flex-direction:column;gap:12px}
.msg{max-width:80%;padding:10px 14px;border-radius:var(--r-lg);background:var(--surface);
 border:1px solid var(--edge-soft);white-space:pre-wrap;word-break:break-word;font-size:13px}
.msg.user{align-self:flex-end;margin-left:auto;background:rgba(255,179,0,.14);
 border:1px solid rgba(255,179,0,.42)}
.msg.user .who{display:block;text-align:right;color:var(--amber)}
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
.msg .mts.foot{display:block;font-size:9px;color:var(--faint);text-align:right;
 margin-top:3px;letter-spacing:.03em;text-transform:none;font-weight:400}
.msg.user .mts.foot,.msg.assistant .mts.foot,.msg.thinking .mts.foot{display:block}
.daydiv{align-self:center;font-size:10px;color:var(--faint);letter-spacing:.08em;
 text-transform:uppercase;font-weight:600;padding:8px 0 2px;user-select:none}
.daydiv::before,.daydiv::after{content:'—';margin:0 8px;opacity:.5}
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
.welcome{margin:auto;text-align:center;max-width:540px;padding:24px}
.wc-glyph{color:var(--primary);margin-bottom:8px;line-height:1}
.wc-glyph svg{width:40px;height:46px;display:inline-block}
.wc-hi{font-size:17px;font-weight:700}
.wc-sub{color:var(--dim);font-size:12.5px;margin:6px 0 18px}
.wc-sub b{color:var(--primary)}
.wc-cards{display:flex;flex-direction:column;gap:8px;text-align:left}
.wcard{background:var(--surface);border:1px solid var(--edge);border-radius:var(--r-md);
 padding:10px 14px;cursor:pointer;transition:all .12s ease}
.wcard:hover{border-color:var(--primary);background:rgba(255,179,0,.05)}
.wl-t{font-weight:600;font-size:12.5px;color:var(--ink)}
.wl-d{color:var(--dim);font-size:11px;margin-top:2px}
.wc-hint{color:var(--faint);font-size:10.5px;margin-top:14px}
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
 padding:8px 10px;margin-bottom:6px;transition:border-color .12s ease;display:flex;
 flex-direction:column}
.bcard.attn{border-color:rgba(248,81,73,.45);background:rgba(248,81,73,.05)}
.bcard.attn .bid{color:var(--red)}
.bops{display:flex;gap:4px;margin-top:6px;flex-wrap:wrap;margin-top:auto;padding-top:6px;
 justify-content:flex-end}
.bops button{padding:2px 8px;font-size:9px}
.bops button.go{border-color:rgba(63,185,80,.4);color:var(--green)}
.bops button.go:hover{border-color:var(--green);color:var(--green);background:rgba(63,185,80,.08)}
.bops button.warn{border-color:rgba(248,81,73,.4);color:var(--red)}
.bchips{display:flex;gap:4px;flex-wrap:wrap;align-items:center}
.bchip{font-size:9px;padding:1px 6px;border-radius:4px;background:var(--surface);
 border:1px solid var(--edge-soft);color:var(--dim)}
.bchip.ok{color:var(--green);border-color:rgba(63,185,80,.3)}
.bchip.bad{color:var(--red);border-color:rgba(248,81,73,.35)}
.bchip.warn{color:var(--amber);border-color:rgba(210,153,34,.35)}
.bchip.ship{color:var(--blue);border-color:rgba(88,166,255,.4);font-weight:700}
.bselall{display:flex;align-items:center;gap:7px;cursor:pointer}
.bselall input{accent-color:var(--primary);cursor:pointer;width:16px;height:16px}
.btop{display:flex;align-items:center;gap:6px;min-width:0;flex-wrap:wrap}
.btop .qgrow{flex:1}
.btitle2{font-size:13px;font-weight:600;line-height:1.45;margin-top:4px;color:var(--ink);
 display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;
 overflow-wrap:break-word}
.bdesc{color:var(--dim);font-size:10.5px;margin-top:3px;white-space:nowrap;overflow:hidden;
 text-overflow:ellipsis;font-family:ui-monospace,Menlo,monospace}
#jobout{font-family:ui-monospace,Menlo,monospace;font-size:11px;white-space:pre-wrap;
 word-break:break-word;background:var(--bg);border:1px solid var(--edge);
 border-radius:var(--r-md);padding:10px 12px;max-height:52vh;overflow-y:auto;margin-top:10px}
#settings{display:none;overflow-y:auto;padding:18px 26px 40px}
.setwrap{max-width:880px;margin:0 auto}
.sethd{display:flex;align-items:center;gap:12px;margin:4px 2px 14px}
.sethd b{color:var(--primary);font-size:13px;letter-spacing:.04em}
.sethd .path{color:var(--faint);font-size:10.5px;font-family:ui-monospace,Menlo,monospace;
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}
.sethd .setsave{background:var(--primary);border:none;color:var(--primary-ink);
 font-weight:700;padding:7px 18px;border-radius:var(--r-md);cursor:pointer;font-size:11.5px;
 letter-spacing:.05em;text-transform:uppercase}
.sethd .setsave:hover{filter:brightness(1.08);border:none;color:var(--primary-ink)}
.setgrp{background:var(--surface);border:1px solid var(--edge);border-radius:var(--r-lg);
 padding:12px 16px 14px;margin-bottom:14px}
.setgrp h3{font-size:10.5px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
 color:var(--primary);margin:2px 2px 8px;display:flex;align-items:center;gap:8px}
.setgrp h3::after{content:"";flex:1;height:1px;background:rgba(255,179,0,.25)}
.setgrp h3 .hdnote{color:var(--faint);font-weight:400;letter-spacing:0;text-transform:none;
 font-size:10px}
.setrow{display:flex;gap:10px;align-items:center;padding:7px 2px;border-bottom:1px solid var(--edge-soft)}
.setrow:last-child{border-bottom:none}
.setrow label{flex:none;width:150px;font-size:11.5px;font-weight:600;color:var(--ink);
 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.setrow label small{display:block;color:var(--faint);font-weight:400;font-size:9.5px}
.setrow input[type=text],.setrow input[type=number]{flex:1;min-width:0;background:var(--bg);
 border:1px solid var(--edge);color:var(--ink);border-radius:var(--r-sm);padding:6px 10px;
 font:11.5px ui-monospace,Menlo,monospace}
.setrow input[type=text]:focus,.setrow input[type=number]:focus{outline:none;border-color:var(--blue)}
.setrow .prov{flex:none;width:120px}
.setrow .mini{flex:none;color:var(--red);cursor:pointer;font-weight:700;padding:2px 6px}
.setrow .mini:hover{color:var(--ink)}
.setnote{color:var(--faint);font-size:10.5px;padding:8px 2px 0}
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
.annrow{border-bottom:1px solid var(--edge-soft);padding:9px 2px}
.annrow:last-child{border-bottom:none}
.annmeta{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;min-width:0}
.annmeta .bchip{flex:none}
.annsel{color:var(--blue);cursor:pointer}
.annsel:hover{text-decoration:underline}
.anntext{color:var(--dim);font-size:11.5px;margin-top:4px;white-space:pre-wrap;
 word-break:break-word;max-height:58px;overflow:hidden}
.annnote{color:var(--ink);font-size:12.5px;font-weight:500;margin-top:4px;
 white-space:pre-wrap;word-break:break-word}
@media (prefers-reduced-motion:reduce){.livedot,.dots i{animation:none}}
.bcard.click{cursor:pointer}
.bcard:hover{border-color:var(--blue)}
.bcard.sel{border-color:rgba(88,166,255,.55);background:rgba(88,166,255,.05)}
.bcard.bulkbusy{opacity:.55}
.bsel{accent-color:var(--blue);cursor:pointer;flex:none;width:18px;height:18px}
.shiphead{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}
.shiphead .bgo{padding:2px 10px}
.bcol-ship .bcard{border-color:rgba(88,166,255,.3)}
.bchip.dim{color:var(--dim);border-color:var(--edge-soft)}
button.busy,.bgo{font-family:inherit}
button.busy{opacity:.65;pointer-events:none}
.spin{display:inline-block;animation:rot .9s linear infinite}
@keyframes rot{to{transform:rotate(360deg)}}
.bbulk{display:flex;align-items:center;gap:8px;margin-left:auto;flex-wrap:wrap}
.bbulk b{color:var(--primary);font-size:11px;letter-spacing:.04em}
.bgo{font-size:10.5px;padding:2px 12px;border-radius:999px;border:1px solid var(--edge);
 background:var(--surface);color:var(--ink);cursor:pointer;transition:all .12s ease}
.bgo:hover{border-color:var(--primary);color:var(--primary)}
.bgo.warn{border-color:rgba(248,81,73,.4);color:var(--red)}
.bgo.warn:hover{border-color:var(--red);color:var(--red)}
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
#drawer{position:fixed;top:52px;right:0;bottom:0;width:min(600px,55vw);background:var(--surface);
 border-left:1px solid var(--edge);box-shadow:var(--sh-2);display:none;flex-direction:column;z-index:25}
#drawer.on{display:flex}
#drawer .dh{display:flex;justify-content:space-between;align-items:center;gap:10px;
 padding:12px 16px;border-bottom:1px solid var(--edge)}
#drawer .dh b{color:var(--primary);font-size:12.5px;letter-spacing:.04em;overflow:hidden;
 text-overflow:ellipsis;white-space:nowrap}
#drawer .dh span{cursor:pointer;color:var(--dim);font-size:10.5px;flex:none;letter-spacing:.06em}
#drawer .dh span:hover{color:var(--ink)}
#drawer .db{flex:1;overflow-y:auto;padding:14px 18px 24px}
.qdoc{max-width:none;background:transparent;border:none;padding:0;white-space:normal;
 font-size:12.5px;flex:1}
.qdoc .who{display:none}
.dother{padding:7px 10px;border:1px solid var(--edge-soft);border-radius:var(--r-md);
 margin-bottom:6px;cursor:pointer;font-size:12px;transition:border-color .12s ease}
.dother:hover{border-color:var(--blue)}
.dother b{color:var(--blue);font-family:ui-monospace,Menlo,monospace;font-size:11px;margin-right:6px}

/* ---- Light mode: cream paper, editorial serif, extra breathing room ---- */
html[data-theme="light"]{
 --bg:#f7f4ec; --surface:#fdfcf8; --surface-2:#efe9da; --edge:#ddd5c2;
 --edge-soft:rgba(80,70,45,.12); --ink:#272219; --dim:#6e6857; --faint:#99927d;
 --primary:#95650e; --primary-ink:#fffcf3; --blue:#31619f; --green:#3d7a44;
 --red:#b5432f; --amber:#8f6410;
 --sh-1:0 1px 2px rgba(74,64,44,.08); --sh-2:0 10px 28px rgba(74,64,44,.14);
}
html[data-theme="light"] body{font:400 13.5px/1.55
 "Iowan Old Style","Palatino Linotype",Palatino,Georgia,Cambria,"Times New Roman",serif}
html[data-theme="light"] ::selection{background:rgba(149,101,14,.16)}
html[data-theme="light"] .chip,html[data-theme="light"] .st,html[data-theme="light"] .badge,
html[data-theme="light"] .qbadge,html[data-theme="light"] .appswitch .app,
html[data-theme="light"] .vtabs .app,html[data-theme="light"] nav h2,
html[data-theme="light"] .smenuhd,html[data-theme="light"] th,html[data-theme="light"] button,
html[data-theme="light"] .codebar,html[data-theme="light"] .bapp,html[data-theme="light"] .bchip,
html[data-theme="light"] .msg .who,html[data-theme="light"] .chipx,
html[data-theme="light"] .bops button,html[data-theme="light"] .bgo,
html[data-theme="light"] #drawer .dh span,html[data-theme="light"] .repohead,
html[data-theme="light"] .wc-hint,html[data-theme="light"] .sessmeta,
html[data-theme="light"] .cmddesc,html[data-theme="light"] .bbulk b,
html[data-theme="light"] .iconbtn{font-family:"Inter","Roboto",-apple-system,"Segoe UI",sans-serif}
html[data-theme="light"] .wc-hi{font-size:19px;font-weight:700;letter-spacing:.01em}
html[data-theme="light"] #chatbar .qslug{font-family:"Iowan Old Style","Palatino Linotype",
 Palatino,Georgia,Cambria,"Times New Roman",serif}
html[data-theme="light"] #composer textarea{font:13.5px/1.55 "Iowan Old Style",
 "Palatino Linotype",Palatino,Georgia,Cambria,"Times New Roman",serif}
html[data-theme="light"] header{height:56px;padding:0 24px}
html[data-theme="light"] main{grid-template-columns:320px 1fr}
html[data-theme="light"] nav{padding:16px 14px 28px}
html[data-theme="light"] nav h2{margin:18px 8px 7px}
html[data-theme="light"] .appswitch{margin-bottom:14px}
html[data-theme="light"] .castle{padding:12px 16px;margin-bottom:14px;
 background:linear-gradient(160deg,#fbf8f0,#f3edda);border-color:rgba(149,101,14,.28)}
html[data-theme="light"] .castle:hover{border-color:rgba(149,101,14,.55)}
html[data-theme="light"] .repohead{margin:16px 6px 7px}
html[data-theme="light"] .repohead::after{background:rgba(149,101,14,.3)}
html[data-theme="light"] .q{padding:9px 12px 10px 16px;margin-bottom:9px}
html[data-theme="light"] .qtitle2{font-size:13.5px}
html[data-theme="light"] .qslug{font-size:12px}
html[data-theme="light"] #chatbar{padding:12px 26px;min-height:56px}
html[data-theme="light"] #transcript{padding:26px 36px;gap:15px}
html[data-theme="light"] .msg{padding:13px 17px;font-size:13.5px;line-height:1.62;
 box-shadow:var(--sh-1)}
html[data-theme="light"] .msg.md{line-height:1.66}
html[data-theme="light"] .msg.user{background:rgba(149,101,14,.13);
 border-color:rgba(149,101,14,.4)}
html[data-theme="light"] .msg.user .who{color:var(--amber)}
html[data-theme="light"] .app.on,html[data-theme="light"] .chipx.on{background:rgba(149,101,14,.1)}
html[data-theme="light"] #composer{padding:14px 26px 18px}
html[data-theme="light"] .wcard{padding:12px 16px}
html[data-theme="light"] .scard{padding:10px 13px}
html[data-theme="light"] .bcard{padding:10px 12px;margin-bottom:8px}
html[data-theme="light"] #boardcols{gap:14px;padding:18px}
html[data-theme="light"] td{padding:8px 10px}
html[data-theme="light"] #modal{background:rgba(46,38,24,.38)}
html[data-theme="light"] #modal .box{box-shadow:var(--sh-2)}
html[data-theme="light"] .qbadge.app-common,html[data-theme="light"] .qbadge.app-intelligence{
 background:rgba(123,83,194,.08);border-color:rgba(123,83,194,.4);color:#7b53c2}
html[data-theme="light"] .iconbtn .bcount{color:#fff}
</style></head><body>
<header><div class="brand"><span class="glyph"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 448 512"><path fill="currentColor" d="M32 192L32 48c0-8.8 7.2-16 16-16l64 0c8.8 0 16 7.2 16 16l0 40c0 4.4 3.6 8 8 8l32 0c4.4 0 8-3.6 8-8l0-40c0-8.8 7.2-16 16-16l64 0c8.8 0 16 7.2 16 16l0 40c0 4.4 3.6 8 8 8l32 0c4.4 0 8-3.6 8-8l0-40c0-8.8 7.2-16 16-16l64 0c8.8 0 16 7.2 16 16l0 144c0 10.1-4.7 19.6-12.8 25.6L352 256l16 144L80 400 96 256 44.8 217.6C36.7 211.6 32 202.1 32 192zm176 96l32 0c8.8 0 16-7.2 16-16l0-48c0-17.7-14.3-32-32-32s-32 14.3-32 32l0 48c0 8.8 7.2 16 16 16zM22.6 473.4L64 432l320 0 41.4 41.4c4.2 4.2 6.6 10 6.6 16c0 12.5-10.1 22.6-22.6 22.6L38.6 512C26.1 512 16 501.9 16 489.4c0-6 2.4-11.8 6.6-16z"/></svg></span><em>CASTLE</em></div>
<div class="vtabs"><div class="app on" id="v_chat" onclick="setView('chat')">chat</div>
<div class="app" id="v_board" onclick="setView('board')">board</div>
<div class="app" id="v_settings" onclick="setView('settings')">settings</div></div>
<div class="vdiv"></div><div id="totals" style="display:flex;gap:8px"></div>
<div class="spacer"></div>
<span class="chip" id="t_today" title="sessions active since local midnight — cost / tokens"></span>
<div class="chip rss">kilo RSS <b id="t_rss">—</b></div>
<div class="iconbtn" id="theme_btn" title="toggle light / dark" onclick="toggleTheme()">☾</div>
<div class="iconbtn" title="recent turns" onclick="openTurns()">≡</div>
<div class="iconbtn" title="search sessions" onclick="openSearch()">⌕</div></header>
<main><nav id="nav"></nav>
<section id="chat">
  <div id="chatbar">
   <span class="wt" id="wt_label">select a worktree</span>
   <span id="wt_badge"></span>
   <span id="easel_chip" class="chip" style="display:none;cursor:pointer" data-easel="1" title=""></span>
   <span id="live_chip" class="chip" style="display:none"><span class="livedot"></span>&nbsp;agent working</span>
   <span id="sess_meta" class="sessmeta"></span>
   <div class="hgrp">
    <div class="iconbtn" title="open quest charter in drawer" onclick="toggleDoc(event)">▤</div>
    <div class="iconbtn" id="browser_btn" title="launch studio chromium" style="display:none" onclick="launchBrowser(event)">▶</div>
    <div class="iconbtn" title="settings — models, presets, suite, MCP" onclick="setView('settings')">⚙</div>
    <div class="iconbtn" id="sess_del" title="delete this session" style="display:none" onclick="delSess(selSess)">✕</div>
    <div class="iconbtn" id="sess_burger" title="sessions in this worktree" onclick="toggleSessMenu(event)">☰</div>
   </div>
   <div id="sessmenu"></div>
  </div>
 <div id="transcript"><div class="welcome"><div class="wc-glyph" id="boot_glyph"></div>
  <div class="wc-hi" id="boot_hi"></div>
  <div class="wc-sub">the court is waking — worktrees and ledger loading…</div></div></div>
 <div id="composer">
  <div id="cmdlist"></div>
  <div class="cont" id="c_cont">new session — pick a worktree, or open the ☰ sessions menu to continue one</div>
  <div class="row">
   <textarea id="c_prompt" placeholder="message the agent… (Enter to send, Shift+Enter for newline)"></textarea>
   <select id="c_model" title="model preset — any preset pairs with any agent class; edit the list in settings"
    style="width:250px;height:46px;background:var(--bg);border:1px solid var(--edge);color:var(--ink);
    border-radius:8px;padding:0 8px;font:11.5px ui-monospace,Menlo,monospace"></select>
   <input id="c_model_custom" list="model_dl" spellcheck="false" autocomplete="off" placeholder="provider/model id"
    title="custom model id — type to filter (kilo/provider/model or provider/model)" style="display:none;
    width:250px;height:46px;background:var(--bg);border:1px solid var(--edge);color:var(--ink);
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
</section>
<section id="settings"><div class="setwrap" id="settings_bd"></div></section></main>
<div id="drawer"><div class="dh"><b id="drawer_title">quest charter</b>
 <span onclick="toggleDoc(event)">CLOSE ✕</span></div>
 <div class="db" id="drawer_bd"></div></div>
<div id="modal"><div class="box"><div class="hd"><b>DETAILS</b>
<span onclick="closeModal()">CLOSE ✕</span></div><div class="bd" id="modal_bd"></div></div></div>
<script>
let S=null, selRepo=null, selWt=null, selSess=null, msgs=[], TURN=null, turns=[];
let LANESEQ=0, curLane=null;
const NAV_EXP={};
const SECTION_ORDER=[
 {id:'artist',label:'Artist studio'},
 {id:'gatehouse',label:'Gatehouse'},
 {id:'treasury',label:'Treasury'},
 {id:'frontier',label:'Frontier'},
 {id:'field',label:'The Field'},
 {id:'other',label:'Other'},
];
function sectionOfBranch(b){
 if(b.startsWith('artist/'))return 'artist';
 if(b.startsWith('the-gatehouse/'))return 'gatehouse';
 if(b.startsWith('scout/'))return 'frontier';
 if(b.startsWith('quest/')||b.startsWith('epic/'))return 'field';
 return 'other';
}
function addUnit(sec,path,b,w){
 if(sec.seen.has(path))return;
 sec.seen.add(path);sec.units.push({b,w});
}
let view='chat', boardApp='all', boardKey='', CMDS=[], cmdIdx=0;
let BOARD_SEL=new Set();
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
function applyTheme(t){
 document.documentElement.dataset.theme=t;
 const b=$('theme_btn');if(b)b.textContent=t==='light'?'☾':'☀';
 try{localStorage.setItem('castle-theme',t)}catch(e){}
}
function toggleTheme(){
 applyTheme(document.documentElement.dataset.theme==='light'?'dark':'light');
}
applyTheme((()=>{try{return localStorage.getItem('castle-theme')||'light'}catch(e){return 'light'}})());
const TRUNKS=['main','castle','master','trunk'];
const KIND_NAMES={quest:'quest',epic:'epic',scout:'scout',artist:'artist','the-gatehouse':'gate'};
function qparse(b){
 if(!b||!b.includes('/'))return null;
 const seg=b.split('/');
 const leaf=seg[seg.length-1];
 const m=leaf.match(/^(?:([A-Za-z]+)[-_]?)?(\d[\w]*)[-_]?(.*)$/);
 if(!m||/^\./.test(m[3]||''))return null;
 const pfx=m[1]||'';
 const rest=[];
 if(pfx.length>2)rest.push(pfx);
 if(seg.length>2)rest.push(seg.slice(1,-1).join('/'));
 if(m[3])rest.push(m[3]);
 return {badge:KIND_NAMES[seg[0]]||seg[0].replace(/^the-/,''),
  big:(pfx.length<=2?pfx.toUpperCase():'')+m[2],rest:rest.join(' · ')};
}
function qnumOf(id){const m=String(id||'').match(/^[A-Za-z]+\d+/);return m?m[0].toUpperCase():'';}
const APP_LABELS={platform:'Platform',shops:'Shops',orders:'Orders',common:'Common',
 inventory:'Inventory',intelligence:'Intel',forecasting:'Forecast',tasks:'Tasks',
 purchases:'Purchases',crm:'CRM',core:'Core'};
function appBadge(app){
 const a=String(app||'').trim().toLowerCase();
 if(!a)return '';
 return APP_LABELS[a]||a.replace(/_/g,' ').replace(/^./,c=>c.toUpperCase());
}
function questFor(repoKey,wt,branch){
 if(!S)return null;
 const qs=(S.quests||[]).filter(q=>!repoKey||q.repo===repoKey);
 return qs.find(q=>wt&&q.worktree===wt)||qs.find(q=>branch&&q.branch===branch)||null;
}
function questTitleHTML(q){
 return `<span class="qnum">${esc(qnumOf(q.id)||q.id)}</span> `+
  (appBadge(q.app)?`<span class="qbadge app-${esc(String(q.app||'').toLowerCase())}">${esc(appBadge(q.app))}</span> `:'')+
  `<span class="qtitle">${esc(q.title||'')}</span>`;
}
function setWtLabel(branch,path){
 const q=questFor(selRepo,path,branch);
 if(q){$('wt_label').innerHTML=questTitleHTML(q);return;}
 const p=qparse(branch);
 $('wt_label').innerHTML=p?
  `<span class="qnum">${esc(p.big)}</span> <span class="qslug">${esc(p.rest||'')}</span>`
  :esc(branch||path&&path.split('/').pop()||'?');
}
const fmtWhen=t=>{if(!t)return'—';const d=new Date(t);
 return d.toLocaleDateString([],{month:'short',day:'numeric'})+' · '+
  d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})};
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
  if(view!=='chat')p.push('view='+view);
  history.replaceState(null,'','#'+p.join('&'));
}
function applyHash(){
 const p=hashState();let hit=false;
 if(p.app&&S.repos.some(r=>r.key===p.app)){selRepo=p.app;hit=true;}
  if(p.view==='board'||p.view==='settings')setView(p.view);
 if(p.wt){
  const r=repo();
  const w=(r.worktrees||[]).find(x=>x.path===p.wt)||(r.worktrees||[]).find(x=>x.branch===p.wt);
   if(w){selWt=w.path;hit=true;
    const wi=(repo().worktrees||[]).find(x=>x.path===w.path);
    setWtLabel(wi&&wi.branch||w.branch||w.path.split('/').pop(),w.path);
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
 try{S=await (await fetch('/api/state')).json();DS_CACHE=null;}catch(e){return;}
 if(!selRepo&&S.repos.length){
  selRepo=(S.repos.find(r=>r.key==='app')||S.repos[0]).key;
  applyHash();renderNav();
  if(!selWt)renderTranscript();
 }else{renderNav();if(selWt)renderSessionsBar();}
  $('totals').innerHTML=`<span class="chip">sessions <b>${S.sessions.length}</b></span>
  <span class="chip">worktrees <b>${S.worktrees.length}</b></span>`+
  ((S.processes||[]).length?`<span class="chip" style="border-color:rgba(248,81,73,.4);cursor:pointer" onclick="openProcs()"><b style="color:var(--red)">${S.processes.length} flagged</b></span>`:'');
  $('t_rss').textContent=mb(S.proc_total_rss);
  const ty=S.today||{};
  $('t_today').innerHTML=`$ <b>${(ty.cost||0).toFixed(2)}</b> today`;
  syncComposer();syncSessMeta();syncEaselChip();syncBrowserBtn();
  if(view==='board'){
    const key=boardApp+'|'+(S.quests||[]).map(q=>q.id+q.status+(q.dirty?'d':'')+(q.app||'')+(q.cogship_id||'')+(q.moc_live?'m':'')+(q.cogship_live?'g':'')).join(',');
   if(key!==boardKey){boardKey=key;renderBoard();}}
}
 // Per-directory turn state, from three observable signals — process
 // liveness (S.active), last emission (session time_updated), and the newest
 // session's message tail (S.tails: assistant `finish` is the deterministic
 // turn-completion marker; null = cut mid-write). Windows are heuristics;
 // any new write instantly returns a card to working.
 //   working ● pulsing — touched inside the freshness window (or a console
 //                       job streaming); a one-shot agent mid-tool-call gets
 //                       the benefit of the doubt between windows.
 //   stalled ! amber   — stopped without signaling completion: a one-shot
 //                       agent alive but silent >10min (these exit when
 //                       done), or a dead session whose tail has finish=null
 //                       (killed mid-write — the goad candidate).
 //   done ✓ green      — the tail signaled an end inside the lookback window;
 //                       cleared only by mark-as-read (per browser), leaving
 //                       idle while the process stays open, silence after.
 //   idle ◌ hollow     — process open, nothing emitting: parked interactive
 //                       session ("studio open"). Interactive roles never
 //                       stall on silence — parking is their normal state.
 const WORKING_FRESH_MS=4*60*1000, STALLED_SILENT_MS=10*60*1000, TAIL_LOOKBACK_MS=30*60*1000;
 const INTERACTIVE_AGENTS=new Set(['artist','steward','code']);
 let DS_CACHE=null;
 function readMap(){try{return JSON.parse(localStorage.getItem('bdread')||'{}')}catch(e){return{}}}
 function markRead(key){const m=readMap();m[key]=1;
  const cutoff=Date.now()-2*3600*1000;
  for(const k of Object.keys(m)){const t=parseInt((k.split('|')[1]||'0'),10);if(!t||t<cutoff)delete m[k];}
  try{localStorage.setItem('bdread',JSON.stringify(m))}catch(e){}
  DS_CACHE=null;renderNav();renderBoard();}
 function dirStates(){if(DS_CACHE)return DS_CACHE;const now=Date.now(),out=new Map();
  const byDir={};for(const a of (S.active||[]))if(a.dir)byDir[a.dir]=a;
  const touch={};for(const x of (S.sessions||[]))if(x.directory){const t=x.time_updated||0;if(t>(touch[x.directory]||0))touch[x.directory]=t;}
  const dirs=new Set([...Object.keys(touch),...Object.keys(byDir)]);
  for(const d of dirs){
   const t=touch[d]||0,a=byDir[d],tail=(S.tails||{})[d];
   const key=d+'|'+t;
   const agent=(tail&&tail.agent)||(a&&a.agent)||'';
   const interactive=INTERACTIVE_AGENTS.has(agent);
   const fresh=t&&now-t<WORKING_FRESH_MS;
   const acked=!!readMap()[key];
   let st=null,why='';
   if(a&&a.source!=='ps'){st='working';why='console job streaming';}
   else if(fresh){st='working';why='emitting';}
   else if(tail&&tail.role==='assistant'&&tail.finish!=null){
    if(!acked&&now-t<TAIL_LOOKBACK_MS){st='done';why='turn completed';}
    else if(a){st='idle';why='session open — idle';}
   }
   else if(a){
    if(interactive){st='idle';why='session open — idle';}
    else if(t&&now-t>=STALLED_SILENT_MS){st='stalled';why='no output for '+Math.round((now-t)/60000)+' min';}
    else{st='working';why='long tool call';}
   }
   else if(tail&&tail.role==='assistant'){st='stalled';why='turn ended without completing';}
   else if(tail&&tail.role==='user'&&a){st='idle';why='prompt queued';}
   if(st)out.set(d,{state:st,why,touch:t,agent,key});
  }
  DS_CACHE=out;return out;}
 function stateFor(path){return dirStates().get(path)||null;}
 function stateGlyph(st){if(!st)return'';
  const t=st.state;
  if(t==='working')return `<span class="livedot" title="${esc(st.why||'agent working')}"></span>`;
  if(t==='done')return `<span class="readdot" data-bread="${esc(st.key)}" title="turn completed — click, or “mark read”, to clear">✓</span>`;
  if(t==='stalled')return `<span class="stalledot" data-bread="${esc(st.key)}" title="stalled — ${esc(st.why)} — click, or “mark read”, to clear">!</span>`;
  return `<span class="idledot" title="${esc(st.why||'idle')}"></span>`;}
 function renderNav(){
   const r=repo(); if(!r){$('nav').innerHTML='';return;}
   let h=`<div class="appswitch">`+S.repos.map(x=>
    `<div class="app ${x.key===selRepo?'on':''}" data-app="${esc(x.key)}">${x.key}</div>`).join('')+`</div>`;
   h+=`<div class="repohead">${esc(r.name)} · ${r.worktrees.length} worktrees</div>`;
   const trunk=r.worktrees.find(w=>w.branch&&(w.branch===TRUNKS.find(t=>t===w.branch)||TRUNKS.includes(w.branch.split('/').pop())));
   if(trunk){const tst=stateFor(trunk.path);
    h+=`<div class="castle ${selWt===trunk.path?'sel':''}" data-wt="${esc(trunk.path)}">
    <div class="name"><span class="dot"></span>${esc(r.name)} trunk${tst?' '+stateGlyph(tst):''}</div>
    <div class="sub">${esc(trunk.branch||'?')}${trunk.dirty?' · dirty':''}</div></div>`;}
  const byBranch={}; for(const w of r.worktrees){if(w.branch)byBranch[w.branch]=w}
  const secs={};for(const s of SECTION_ORDER)secs[s.id]={seen:new Set(),units:[]};
  for(const b of r.branches){
   if(TRUNKS.includes(b))continue;
   const w=byBranch[b];
   if(!w)continue;
   addUnit(secs[sectionOfBranch(b)],w.path,b,w);
  }
  for(const w of r.worktrees){
   if(!w.branch)continue;
   const agents=new Set(sessionsFor(w.path).map(s=>s.agent));
   for(const a of (S.active||[]))if(a.dir===w.path)agents.add(a.agent);
   if(agents.has('master_of_coin'))addUnit(secs.treasury,w.path,w.branch,w);
   if(agents.has('gatekeeper'))addUnit(secs.gatehouse,w.path,w.branch,w);
   if(agents.has('artist'))addUnit(secs.artist,w.path,w.branch,w);
  }
  for(const sec of SECTION_ORDER){
   const s=secs[sec.id];
   if(!s.units.length)continue;
   h+=`<h2>${esc(sec.label)} · ${s.units.length}</h2>`;
   const items=NAV_EXP[sec.id]?s.units:s.units.slice(0,30);
    for(const u of items){
     const b=u.b,w=u.w;
     const nst=stateFor(w.path);
     const q=questFor(r.key,w.path,b);
     const p=qparse(b);
     const marks=(nst?stateGlyph(nst):'')+
     (w.dirty?'<span class="badge">dirty</span>':'');
    let inner;
    if(q){
     const badge=appBadge(q.app)?
      `<span class="qbadge app-${esc(String(q.app||'').toLowerCase())}">${esc(appBadge(q.app))}</span>`:'';
     inner=`<div class="qtop"><span class="qnum">${esc(qnumOf(q.id)||q.id)}</span>${badge}`+
      `<span class="qgrow"></span>${marks}</div>`+
      `<div class="qtitle2">${esc(q.title||'')}</div>`;
      const nsess=sessionsFor(w.path).length;
      const a=q.audit;
      let chips='';
      if(q.cogship_id)chips+=`<span class="bchip ship" title="stamped onto this cog ship convoy">🚢 ${esc(q.cogship_id)}</span>`;
      if(a&&a.tasks_total)chips+=`<span class="bchip ${a.tasks_pct>=100?'ok':''}">${a.tasks_done}/${a.tasks_total} tasks</span>`;
     if(q.tribute_total)chips+=`<span class="bchip ${q.tribute_done>=q.tribute_total?'ok':''}">${q.tribute_done}/${q.tribute_total} tribute</span>`;
     else if(a&&a.tribute_present)chips+='<span class="bchip ok">tribute</span>';
     if(q.wt_status&&q.wt_status!==q.status)chips+=`<span class="bchip warn" title="worktree charter status — ahead of the master charter until sync-back">wt: ${esc(q.wt_status.toLowerCase())}</span>`;
     chips+=`<span class="bchip${nsess?'':' dim'}">${nsess} session${nsess===1?'':'s'}</span>`;
     inner+=`<div class="qmeta">${chips}</div>`;
    }else if(p){
     inner=`<div class="qtop"><span class="qnum">${esc(p.big)}</span><span class="qgrow"></span>${marks}</div>`+
      `<div class="qtitle2">${esc(p.rest||b)}</div>`;
    }else{
     inner=`<div class="qtop"><span class="qgrow"></span>${marks}</div>`+
      `<div class="qtitle2">${esc(b.includes('/')?b.slice(b.indexOf('/')+1):b)}</div>`+
      `<div class="qsub">${esc(b)}</div>`;
    }
    h+=`<div class="q ${selWt===w.path?'sel':''}" title="${esc(b)}" data-wt="${esc(w.path)}" data-branch="${esc(b)}">${inner}</div>`;
   }
   if(s.units.length>30)h+=`<div class="q navmore" data-navmore="${esc(sec.id)}">${NAV_EXP[sec.id]?'▴ show less':'… '+(s.units.length-30)+' more'}</div>`;
  }
  $('nav').innerHTML=h;
}
 $('nav').addEventListener('click',e=>{
  const bread=e.target.closest('[data-bread]');
  if(bread){markRead(bread.dataset.bread);return;}
   const app=e.target.closest('[data-app]');
  if(app){switchApp(app.dataset.app);return;}
  const more=e.target.closest('[data-navmore]');
  if(more){NAV_EXP[more.dataset.navmore]=!NAV_EXP[more.dataset.navmore];renderNav();return;}
  const wt=e.target.closest('[data-wt]');
  if(wt){pickWt(wt.dataset.wt||null,wt.dataset.branch||null);}
});
function switchApp(key){selRepo=key;selWt=null;selSess=null;msgs=[];curLane=null;
 $('wt_label').textContent='select a worktree';$('wt_badge').innerHTML='';
 $('c_cont').innerHTML='new session — pick a worktree, or open the ☰ sessions menu to continue one';
 CMDS=[];renderCmdList();
 renderNav();renderTranscript();syncHash();syncComposer();}
let cmdSeq=0;
const CMD_CACHE={};
async function loadCmds(){
 const dir=selWt;
 if(!dir){CMDS=[];renderCmdList();return;}
 const hit=CMD_CACHE[dir];
 if(hit&&Date.now()-hit.ts<60000){if(CMDS!==hit.list){CMDS=hit.list;renderCmdList();}return;}
 const seq=++cmdSeq;
 let c=[];
 try{c=await (await fetch('/api/commands?dir='+encodeURIComponent(dir))).json();}
 catch(e){}
 if(Array.isArray(c))CMD_CACHE[dir]={ts:Date.now(),list:c};
 if(seq===cmdSeq){CMDS=c;renderCmdList();}
}
function pickWt(path,branch){
 selWt=path;selSess=null;msgs=[];
 if(view==='board')setView('chat');
 if(path){
  const w=(repo().worktrees||[]).find(x=>x.path===path);
  setWtLabel(branch||w&&w.branch||path.split('/').pop(),path);
  $('wt_badge').innerHTML=w&&w.dirty?'<span class="badge">dirty</span>':'';
 }else{
  $('wt_label').innerHTML=esc(branch||'?')+' <span class="dim">· no worktree</span>';
  $('wt_badge').innerHTML='';
 }
  renderNav();renderSessionsBar();renderTranscript();syncComposer();syncSessMeta();syncEaselChip();syncBrowserBtn();
  $('c_cont').innerHTML='new session — pick a worktree, or click a session tab to continue it';
 const sess=sessionsFor(path);
 if(sess.length)openSess(sess[0].id);
 else{
  selSess=null;msgs=[];curLane='L'+(++LANESEQ);renderTranscript();
  $('c_cont').innerHTML='new session — checking for earlier sessions…';
  wtSessions(path).then(list=>{
   if(selWt!==path)return;
   if(list.length)openSess(list[0].id);
   else $('c_cont').innerHTML='new session — no sessions in this worktree yet';
  });
 }
 loadCmds();
 syncHash();
}
function sessionsFor(path){
 // exact-directory match only: a repo-root (trunk) card must show ONLY its own
 // root sessions, never the sessions of nested quest/artist worktrees.
 const base=S.sessions.filter(s=>s.directory&&s.directory===path);
 const extra=(WT_SESS[path]||{}).list||[];
 if(!extra.length)return base;
 const seen=new Set(base.map(s=>s.id));
 return base.concat(extra.filter(s=>!seen.has(s.id)));
}
const WT_SESS={};
async function wtSessions(path){
 if(!path)return[];
 if(WT_SESS[path]&&Date.now()-WT_SESS[path].ts<60000)return WT_SESS[path].list;
 try{
  const list=await (await fetch('/api/sessions?wt='+encodeURIComponent(path))).json();
  WT_SESS[path]={ts:Date.now(),list:list||[]};
  renderSessionsBar();
 }catch(e){return[];}
 return WT_SESS[path].list;
}
function sessMeta(s){
 return `tok ${ktop((s.tokens_input||0)+(s.tokens_output||0))} · $${(s.cost||0).toFixed(2)}`;
}
function renderSessionsBar(){
 const sess=sessionsFor(selWt);
 const b=$('sess_burger');
 if(!b)return;
 b.innerHTML='☰'+(sess.length?`<span class="bcount">${sess.length}</span>`:'');
 if($('sessmenu').classList.contains('on'))fillSessMenu();
}
let sessMenuSeq=0;
async function fillSessMenu(){
 const seq=++sessMenuSeq;
 const sess=sessionsFor(selWt);
 const m=$('sessmenu');
 const head=`<div class="smenuhd">sessions <span class="dim">· ${esc(selWt?selWt.split('/').pop():'no worktree')}</span></div>`;
 m.innerHTML=head+(sess.length?'':
  '<div class="scard" style="cursor:default;color:var(--faint)">no sessions in this worktree yet</div>');
 let starts={};
 if(sess.length)try{
  starts=await (await fetch('/api/session/starts?ids='+sess.map(s=>s.id).join(','))).json();
 }catch(e){}
 if(seq!==sessMenuSeq)return;
 m.innerHTML=head+sess.map(s=>{
  const st=starts[s.id]||{};
  const snip=String(st.start||s.title||s.id);
  return `<div class="scard ${selSess===s.id?'on':''}" data-sid="${esc(s.id)}" title="${esc(s.title||s.id)}">
   <div class="sc1"><span class="sagent">${esc(s.agent||'?')}</span><span class="swhen">${esc(fmtWhen(s.time_updated))}</span></div>
   <div class="ssnip">${esc(snip.length>120?snip.slice(0,120)+'…':snip)}</div>
   <div class="smeta">${esc(sessMeta(s))}${s.model?' · '+esc(s.model.split('/').pop()):''}</div>
   <span class="sx" data-act="del" data-sid="${esc(s.id)}" title="delete session">✕</span></div>`;
 }).join('')+
 `<div class="scard snew ${selSess===null?'on':''}" data-act="new">＋ new session</div>`;
}
function toggleSessMenu(ev){
 if(ev)ev.stopPropagation();
 const m=$('sessmenu'),b=$('sess_burger');
 const on=m.classList.toggle('on');
 b.classList.toggle('on',on);
 if(on)fillSessMenu();
}
function closeSessMenu(){
 const m=$('sessmenu');if(!m)return;
 m.classList.remove('on');$('sess_burger').classList.remove('on');
}
$('sessmenu').addEventListener('click',e=>{
 const del=e.target.closest('[data-act="del"]');
 if(del){delSess(del.dataset.sid);return;}
 if(e.target.closest('[data-act="new"]')){closeSessMenu();newSess();return;}
 const card=e.target.closest('[data-sid]');
 if(card){closeSessMenu();openSess(card.dataset.sid);}
});
document.addEventListener('click',e=>{
 if(!e.target.closest('#sessmenu')&&!e.target.closest('#sess_burger'))closeSessMenu();});
function newSess(){selSess=null;msgs=[];curLane='L'+(++LANESEQ);
 $('c_cont').innerHTML='new session in <b>'+esc(selWt?selWt.replace('/Users/scrummage/Python/',''):'?')+'</b> <span class="dim">— agent replies as a fresh session</span>';
 renderSessionsBar();renderTranscript();syncHash();syncComposer();syncSessMeta();}
let sessOpenSeq=0;
async function openSess(id){
 const seq=++sessOpenSeq;
 selSess=id;msgs=[];curLane=id;
 $('c_cont').innerHTML=`continuing <b>${esc(id.slice(0,24))}…</b> <span class="x" onclick="newSess()">start new instead</span>`;
 renderSessionsBar();renderTranscript();syncHash();syncComposer();syncSessMeta();
 loadCmds();
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
  const who=cls==='user'?'you':cls;
  const showTs=cls==='user'||cls==='assistant'||cls==='thinking';
  const ts=showTs&&m.time_created?msgTS(m.time_created):null;
  return `<div class="msg ${cls}${md?' md':''}${fold}"${k?` data-fk="${esc(k)}"`:''}><div class="who">${esc(who)}</div>${content}${ts?`<div class="mts foot" title="${esc(ts.day)}">${esc(ts.day)} · ${esc(ts.time)}</div>`:''}</div>`;
}
function msgTS(v){
  const d=new Date(typeof v==='number'?v:Number(v));
  if(isNaN(d))return null;
  return {
    day:d.toLocaleDateString([], {weekday:'short', month:'short', day:'numeric'}),
    time:d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
  };
}
function dayKey(v){
  const d=new Date(typeof v==='number'?v:Number(v));
  return isNaN(d)?null:d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0');
}
function transcriptHTML(list){
  let prev=null,out='';
  for(const m of list){
    const k=m.time_created?dayKey(m.time_created):null;
    if(k&&k!==prev){
      const d=new Date(Number(m.time_created));
      if(!isNaN(d))out+=`<div class="daydiv">${esc(d.toLocaleDateString([], {weekday:'long', month:'long', day:'numeric', year:'numeric'}))}</div>`;
      prev=k;
    }
    out+=msgHTML(m);
  }
  return out;
}
function renderTranscript(){
  const t=$('transcript');
  delete t.dataset.turn;
  if(!msgs.length){
   if(!turnForView()){t.innerHTML=welcomeHTML();return;}
   t.innerHTML='<div class="empty-note">working — live output streams here</div>';return;}
  t.innerHTML=transcriptHTML(msgs)||'<div class="empty-note">no text messages in this session yet</div>';
  t.scrollTop=t.scrollHeight;
}
const LAUNCHES=[
 {t:'Plan the next quest',d:'Ledger + board review — the Steward drafts a charter for your approval',
  p:'Review .court/LEDGER.md and run `python3 -m court.cli status`. Identify the highest-value next piece of work, draft a quest charter for it (goal, scope, expected tribute), and present the draft for approval. Do not dispatch any agents yet.'},
 {t:'Survey the realm',d:'Full triage: stalled serfs, tribute ready, gates queued, violations',
  p:'Run `python3 -m court.cli status` and survey the quests across all repos. Report: active quests and their progress, serfs that appear stalled, quests at TRIBUTE_READY or GATE, and any audit violations or pending audiences. Finish with a recommended order of operations.'},
 {t:'Ledger briefing',d:'What shipped recently, open threads, promises unfulfilled',
  p:'Read .court/LEDGER.md and the recent Cog Ship manifests under .court/. Summarize what shipped recently, which threads remain open, and anything promised but unfinished. Keep it to a short briefing.'}
];
const ROOK='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 448 512"><path fill="currentColor" d="M32 192L32 48c0-8.8 7.2-16 16-16l64 0c8.8 0 16 7.2 16 16l0 40c0 4.4 3.6 8 8 8l32 0c4.4 0 8-3.6 8-8l0-40c0-8.8 7.2-16 16-16l64 0c8.8 0 16 7.2 16 16l0 40c0 4.4 3.6 8 8 8l32 0c4.4 0 8-3.6 8-8l0-40c0-8.8 7.2-16 16-16l64 0c8.8 0 16 7.2 16 16l0 144c0 10.1-4.7 19.6-12.8 25.6L352 256l16 144L80 400 96 256 44.8 217.6C36.7 211.6 32 202.1 32 192zm176 96l32 0c8.8 0 16-7.2 16-16l0-48c0-17.7-14.3-32-32-32s-32 14.3-32 32l0 48c0 8.8 7.2 16 16 16zM22.6 473.4L64 432l320 0 41.4 41.4c4.2 4.2 6.6 10 6.6 16c0 12.5-10.1 22.6-22.6 22.6L38.6 512C26.1 512 16 501.9 16 489.4c0-6 2.4-11.8 6.6-16z"/></svg>';
function greet(){
 const h=new Date().getHours();
 return h<5?"Burning the midnight oil, M'lord":h<12?"Good morning, M'lord":
  h<18?"Good afternoon, M'lord":"Good evening, M'lord";
}
function welcomeHTML(){
 if(!S)return '<div class="notice">summoning the court…</div>';
 const qs=S.quests||[];
  const working=qs.filter(q=>q.status==='WORKING').length;
  const act=[...dirStates().values()].filter(s=>s.state==='working').length;
 const g=greet();
 const sub=(working||act)?
  `The court is in motion — <b>${working}</b> quest${working===1?'':'s'} underway, <b>${act}</b> agent${act===1?'':'s'} working. What does M'lord require?`
  :"The court is quiet, M'lord — no quests underway. Shall we raise one?";
 return `<div class="welcome">
  <div class="wc-glyph">${ROOK}</div>
  <div class="wc-hi">${esc(g)}</div>
  <div class="wc-sub">${sub}</div>
  <div class="wc-cards">`+
  LAUNCHES.map((l,i)=>`<div class="wcard" data-launch="${i}">
   <div class="wl-t">${esc(l.t)}</div><div class="wl-d">${esc(l.d)}</div></div>`).join('')+
  `</div>
  <div class="wc-hint">suggestions launch a fresh steward session in your selected worktree — pick a worktree on the left to change the target</div>
 </div>`;
}
function launchSuggestion(i){
 const L=LAUNCHES[i];if(!L||!S)return;
 let wt=selWt||null;
 if(!wt){
  const r=repo();
  if(r)wt=r.root;
 }
 if(!wt){
  const cr=S.repos.find(r=>r.key==='castle');
  if(cr){wt=cr.root;selRepo='castle';}
 }
 if(!wt)return;
 if(selWt!==wt)pickWt(wt);
 newSess();
 $('c_agent').value='steward';syncRoleModel();
 dispatch(wt,null,'steward',composerModel(),L.p);
}
$('transcript').addEventListener('click',e=>{
 const c=e.target.closest('[data-launch]');
 if(c)launchSuggestion(+c.dataset.launch);});
function turnForView(){
  for(let i=turns.length-1;i>=0;i--){
    const T=turns[i];
    if(T.wt!==selWt)continue;
    if(T.sess){if(selSess===T.sess)return T;continue;}
    if(selSess===T.sid)return T;
    if(selSess===null&&T.lane===curLane)return T;
  }
  return null;
}
function blockHTML(b){
  const md=b.cls==='assistant'||b.cls==='thinking';
  const k=b.cls==='thinking'?foldKey(b.text):null;
  const fold=k&&foldMemo[k]===true?' folded':'';
  const showTs=b.cls==='user'||b.cls==='assistant'||b.cls==='thinking';
  const ts=showTs?msgTS(b.ts||(b.ts=Date.now())):null;
  return `<div class="msg ${b.cls}${md?' md':''}${fold}"${k?` data-fk="${esc(k)}"`:''}>`+
   `<div class="who">${b.cls==='thinking'?'reasoning':b.cls==='user'?'you':b.cls}</div>`+
   (md?mdRender(b.text):esc(b.text))+
   (ts?`<div class="mts foot" title="${esc(ts.day)}">${esc(ts.day)} · ${esc(ts.time)}</div>`:'')+'</div>';
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
   t.queue={wt:t.wt,agent:$('c_agent').value,model:composerModel(),prompt:txt};
   ta.value='';autosizeTa();
   t.blocks.push({cls:'notice',
    text:'queued next: '+txt.slice(0,80)+(txt.length>80?'…':'')});
   if(turnForView()===t)renderLive();
   return;}
  if(!selWt){
   const r=repo();
   if(r&&r.root){pickWt(r.root);}
   else{
    $('c_cont').innerHTML='<span style="color:var(--red)">select a worktree with sessions (left) before sending — cannot dispatch without a directory</span>';
    return;}
  }
  ta.value='';autosizeTa();
  dispatch(selWt,selSess,$('c_agent').value,composerModel(),txt);
}
function autosizeTa(){
 const ta=$('c_prompt');
 ta.style.height='auto';
 ta.style.height=Math.min(140,Math.max(46,ta.scrollHeight))+'px';
}
async function dispatch(wt,sess,agent,model,prompt){
  const T={id:++TURNSEQ[0],wt,sess,sid:null,job:null,evs:0,ran:false,done:false,
   lane:sess||curLane,t0:Date.now(),blocks:[{cls:'user',text:prompt}],queue:null,
   histHTML:transcriptHTML(msgs),stopping:false,stopped:false,
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
   document.title=(ok?'✓':'✕')+' turn '+(ok?'done':'failed')+' — Castle';
   if(window.notifyTO)clearTimeout(window.notifyTO);
   window.notifyTO=setTimeout(()=>{document.title='Castle';},8000);
   try{
    if(typeof Notification!=='undefined'&&Notification.permission==='granted')
     new Notification(ok?'Agent turn done':'Agent turn failed',
      {body:(T.wt||'').split('/').pop()+' · '+secs+'s'});
   }catch(e){}
  }
  window.addEventListener('focus',()=>{document.title='Castle';},{once:true});
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
 if(selWt)delete WT_SESS[selWt];
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
 el.style.display='block';
}
function completeCmd(name){
 const ta=$('c_prompt');
 ta.value='/'+name+' ';ta.focus();
 renderCmdList();
}
$('cmdlist').addEventListener('mousedown',e=>{
 const r=e.target.closest('.cmdrow');
 if(r){e.preventDefault();completeCmd(r.dataset.name);}});
function setView(v){
 view=v;syncHash();
 $('chat').style.display=v==='chat'?'':'none';
 $('board').style.display=v==='board'?'flex':'none';
 $('settings').style.display=v==='settings'?'block':'none';
 $('v_chat').classList.toggle('on',v==='chat');
 $('v_board').classList.toggle('on',v==='board');
 $('v_settings').classList.toggle('on',v==='settings');
 renderCmdList();
 renderNav();
 if(v==='board'){boardKey='';renderBoard();}
 if(v==='settings')renderSettings();
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
 TRIBUTE_READY:[["coin","coin","go"],["studio","🎨 studio","go"],["advance","advance","GATE",""]],
 GATE:[["collect","🚢 collect","go"],["studio","🎨 studio","go"]],
 READY_TO_RAZE:[["raze","raze","warn"]],
 PLANNED:[["dispatch","dispatch","go"]]};
function boardAttn(q){
 const a=q.audit;
 return !!(a&&(a.violations>0||a.pending_audience||a.forced_transition));
}
function renderBoardBar(){
 if(view!=='board')return;
 const qs=S.quests||[];
 const apps={};for(const q of qs){const a=q.app||q.repo||'other';apps[a]=(apps[a]||0)+1;}
 const nAttn=qs.filter(boardAttn).length;
 let h='<div class="bfil"><span class="chipx '+(boardApp==='all'?'on':'')+'" data-app="all">all · '+qs.length+'</span>'+
  Object.keys(apps).sort().map(a=>`<span class="chipx ${a===boardApp?'on':''}" data-app="${esc(a)}">${esc(a)} · ${apps[a]}</span>`).join('')+
  (nAttn?`<span class="chipx" style="border-color:rgba(248,81,73,.5);color:var(--red)">${nAttn} need attention</span>`:'')+
  (BOARD_SEL.size?`<div class="bbulk"><b>${BOARD_SEL.size} selected</b>`+
   '<button class="bgo" data-bulk="collect">🚢 pack</button>'+
   '<button class="bgo" data-bulk="studio">🎨 studio</button>'+
   '<button class="bgo" data-bulk="coin">coin</button>'+
   '<button class="bgo" data-bulk="goad">goad</button>'+
   '<button class="bgo" data-bulk="advance">→ tribute</button>'+
   '<button class="bgo warn" data-bulk="raze">raze</button>'+
   '<button class="bgo" data-bulk="clear">clear</button></div>':'')+'</div>';
 $('boardbar').innerHTML=h;
}
function renderBoard(){
 if(view!=='board')return;
 renderBoardBar();
 const qs=S.quests||[];
 const bc=$('boardcols');
  const keep={left:bc?bc.scrollLeft:0,tops:bc?[...bc.querySelectorAll('.bcol')].map(c=>c.scrollTop):[]};
  // Virtual 🚢 cogships column — built here, injected right after the GATE
  // column below. Live convoys: any member integrating at GATE or promoted-
  // but-undeployed (ship_ready). Click a card → ship manifest document;
  // confirm button → court ship --confirm for the convoy's ready members.
  // Fully-deployed/archived convoys drop off.
  const rel=qs.filter(q=>boardApp==='all'||(q.app||q.repo)===boardApp);
  const trunkAhead=rel.length?Math.max(0,...rel.map(q=>q.trunk_ahead||0)):0;
  const byCs={};
  for(const q of rel)if(q.cogship_id)(byCs[q.cogship_id]=byCs[q.cogship_id]||[]).push(q);
  const csKeys=Object.keys(byCs).filter(cs=>byCs[cs].some(q=>
   q.ship_ready||q.status==='GATE')).sort((a,b)=>{
    const na=parseInt(a.replace(/\D/g,''),10),nb=parseInt(b.replace(/\D/g,''),10);
    return nb-na;});
  let csCol=`<div class="bcol bcol-ship"><h3 class="shiphead"><span>🚢 cogships · ${csKeys.length}</span>`+
   (trunkAhead>0?`<span class="bchip ship" title="commits on the castle trunk not yet deployed to main — archived convoys ride in the next promote">trunk ↑${trunkAhead}</span>`:'')+
   '</h3>';
   for(const cs of csKeys){
    const members=byCs[cs];
    const readyIds=members.filter(q=>q.ship_ready).map(q=>q.id);
    const anyGate=members.some(q=>q.status==='GATE');
     const msts=members.map(q=>q.worktree?stateFor(q.worktree):null).filter(Boolean);
     const nStall=msts.filter(s=>s.state==='stalled').length;
     const nWork=msts.filter(s=>s.state==='working').length;
     const hasDone=!nStall&&!nWork&&msts.some(s=>s.state==='done');
     const anyLive=members.some(q=>q.moc_live||q.cogship_live);
     const csTitle=nStall?('stalled — '+nStall+' member quest'+(nStall===1?'':'s')+' silent without completing'):
      (members.some(q=>q.cogship_live)?'integration in progress — '+cs:
       (members.some(q=>q.moc_live)?'master of coin auditing':
        (nWork?'agent working in '+nWork+' member quest'+(nWork===1?'':'s'):
         (hasDone?'member turns completed — clear via mark-read on the quest cards':''))));
     const csGlyph=nStall?`<span class="stalledot" title="${esc(csTitle)}">!</span>`:
      ((nWork||anyLive)?`<span class="livedot" title="${esc(csTitle)}"></span>`:
       (hasDone?`<span class="readdot" title="${esc(csTitle)}">✓</span>`:''));
     const chips=members.map(q=>{
      const qn=(String(q.id).match(/^[A-Za-z]+\d+/)||[q.id])[0].toUpperCase();
      const cls=q.ship_ready?'ok':(q.status==='GATE'?'warn':'');
      return `<span class="bchip ${cls}" title="${esc(q.id)} — ${esc(String(q.status).toLowerCase())}">${esc(qn)}</span>`;}).join('');
     csCol+=`<div class="bcard click" data-cs="${esc(cs)}" title="click to open the ship manifest document">`+
      `<div class="btop">${csGlyph}`+
      `<span class="qnum" style="color:var(--blue)">🚢 ${esc(cs)}</span>`+
    `<span class="qgrow"></span>`+
    (anyGate?'<span class="bchip warn">integrating</span>':'')+
    (readyIds.length?`<button class="bgo" data-csconfirm="${esc(cs)}" data-ids="${esc(readyIds.join(','))}" title="court ship --confirm ${esc(readyIds.join(','))} — promote castle to main and deploy to production">confirm</button>`:'')+
    `</div>`+
    `<div class="bchips">${chips}</div>`+
    '</div>';
  }
  csCol+=csKeys.length?'':'<div class="bempty">—</div>';
  csCol+='</div>';
  let cols='';
  for(const st of STATUS_ORDER){
   if(st==='ASHES')continue;
   const items=qs.filter(q=>q.status===st&&(boardApp==='all'||(q.app||q.repo)===boardApp));
  const allSel=items.length&&items.every(q=>BOARD_SEL.has(q.id));
  cols+=`<div class="bcol"><h3><label class="bselall" title="select all in this column">`+
   `<input type="checkbox" class="bselall" data-bselall="${esc(st)}"${allSel?' checked':''}>`+
   `${esc(st.toLowerCase())} · ${items.length}</label></h3>`;
    for(const q of items){
     const wst=q.worktree?stateFor(q.worktree):null;
     const on=!!wst||q.moc_live||q.cogship_live;
     const liveTitle=wst?wst.why:(q.moc_live?'master of coin auditing':'integration in progress — '+(q.cogship_id||'convoy'));
     const liveGlyph=wst?stateGlyph(wst):(on?`<span class="livedot" title="${esc(liveTitle)}"></span>`:'');
     const readBtn=(wst&&(wst.state==='done'||wst.state==='stalled'))?`<button class="bread" data-bread="${esc(wst.key)}" title="clear the turn marker">mark read</button>`:'';
    const a=q.audit;
    const ops=BOARD_OPS[q.status]||[];
    let chips='';
    if(q.cogship_id)chips+=`<span class="bchip ship" title="stamped onto this cog ship convoy">🚢 ${esc(q.cogship_id)}</span>`;
    if(a){
    if(a.tasks_total)chips+=`<span class="bchip ${a.tasks_pct>=100?'ok':''}">${a.tasks_done}/${a.tasks_total} tasks</span>`;
    if(q.tribute_total)chips+=`<span class="bchip ${q.tribute_done>=q.tribute_total?'ok':''}">${q.tribute_done}/${q.tribute_total} tribute</span>`;
    else chips+=a.tribute_present?'<span class="bchip ok">tribute</span>':'';
    if(q.wt_status&&q.wt_status!==q.status)chips+=`<span class="bchip warn" title="worktree charter status — ahead of the master charter until sync-back">wt: ${esc(q.wt_status.toLowerCase())}</span>`;
    if(a.violations)chips+=`<span class="bchip bad">${a.violations} viol</span>`;
    if(a.pending_audience)chips+='<span class="bchip warn">audience</span>';
    if(a.forced_transition)chips+='<span class="bchip warn">forced</span>';
    if(a.commutation_done)chips+='<span class="bchip ok">commuted</span>';
   }
   const qnum=(String(q.id).match(/^[A-Za-z]+\d+/)||[q.id])[0].toUpperCase();
   const appLabel=appBadge(q.app);
   const desc=[q.section,q.branch].filter(Boolean).join(' · ');
   cols+=`<div class="bcard click ${boardAttn(q)?'attn':''} ${BOARD_SEL.has(q.id)?'sel':''}" data-qid="${esc(q.id)}" ${q.worktree?`data-wt="${esc(q.worktree)}"`:''}>
     <div class="btop"><input type="checkbox" class="bsel" data-bsel="${esc(q.id)}" title="select for bulk action"${BOARD_SEL.has(q.id)?' checked':''}>`+
     liveGlyph+
     `<span class="qnum">${esc(qnum)}</span>`+
     (appLabel?`<span class="qbadge app-${esc(String(q.app||'').toLowerCase())}">${esc(appLabel)}</span>`:'')+
     `<span class="qgrow"></span>`+
     readBtn+
     (q.dirty?'<span class="badge">dirty</span>':'')+`</div>`+
    `<div class="btitle2">${esc(q.title||'')}</div>`+
    (desc?`<div class="bdesc" title="${esc(desc)}">${esc(desc)}</div>`:'')+
    (chips?`<div class="bchips">${chips}</div>`:'')+
    (ops.length?`<div class="bops">${ops.map(o=>
     `<button class="${o[2]==='warn'?'warn':'go'}" data-op="${o[0]}" data-id="${esc(q.id)}"${o[0]==='advance'?` data-status="${o[3]}"`:''}>${esc(o[1])}</button>`).join('')}</div>`:'')+
    '</div>';
  }
  cols+=items.length?'':'<div class="bempty">—</div>';
  cols+='</div>';
  if(st==='GATE')cols+=csCol;
  }
  $('boardcols').innerHTML=cols;
 if(bc){bc.scrollLeft=keep.left;
  [...bc.querySelectorAll('.bcol')].forEach((c,i)=>{c.scrollTop=keep.tops[i]||0;});}
}
$('boardbar').addEventListener('click',e=>{
 const bulk=e.target.closest('[data-bulk]');
 if(bulk){bulkRun(bulk.dataset.bulk);return;}
 const c=e.target.closest('[data-app]');if(c)boardFilter(c.dataset.app);});
 $('boardcols').addEventListener('click',e=>{
  const bread=e.target.closest('[data-bread]');
  if(bread){markRead(bread.dataset.bread);return;}
  const sa=e.target.closest('[data-bselall]');
 if(sa){const st=sa.dataset.bselall;
  const qs=S.quests||[];
  const ids=qs.filter(q=>q.status===st&&(boardApp==='all'||(q.app||q.repo)===boardApp)).map(q=>q.id);
  const all=ids.length&&ids.every(id=>BOARD_SEL.has(id));
  if(all)ids.forEach(id=>BOARD_SEL.delete(id));else ids.forEach(id=>BOARD_SEL.add(id));
  renderBoard();return;}
 const cb=e.target.closest('[data-bsel]');
 if(cb){const id=cb.dataset.bsel;
  if(cb.checked)BOARD_SEL.add(id);else BOARD_SEL.delete(id);
  const card=cb.closest('.bcard');if(card)card.classList.toggle('sel',cb.checked);
  renderBoardBar();return;}
  const op=e.target.closest('[data-op]');
  const csc=e.target.closest('[data-csconfirm]');
  if(csc){shipCogship(csc.dataset.csconfirm,csc.dataset.ids);return;}
  const ccard=e.target.closest('[data-cs]');
  if(ccard){openCogshipDoc(ccard.dataset.cs);return;}
 if(op){courtOp(op.dataset.op,op.dataset.id,op.dataset.status||'',op);return;}
 const card=e.target.closest('.bcard');
 if(card&&card.dataset.qid&&!card.dataset.wt){openDocFor(card.dataset.qid);return;}
 const c=e.target.closest('[data-wt]');if(c)openQuestWt(c.dataset.wt);});
function courtOp(op,id,status,btn){
 if(op==='raze'&&!confirm('RAZE (verify merge + queue for teardown)\n\n'+id+' — proceed?'))return;
 runCourtOp(op,id,status,btn,btn&&btn.closest('.bcard')).then(r=>{
  if(r&&r.error)alert(op.toUpperCase()+' '+id+' — '+r.error);});
}
async function runCourtOp(op,id,status,btn,card,ids,confirmFlag){
 let d;
 try{d=await (await fetch('/api/court',{method:'POST',body:JSON.stringify(Object.assign(ids?{op,id:id||'',status,ids}:{op,id:id||'',status}, confirmFlag?{confirm:true}:{}))})).json();}
 catch(e){return {error:'failed: '+e};}
 if(d.error)return {error:d.error};
 const job=d.job;
 const sibs=card?[...card.querySelectorAll('button')]:[];
 const orig=btn?btn.textContent:'';
 if(btn){btn.classList.add('busy');btn.innerHTML='<span class="spin">⟳</span>';btn.disabled=true;}
 if(card){card.classList.toggle('bulkbusy',!btn);
  sibs.forEach(b=>{b.disabled=true;});}
 let shown=0,res={ok:true};
 while(true){
  let st;
  try{st=await (await fetch('/api/send/'+job+'?since='+shown)).json();}
  catch(e){await new Promise(r=>setTimeout(r,900));continue;}
  shown=(st.events||[]).length;
  if(st.done){
   if(st.exit===0)res={ok:true};
   else{const errs=(st.events||[]).filter(e=>e.type==='error');
    res={ok:false,error:(errs.length?errs[errs.length-1].text:'court op failed (exit '+st.exit+')')};}
   break;}
  await new Promise(r=>setTimeout(r,800));
 }
 if(btn&&btn.isConnected){btn.classList.remove('busy');btn.textContent=orig;btn.disabled=false;}
 sibs.forEach(b=>{if(b.isConnected)b.disabled=false;});
 if(card&&card.isConnected)card.classList.remove('bulkbusy');
 poll();
 return res;
}
async function bulkRun(op){
 const ids=[...BOARD_SEL];
 if(!ids.length)return;
 if(op==='clear'){BOARD_SEL=new Set();renderBoard();return;}
  if(op==='collect'){
   const atGate=ids.filter(id=>{const q=(S.quests||[]).find(x=>x.id===id);return q&&q.status==='GATE';}).length;
   const gateNote=atGate===ids.length
    ?'All selected are already at GATE — they are re-stamped onto the new convoy, no status change.'
    :'Quests not yet at GATE are advanced to GATE as part of the pack.';
   if(!confirm('PACK × '+ids.length+' quest'+(ids.length>1?'s':'')+' onto ONE cog ship convoy?\n\ncollect '+ids.join(',')+
    '\n\nStamps the selection as one convoy and hands it to the Gatekeeper. '+gateNote+' Quests that fail the pack audit are skipped with reasons.'))
    return;
  const bar0=$('boardbar').querySelector('.bbulk');
  if(bar0)bar0.innerHTML='<b>collecting '+ids.length+' quest'+(ids.length===1?'':'s')+' onto one convoy…</b>';
  const r=await runCourtOp('collect',null,'',null,null,ids);
  BOARD_SEL=new Set();
  renderBoard();
  if(r&&!r.ok)alert('collect failed — '+r.error);
  return;}
 if(op==='studio'){
  if(!confirm('STUDIO × '+ids.length+' quest'+(ids.length>1?'s':'')+' — one combined artist studio?\n\ncourt studio '+ids.join(',')+
   '\n\nCuts an artist-studio worktree from the trunk tip, merges the selection with the established conflict policy, boots the freshness-gated runserver, and stands up the Court Artist.'))
   return;
  const barS=$('boardbar').querySelector('.bbulk');
  if(barS)barS.innerHTML='<b>standing up combined studio for '+ids.length+' quest'+(ids.length===1?'':'s')+'…</b>';
  const r=await runCourtOp('studio',null,'',null,null,ids);
  BOARD_SEL=new Set();
  renderBoard();
  if(r&&!r.ok)alert('studio failed — '+r.error);
  return;}
 if(!confirm(op.toUpperCase()+' × '+ids.length+' quest'+(ids.length>1?'s':'')+' — run sequentially now?'))return;
 const bar=$('boardbar').querySelector('.bbulk');
 const setProg=txt=>{if(bar)bar.innerHTML='<b>'+esc(txt)+'</b>';};
 const results=[];
 for(let i=0;i<ids.length;i++){
  const qid=ids[i];
  setProg(op+' '+qid+' ('+(i+1)+'/'+ids.length+')…');
  const card=document.querySelector('#boardcols .bcard[data-qid="'+qid+'"]');
  const r=await runCourtOp(op,qid,op==='advance'?'TRIBUTE_READY':'',null,card);
  results.push(Object.assign({id:qid},r));
 }
 BOARD_SEL=new Set();
 renderBoard();
 const bad=results.filter(r=>!r.ok);
 if(bad.length)alert(op+' finished — '+results.filter(r=>r.ok).length+' ok, '+bad.length+' failed\n\n'+
  bad.map(r=>r.id+': '+r.error).join('\n'));
}
 async function shipCogship(cs,ids){
  if(!ids)return;
  if(!confirm('🚢 SHIP '+cs.toUpperCase()+' — PRODUCTION DEPLOY\n\ncourt ship --confirm '+ids+
   '\n\nPromotes castle → main and releases the fleet. This is the production deploy step — the convoy leaves the board once merged into main.'))
   return;
  const r=await runCourtOp('ship',null,'',null,null,ids.split(','),true);
  renderBoard();
  if(r&&!r.ok)alert('ship failed — '+r.error);
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
function toggleDoc(ev){
 if(ev)ev.stopPropagation();
 const d=$('drawer');
 if(d.classList.contains('on')){d.classList.remove('on');return;}
 openDocFor('');
}
function openDocFor(qid){$('drawer').classList.add('on');loadQuestDoc(qid);}
function openCogshipDoc(cs){$('drawer').classList.add('on');loadCogshipDoc(cs);}
async function loadCogshipDoc(cs){
 const bd=$('drawer_bd');
 bd.innerHTML='<div class="dim">packing manifest…</div>';
 $('drawer_title').textContent='🚢 '+cs+' — ship manifest';
 try{
  const j=await (await fetch('/api/cogship?cogship='+encodeURIComponent(cs))).json();
  if(j.error){bd.innerHTML='<div class="dim">'+esc(j.error)+'</div>';return;}
  $('drawer_title').textContent=j.title;
  const ids=(j.ids||[]).length?'<div class="bchips" style="margin-bottom:10px">'+
   j.ids.map(id=>`<span class="bchip"><b>${esc(id.split('-')[0]+id.match(/\d+/)[0])}</b> ${esc(id)}</span>`).join('')+'</div>':'';
  bd.innerHTML=ids+'<div class="msg md qdoc">'+mdRender(String(j.text||''))+'</div>';
  bd.scrollTop=0;
 }catch(e){bd.innerHTML='<div class="dim">failed to load: '+esc(e)+'</div>';}
}
async function loadQuestDoc(qid){
 const bd=$('drawer_bd');
 bd.innerHTML='<div class="dim">loading…</div>';
 const r=repo();
 try{
  const j=await (await fetch('/api/quest?app='+encodeURIComponent(r?r.key:'')+
   '&id='+encodeURIComponent(qid||'')+'&wt='+encodeURIComponent(selWt||''))).json();
  if(j.error){
   $('drawer_title').textContent='quest charter';
   bd.innerHTML='<div class="dim" style="margin-bottom:8px">'+esc(j.error)+' — pick a charter:</div>'+
    (j.others||[]).map(q=>`<div class="dother" data-qid="${esc(q.id)}"><b>${esc(q.id)}</b>${esc(q.title||'')}`+
     `<span class="dim" style="margin-left:8px;font-size:10px">${esc(String(q.status||'').toLowerCase())}</span></div>`).join('');
   return;}
  $('drawer_title').textContent=(j.quest.id?j.quest.id.toUpperCase()+' · ':'')+
   (j.quest.title||'quest charter')+' — '+String(j.quest.status||'').toLowerCase();
  const md=String(j.md||'').replace(/^---\n[\s\S]*?\n---\s*\n?/,'');
  const fm=j.fm||{};
  const chips=[['status',j.quest.status],['app',j.quest.app],['section',fm.section],
   ['concern',fm.concern],['branch',fm.branch],['convoy',fm.cogship_id],
   ['epic',fm.parent_epic],['worktree',fm.worktree]].filter(x=>x[1]).
   map(x=>`<span class="bchip"><b>${esc(x[0])}</b> ${esc(String(x[1]))}</span>`).join('');
  const det=chips?'<div class="bchips" style="margin-bottom:10px">'+chips+'</div>':'';
  const thin=!md||!md.trim();
  bd.innerHTML=det+'<div class="msg md qdoc">'+
   (thin?'<div class="dim">No charter content yet — this quest is '+
    esc(String(j.quest.status||'').toLowerCase())+
    '. Its Goal & Scope and Expected Tribute are written when the quest is chartered.</div>'
    :mdRender(md))+'</div>';
  bd.scrollTop=0;
 }catch(e){bd.innerHTML='<div class="dim">failed to load: '+esc(e)+'</div>';}
}
$('drawer_bd').addEventListener('click',e=>{
 const o=e.target.closest('[data-qid]');
 if(o)loadQuestDoc(o.dataset.qid);});
function openMcp(){setView('settings');}
const _SET_ED={};
function setRow(label,inner,note){
 return `<div class="setrow"><label title="${esc(label)}">${esc(label)}${note?`<small>${esc(note)}</small>`:''}</label>${inner}</div>`;}
function renderSettings(){
 if(view!=='settings')return;
 if(!SETTINGS){$('settings_bd').innerHTML='<div class="empty-note">loading settings…</div>';
  loadSettings().then(renderSettings);return;}
 const ed=_SET_ED;
 if(!ed.ready){
  ed.roles=SETTINGS.roles.slice();
  ed.models=Object.assign({},SETTINGS.models);
  ed.providers=Object.assign({},SETTINGS.providers);
  ed.aliases=Object.assign({},SETTINGS.aliases);
  ed.presets=SETTINGS.presets.slice();
  ed.suite=SETTINGS.suite;ed.harness=SETTINGS.harness;
  ed.freshness=Object.assign({},SETTINGS.freshness);
  ed.no_kilo_mode=SETTINGS.no_kilo_mode;
  ed.ready=true;}
 let h=`<div class="sethd"><b>SETTINGS — court manifest</b>`+
  `<span class="path">${esc(SETTINGS.manifest_path||'')}</span>`+
  `<span class="sessmeta" id="set_state"></span>`+
  `<button class="setsave" onclick="saveSettings()">save</button></div>`;
 // role models
 h+=`<div class="setgrp"><h3>models per class<span class="hdnote">applies to the composer defaults, court dispatch, and every CLI that resolves a role model</span></h3>`+
  ed.roles.map(r=>setRow(r,
   `<input type="text" id="set_m_${esc(r)}" value="${esc(ed.models[r]||'')}" list="model_dl" spellcheck="false">`+
   `<input type="text" id="set_p_${esc(r)}" class="prov" value="${esc(ed.providers[r]||'')}" placeholder="provider" spellcheck="false" title="optional Kilo CLI provider (models.${esc(r)}_provider)">`,
   'model · provider')).join('')+
  `<div class="setnote">any model preset may be paired with any class inside a session from the composer; bare names resolve through the aliases below.</div></div>`;
 // presets
 h+=`<div class="setgrp"><h3>model presets<span class="hdnote">the basic pick list in the composer model select</span></h3>`+
  ed.presets.map((p,i)=>setRow('preset '+(i+1),
   `<input type="text" value="${esc(p)}" data-preset="${i}" spellcheck="false" onchange="edPreset(${i},this.value)">`+
   `<span class="mini" title="remove preset" onclick="edPreset(${i},'')">✕</span>`)).join('')+
  setRow('add preset','<input type="text" id="set_preset_add" list="model_dl" placeholder="provider/model or alias" spellcheck="false">'+
   '<button class="bgo" onclick="edPresetAdd()">add</button>')+
  '</div>';
 // aliases
 h+=`<div class="setgrp"><h3>model aliases<span class="hdnote">display name → qualified id</span></h3>`+
  Object.entries(ed.aliases).map(([n,v],i)=>setRow(n,
   `<input type="text" value="${esc(v)}" data-alias="${esc(n)}" spellcheck="false" onchange="edAlias('${esc(n)}',this.value)">`+
   `<span class="mini" title="remove alias" onclick="edAlias('${esc(n)}','')">✕</span>`)).join('')+
  setRow('add alias','<input type="text" id="set_alias_name" placeholder="display name">'+
   '<input type="text" id="set_alias_val" placeholder="provider/model">'+
   '<button class="bgo" onclick="edAliasAdd()">add</button>')+
  '</div>';
 // pipeline
 h+=`<div class="setgrp"><h3>pipeline commands</h3>`+
  setRow('integration suite','<input type="text" id="set_suite" value="'+esc(ed.suite)+'" spellcheck="false">','suite.command')+
  setRow('verification harness','<input type="text" id="set_harness" value="'+esc(ed.harness)+'" spellcheck="false">','harness.command')+
  setRow('studio freshness gate','<input type="text" id="set_fresh" value="'+esc(ed.freshness.command||'')+'" spellcheck="false">','studio.freshness_command')+
  setRow('freshness max age (h)','<input type="number" id="set_fresh_age" value="'+esc(ed.freshness.max_age_hours!=null?ed.freshness.max_age_hours:24)+'" min="0.5" step="0.5">')+
  setRow('no-kilo mode','<input type="checkbox" id="set_nokilo"'+(ed.no_kilo_mode?' checked':'')+'>')+
  '</div>';
 // MCP
 const list=S&&S.mcp?S.mcp:[];
 h+=`<div class="setgrp"><h3>mcp servers<span class="hdnote">merged inventory — toggles edit the owning config file (a .bak copy is kept)</span></h3>`;
 if(!list.length)h+='<div class="setnote">no MCP servers inventoried yet</div>';
 else{
  h+='<table><thead><tr><th>name</th><th>scope</th><th>type</th><th>enabled</th><th>running</th><th></th></tr></thead><tbody>'+
   list.map(m=>{
    const run=m.running?`<span class="num" style="color:var(--green)">yes</span> <span class="num blue">${mb(m.rss)}</span>`
     :'<span class="dim">no</span>';
    const btn=m.file.endsWith('.jsonc')?'<span class="dim">manual</span>'
     :`<button title="takes effect for sessions started after the change" data-file="${esc(m.file)}" data-name="${esc(m.name)}">${m.enabled?'disable':'enable'}</button>`;
    return `<tr><td class="mono">${esc(m.name)}</td><td class="dim">${esc(m.scope)}</td><td class="dim">${esc(m.type)}</td>`+
     `<td>${m.enabled?'<span class="num" style="color:var(--green)">yes</span>':'<span class="dim">no</span>'}</td><td>${run}</td><td>${btn}</td></tr>`;
   }).join('')+'</tbody></table>';
 }
 h+='</div>';
 $('settings_bd').innerHTML=h;
}
function edPreset(i,val){
 val=String(val||'').trim();
 if(val)_SET_ED.presets[i]=val;else _SET_ED.presets.splice(i,1);
 renderSettings();}
function edPresetAdd(){
 const v=($('set_preset_add').value||'').trim();if(!v)return;
 _SET_ED.presets.push(v);renderSettings();}
function edAlias(name,val){
 val=String(val||'').trim();
 if(val)_SET_ED.aliases[name]=val;else delete _SET_ED.aliases[name];
 renderSettings();}
function edAliasAdd(){
 const n=($('set_alias_name').value||'').trim(),v=($('set_alias_val').value||'').trim();
 if(!n||!v)return;
 _SET_ED.aliases[n]=v;renderSettings();}
async function loadSettings(){
 try{SETTINGS=await (await fetch('/api/settings')).json();
  if(_SET_ED.ready){_SET_ED.ready=false;}
 }catch(e){}}
$('settings_bd').addEventListener('click',e=>{
 const b=e.target.closest('[data-file]');
 if(b)mcpToggle(b.dataset.file,b.dataset.name);});
$('settings_bd').addEventListener('keydown',e=>{
 if(e.key!=='Enter')return;
 const t=e.target;
 if(t.id==='set_preset_add')edPresetAdd();
 else if(t.id==='set_alias_name'||t.id==='set_alias_val')edAliasAdd();
 else if(t.id==='set_suite'||t.id==='set_harness'||t.id==='set_fresh'||t.id==='set_fresh_age')saveSettings();});
async function saveSettings(){
 const ed=_SET_ED;
 if(!ed.ready){renderSettings();return;}
 for(const r of ed.roles){
  ed.models[r]=($('set_m_'+r).value||'').trim();
  ed.providers[r]=($('set_p_'+r).value||'').trim();}
 ed.suite=($('set_suite').value||'').trim();
 ed.harness=($('set_harness').value||'').trim();
 ed.freshness={command:($('set_fresh').value||'').trim(),
  max_age_hours:parseFloat($('set_fresh_age').value)||24};
 ed.no_kilo_mode=$('set_nokilo').checked;
 const body={models:{},aliases:ed.aliases,presets:ed.presets,
  suite:ed.suite,harness:ed.harness,freshness:ed.freshness,no_kilo_mode:ed.no_kilo_mode};
 for(const r of ed.roles)body.models[r]={model:ed.models[r],provider:ed.providers[r]};
 const st=$('set_state');if(st)st.textContent='saving…';
 let out=null;
 try{
  const r=await fetch('/api/settings',{method:'POST',body:JSON.stringify(body)});
  out=await r.json();
  if(!r.ok){if(st)st.textContent='refused: '+(out.error||r.status);return;}
 }catch(e){if(st)st.textContent='save failed: '+e;return;}
 _SET_ED.ready=false;
 await loadSettings();
 _SET_ED.ready=false;
 renderSettings();
 if($('set_state'))$('set_state').textContent='saved ✓';
 try{META=await (await fetch('/api/compose-meta')).json();
  $('model_dl').innerHTML=(META.models||[]).map(m=>`<option value="${esc(m)}"></option>`).join('');
  $('c_model').innerHTML=(META.presets||[]).map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join('')+
   '<option value="__custom__">custom…</option>';
  syncRoleModel();
 }catch(e){}
 setTimeout(()=>{const s=$('set_state');if(s)s.textContent='';},2500);
}
$('modal_bd').addEventListener('click',e=>{
  const c=e.target.closest('[data-copy]');
  if(c){navigator.clipboard.writeText(c.dataset.copy).then(()=>{
    const t=c.textContent;c.textContent='copied!';
    setTimeout(()=>{c.textContent=t;},1200);});return;}
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
  if(e.key==='Escape'){closeModal();closeSessMenu();$('drawer').classList.remove('on');}});
function isArtistWt(){
 if(!selWt||!S)return false;
 const w=(repo().worktrees||[]).find(x=>x.path===selWt);
 return !!(w&&w.branch&&w.branch.startsWith('artist/'));
}
function syncBrowserBtn(){
 const b=$('browser_btn');if(!b)return;
 const st=(S||{}).browser||{};
 b.style.display=isArtistWt()?'':'none';
 const running=!!st.running;
 b.classList.toggle('on',running);
 const port=st.recorded?st.recorded.port:null;
 b.title=running?
  'studio chromium is up'+(port?' (CDP :'+port+')':'')+' — click to pin the annotation overlay to this worktree'
  :'launch shared studio chromium (headed, persistent login profile, CDP + annotation overlay for this worktree)';
}
async function launchBrowser(ev){
 if(ev)ev.stopPropagation();
 if(!selWt)return;
 $('c_cont').innerHTML='starting studio chromium + runserver…';
 let j;
 try{
  const r=await fetch('/api/browser',{method:'POST',body:JSON.stringify({wt:selWt})});
  j=await r.json();
  if(!r.ok){alert('browser launch refused: '+(j.error||r.status));poll();return;}
 }catch(e){alert('browser launch failed: '+e);poll();return;}
 poll();
 const sv=j.server||{};
 $('c_cont').innerHTML='studio chromium up — app <b>'+esc(sv.url||'?')+'</b> '+
  (sv.up?(sv.spawned?'<span class="dim">(runserver just started)</span>':'<span class="dim">(runserver already up)</span>')
       :'<span style="color:var(--red)">runserver did not come up — see .kilo/runserver.log</span>')+
  ' <span class="dim">'+(j.annotator_alive?'— annotation overlay pinned to this worktree':'— annotator not attached')+'</span>';
 if(!sv.up&&sv.log_tail)alert('runserver log tail:\n'+sv.log_tail);
}
function syncEaselChip(){
 const el=$('easel_chip');if(!el)return;
 const w=selWt&&S?(S.worktrees||[]).find(x=>x.path===selWt):null;
 if(w&&w.easel&&w.easel.port){
  el.style.display='';
  el.innerHTML='<span style="color:var(--blue)">⌖</span> easel <b>:'+w.easel.port+
   '</b> · <b>'+(w.annotations||0)+'</b> notes';
  el.title='studio easel'+(w.easel.cdp_url?' — '+w.easel.cdp_url:'')+
   ' — click to review pinned annotations';
 }else{el.style.display='none';el.title='';}
}
$('chatbar').addEventListener('click',e=>{
 if(e.target.closest('[data-easel]'))openEaselNotes();});
let annSeq=0;
async function openEaselNotes(){
 const wt=selWt;const seq=++annSeq;
 openModal('PINNED ANNOTATIONS — studio easel','<div id="annlist" class="dim">loading…</div>');
 let j=null;
 try{j=await (await fetch('/api/annotations?wt='+encodeURIComponent(wt||''))).json();}
 catch(e){}
 if(seq!==annSeq)return;
 const el=$('annlist');if(!el)return;
 const items=(j&&j.items)||[];
 if(!items.length){el.className='';
  el.innerHTML='<div class="empty-note">No annotations pinned in this worktree yet.</div>';
  return;}
 el.className='';
 el.innerHTML=items.map(a=>{
  const sel=String(a.selector||'');
  const route=String(a.route||'');
  return `<div class="annrow">
   <div class="annmeta"><span class="tmono">${esc(a.ts||'')}</span>
    <span class="bchip">${esc(a.tag||'note')}</span>
    ${route?`<span class="tmono" title="${esc(route)}">${esc(route.length>42?'…'+route.slice(-42):route)}</span>`:''}
    ${sel?`<span class="annsel tmono" data-copy="${esc(sel)}" title="click to copy selector">${esc(sel.length>60?'…'+sel.slice(-60):sel)}</span>`:''}
   </div>`+
   (a.text?`<div class="anntext">${esc(a.text)}</div>`:'')+
   `<div class="annnote">${esc(a.note||'')}</div></div>`;
 }).join('');
}
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
   if(w){selWt=w.path;setWtLabel(w.branch||w.path.split('/').pop(),w.path);}
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
  if(!m){if(view==='settings')renderSettings();return;}
  let r;
  try{r=await fetch('/api/mcp',{method:'POST',body:JSON.stringify({file,name,enabled:!m.enabled})});}
  catch(e){alert('toggle failed: '+e);return;}
  if(!r.ok){alert('refused: '+(await r.text()));return;}
  await poll();
  if(view==='settings')renderSettings();
 }
let META=null, SETTINGS=null;
function canonModelJS(v){
 const m=String(v||'').trim();if(!m)return'';
 if(m.includes('/'))return m;
 const low=m.toLowerCase().replace(/ /g,'').replace(/-/g,'').replace(/\./g,'');
 for(const[name,q]of Object.entries((META&&META.aliases)||{})){
  const n=String(name).toLowerCase().replace(/ /g,'').replace(/-/g,'').replace(/\./g,'');
  if(n&&n===low&&String(q||'').trim())return String(q).trim();}
 return'openrouter/'+m;}
function composerModel(){
 const sel=$('c_model');
 if(sel.value==='__custom__')return($('c_model_custom').value||'').trim();
 return sel.value;}
function setComposerModel(qid){
 qid=canonModelJS(qid);if(!qid)return;
 const sel=$('c_model');
 const hit=[...sel.options].find(o=>o.value===qid);
 if(hit){sel.value=qid;$('c_model_custom').style.display='none';$('c_model_custom').value='';}
 else{sel.value='__custom__';$('c_model_custom').style.display='';$('c_model_custom').value=qid;}}
function syncRoleModel(){
 if(!META)return;
 const roleModel=(META.role_models||{})[$('c_agent').value]||'';
 if(roleModel)setComposerModel(roleModel);}
$('c_agent').addEventListener('change',syncRoleModel);
$('c_model').addEventListener('change',()=>{
 const custom=$('c_model').value==='__custom__';
 $('c_model_custom').style.display=custom?'':'none';
 if(custom)$('c_model_custom').focus();});
(async()=>{try{META=await (await fetch('/api/compose-meta')).json();
 $('c_agent').innerHTML=META.agents.map(x=>`<option>${x}</option>`).join('');
 $('model_dl').innerHTML=(META.models||[]).map(m=>`<option value="${esc(m)}"></option>`).join('');
 const sel=$('c_model');
 sel.innerHTML=(META.presets||[]).map(m=>`<option value="${esc(m)}">${esc(m)}</option>`).join('')+
  '<option value="__custom__">custom…</option>';
 syncRoleModel();
}catch(e){}})();
const bootHi=$('boot_hi');if(bootHi)bootHi.textContent=greet();
const bootGl=$('boot_glyph');if(bootGl)bootGl.innerHTML=ROOK;
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


def _frontmatter_status(text):
    """status: field from a charter's frontmatter block only ('' if absent)."""
    head = text.split("\n---", 1)[0]
    m = re.search(r"^status:\s*(\S+)", head, re.M)
    return m.group(1) if m else ""


def _tribute_counts(text):
    """[done, total] of the charter's 'Expected Tribute' markdown checklist.

    Mirrors models.Quest.from_markdown shape detection: sections may use
    '## Expected Tribute' (current charters) or '# Expected Tribute' (legacy).
    Checklist semantics mirror ward.parse_markdown_checklist: [x] done,
    [ ] pending, [~]/[-] cancelled — all three count toward total."""
    body = text
    fm = re.match(r"\A---\n.*?\n---\n", text, re.S)
    if fm:
        body = text[fm.end():]
    level = 2 if re.search(r"^##\s+Expected Tribute\s*$", body, re.M) else 1
    pat = re.compile(r"^#{%d}\s+(.+?)\s*$" % level, re.M)
    start = end = None
    for m in pat.finditer(body):
        if start is not None:
            end = m.start()
            break
        if m.group(1).strip() == "Expected Tribute":
            start = m.end()
    if start is None:
        return 0, 0
    sec = body[start:end] if end is not None else body[start:]
    done = len(re.findall(r"^\s*[-*]\s+\[[xX]\]", sec, re.M))
    total = len(re.findall(r"^\s*[-*]\s+\[[ xX]\]", sec, re.M)) + \
        len(re.findall(r"^\s*[-*]\s+\[[~-]\]", sec, re.M))
    return done, total


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
            charter_text = ""
            try:
                with open(path) as f:
                    charter_text = f.read()
                lines = charter_text.splitlines()
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
            wt_status = ""
            if wt and path:
                wt_copy = os.path.join(
                    wt, ".court", "quests", os.path.basename(path))
                if os.path.isfile(wt_copy):
                    try:
                        with open(wt_copy) as f:
                            wt_status = _frontmatter_status(f.read())
                    except OSError:
                        wt_status = ""
            t_done, t_total = _tribute_counts(charter_text)
            quests.append({
                "id": qid, "title": fm.get("title", ""),
                "status": fm.get("status", "OPEN"), "app": app,
                "branch": fm.get("branch", ""), "worktree": wt,
                "epic": fm.get("parent_epic", ""),
                "section": fm.get("section", ""), "concern": fm.get("concern", ""),
                "kind": fm.get("kind", ""),
                "dirty": _wt_dirty(wt), "path": path,
                "wt_status": wt_status,
                "cogship_id": fm.get("cogship_id", ""),
                "cogship_promoted_commit": fm.get("cogship_promoted_commit", ""),
                "tribute_done": t_done, "tribute_total": t_total,
                "ship_ready": False,
            })
    quests.sort(key=lambda q: (
        STATUS_ORDER.index(q["status"]) if q["status"] in STATUS_ORDER else 99,
        q["id"]))
    with ThreadPoolExecutor(max_workers=8) as ex:
        for q, d in zip(quests, ex.map(_wt_dirty, [q["worktree"]
                                                   for q in quests])):
            q["dirty"] = d
    # Ship-ready mirror of the CLI's "Cogships Ready" predicate: promoted to
    # castle but NOT yet merged into main (deploy pending). Computed, not a
    # status — the board renders it as its own virtual column.
    for q in quests:
        if q["status"] not in ("READY_TO_RAZE", "READY_FOR_TEARDOWN", "DONE"):
            continue
        if q["kind"] == "scout" or q["section"] == "Investigation":
            continue
        probe = SimpleNamespace(**{k: q.get(k, "") for k in (
            "id", "branch", "kind", "section", "cogship_promoted_commit")})
        if not probe.cogship_promoted_commit:
            probe.cogship_promoted_commit = None
        try:
            q["ship_ready"] = not git_ops.is_quest_merged_into(
                probe, target_ref="main", cwd=Path(root))
        except Exception:
            q["ship_ready"] = False
    # Trunk deploy backlog: commits on castle not yet merged into main.
    # One git call per repo; archived-but-undeployed quests (razed, shipped
    # into the castle, awaiting the next castle→main promote) ride here.
    try:
        ra = subprocess.run(["git", "rev-list", "--count", "main..castle"],
                            cwd=root, capture_output=True, text=True, timeout=15)
        trunk_ahead = int(ra.stdout.strip()) if ra.returncode == 0 else 0
    except Exception:
        trunk_ahead = 0
    for q in quests:
        q["trunk_ahead"] = trunk_ahead
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


def _quest_doc(root, wt, qid):
    """Charter markdown for the doc drawer: match by quest id when given,
    else by the selected worktree path; the repo's quest list always rides
    along so the client can fall back to a picker."""
    quests = _quests(root)

    def norm(p):
        return os.path.normpath(p or "")

    q = None
    if qid:
        ql = qid.strip().lower()
        q = next((x for x in quests if x["id"].lower() == ql), None)
        if q is None:
            cands = [x for x in quests if x["id"].lower().startswith(ql)]
            if len(cands) == 1:
                q = cands[0]
    if q is None and wt:
        q = next((x for x in quests if norm(x["worktree"]) == norm(wt)), None)
    others = [{"id": x["id"], "title": x["title"], "status": x["status"]}
              for x in quests]
    if q is None:
        return {"error": "no quest matches this worktree or id",
                "others": others}
    try:
        with open(q["path"]) as f:
            md = f.read()
    except OSError as exc:
        return {"error": f"charter unreadable: {exc}", "others": others}
    # Split frontmatter from body so the drawer can show charter details
    # even for quests too young to have charter content (OPEN/PLANNED).
    fm = {}
    body = md
    lines = md.splitlines()
    if lines and lines[0].strip() == "---":
        for i, ln in enumerate(lines[1:], 1):
            if ln.strip() == "---":
                body = "\n".join(lines[i + 1:])
                break
            if ":" in ln:
                k, v = ln.split(":", 1)
                fm[k.strip()] = v.strip()
    return {"quest": {"id": q["id"], "title": q["title"],
                      "status": q["status"], "app": q["app"],
                      "branch": q["branch"]},
            "fm": {k: fm.get(k, "") for k in (
                "kind", "section", "concern", "parent_epic", "tags",
                "cogship_id", "worktree", "created_at", "updated_at")},
            "md": body, "others": others}


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


def _session_rows(rows):
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
    return _session_rows(rows)


_TAILS_CACHE = {"ts": 0.0, "data": {}}


def _turn_tails(lookback_ms=30 * 60 * 1000, ttl=60.0):
    """Turn-tail state per recently-touched directory: the newest session's
    last message role + finish. An assistant message's `finish` field is the
    deterministic turn-completion signal — present (stop/tool-calls/…) means
    the turn signaled an end; null means cut mid-write (working, or killed).
    Cached 60s: tails move only when a turn ends, never mid-emission."""
    now = time.time()
    if _TAILS_CACHE["data"] and now - _TAILS_CACHE["ts"] < ttl:
        return _TAILS_CACHE["data"]
    if not os.path.exists(KILO_DB):
        return {}
    cutoff = (now * 1000) - lookback_ms
    out = {}
    try:
        db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=3)
        db.execute("pragma query_only=1")
        rows = db.execute(
            "select directory, id, agent, time_updated from session"
            " where time_updated >= ? order by time_updated desc limit 400",
            (cutoff,)).fetchall()
        seen = set()
        for directory, sid, agent, tu in rows:
            if not directory or directory in seen:
                continue
            seen.add(directory)
            tail = {}
            m = db.execute(
                "select data from message where session_id=?"
                " order by time_created desc limit 1", (sid,)).fetchone()
            if m:
                try:
                    d = json.loads(m[0])
                    tail = {"role": d.get("role"), "finish": d.get("finish")}
                except Exception:
                    pass
            out[directory] = {"agent": agent or "", "touch": tu, **tail}
        db.close()
    except Exception:
        return _TAILS_CACHE["data"]
    _TAILS_CACHE["ts"] = now
    _TAILS_CACHE["data"] = out
    return out


def _sessions_for_wt(wt, limit=200):
    """All sessions rooted at one worktree path. The global 200-most-recent
    cap in _sessions() hides older quest sessions from the console sidebar."""
    if not wt or not os.path.exists(KILO_DB):
        return []
    try:
        db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=3)
        db.execute("pragma query_only=1")
        rows = db.execute(
            "select id, title, agent, model, directory, time_updated,"
            " tokens_input, tokens_output, tokens_cache_read,"
            " tokens_cache_write, cost from session"
            " where directory=?"
            " order by time_updated desc limit ?",
            (wt, limit)).fetchall()
        db.close()
    except Exception:
        return []
    return _session_rows(rows)


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


def _browser_runtime():
    """Lazy import keeps court.browser (and its subprocess constants) out of
    the module graph until the studio browser feature is actually used."""
    from court import browser as studio_browser
    return studio_browser


def _probe_http(url, timeout=2.0):
    """Any HTTP answer (2xx/3xx/4xx/5xx) counts as up; connection errors don't."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False


def _tail_file(path, limit=800):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - limit))
            return f.read().decode(errors="replace")
    except Exception:
        return ""


def _ensure_runserver(wt):
    """Guarantee the worktree dev server is reachable: adopt the port recorded
    in .worktree-port (else 8000), probe it, and spawn
    `<venv>/bin/python manage.py runserver 127.0.0.1:PORT --noreload` detached
    when it is down. Returns a status dict; never raises."""
    port = None
    port_file = os.path.join(wt, ".worktree-port")
    if os.path.isfile(port_file):
        try:
            with open(port_file) as f:
                port = int(f.read().strip())
        except Exception:
            port = None
    if not port:
        port = 8000
    url = f"http://127.0.0.1:{port}/"
    if _probe_http(url, timeout=2.0):
        return {"port": port, "url": url, "up": True, "spawned": False, "log_tail": ""}
    repo = next((r["root"] for r in _repos()
                 if wt.startswith(r["root"] + os.sep)), None)
    py = None
    for cand in (os.path.join(repo, "venv", "bin", "python") if repo else "",
                 os.path.join(repo, ".venv", "bin", "python") if repo else "",
                 shutil.which("python3") or ""):
        if cand and os.path.exists(cand):
            py = cand
            break
    log_path = os.path.join(wt, ".kilo", "runserver.log")
    log_tail = ""
    if repo and os.path.exists(os.path.join(wt, "manage.py")):
        try:
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "ab") as log:
                log.write(
                    f"\n--- console runserver spawn {time.strftime('%F %T')} "
                    f"port {port} ---\n".encode())
                proc = subprocess.Popen(
                    [py, "manage.py", "runserver", f"127.0.0.1:{port}",
                     "--noreload"],
                    cwd=wt, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True)
            deadline = time.time() + 25
            while time.time() < deadline:
                if _probe_http(url, timeout=1.5):
                    return {"port": port, "url": url, "up": True,
                            "spawned": True, "log_tail": ""}
                if proc.poll() is not None:
                    break
                time.sleep(0.4)
        except Exception:
            pass
        log_tail = _tail_file(log_path)
    return {"port": port, "url": url, "up": False, "spawned": False,
            "log_tail": log_tail}


def _cdp_open_tab(cdp_port, url):
    """Open a new tab in the shared Chromium via the DevTools HTTP API.
    Newer Chrome builds ignore ?url=…: the working form is PUT with the raw
    target URL as the query string; the encoded ?url=… form stays as fallback."""
    import urllib.parse
    base = f"http://127.0.0.1:{int(cdp_port)}/json/new?"
    for query in (url, "url=" + urllib.parse.quote(url, safe="")):
        try:
            req = urllib.request.Request(base + query, method="PUT")
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode() or "{}")
        except Exception:
            continue
    return None


def _cdp_close_blanks(cdp_port, keep_id=None):
    """Close leftover about:blank page targets (from earlier launches)."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{int(cdp_port)}/json", timeout=3) as r:
            targets = json.loads(r.read().decode() or "[]")
    except Exception:
        return 0
    closed = 0
    for t in targets:
        if t.get("type") != "page" or t.get("url") not in ("", "about:blank"):
            continue
        if keep_id and t.get("id") == keep_id:
            continue
        for method in ("GET", "PUT"):
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{int(cdp_port)}/json/close/{t.get('id')}",
                    method=method)
                with urllib.request.urlopen(req, timeout=3) as r:
                    r.read()
                closed += 1
                break
            except Exception:
                continue
    return closed


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


def _session_starts(sids, limit=200):
    """First user text snippet per session id, for the session-menu cards.
    Bounded: max 12 ids, oldest 6 messages scanned per id."""
    sids = [s for s in (sids or [])
            if isinstance(s, str) and re.fullmatch(r"[\w-]+", s)][:12]
    if not sids or not os.path.exists(KILO_DB):
        return {}
    out = {}
    try:
        db = sqlite3.connect(f"file:{KILO_DB}?mode=ro", uri=True, timeout=3)
        db.execute("pragma query_only=1")
        for sid in sids:
            rows = db.execute(
                "select id, json_extract(data,'$.role') from message"
                " where session_id=? order by time_created asc limit 6",
                (sid,)).fetchall()
            roles = {r[0]: r[1] for r in rows}
            mids = [r[0] for r in rows]
            texts = {}
            if mids:
                q = ",".join("?" * len(mids))
                for mid, pdata in db.execute(
                        f"select message_id, data from part"
                        f" where message_id in ({q})", mids):
                    try:
                        p = json.loads(pdata)
                    except Exception:
                        continue
                    if p.get("type") == "text" and p.get("text"):
                        texts.setdefault(mid, p["text"])
            for mid in mids:
                if roles.get(mid) == "user" and mid in texts:
                    out[sid] = {"start": texts[mid][:limit]}
                    break
        db.close()
    except Exception:
        return out
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


_ALLOWED_AGENTS = ("steward", "code", "serf", "scout", "artist",
                   "gatekeeper", "master_of_coin")

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
    "studio": ("studio", "{id}"),
    "raze": ("raze", "{id}"),
    "dispatch": ("dispatch", "{id}", "--standup"),
    # ship: --confirm is appended ONLY when the request carries confirm:true
    # (dialog-acknowledged board confirm) — never baked into the template.
    "ship": ("ship", "{id}"),
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


def _court_op(job, op, qid, status, note, ids=None, confirm=False):
    try:
        found = _find_quest_repo(qid)
        if not found and op in ("collect", "ship") and not qid:
            # Bare collect/ship (no selection): explicit pack/launch intent.
            found = {"root": COURT_DIR}
        if not found and op == "studio" and not ids and not qid:
            job["events"].append({"type": "error",
                                  "text": "studio needs an explicit quest selection"})
            job["done"] = True
            job["ended"] = time.time()
            return
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
        elif ids and op in ("collect", "studio", "ship"):
            # Batched call: one command for the whole selection
            # (collect id1,id2,... packs ONE convoy; studio id1,id2,...
            # routes ONE combined artist studio; ship id1,id2,... launches
            # exactly that deployment manifest). Refuse cross-repo
            # selections — a court root serves one convoy/studio.
            roots = set()
            resolved = []
            for bid in ids:
                f = _find_quest_repo(bid)
                if not f or not f.get("quest"):
                    job["events"].append({"type": "error",
                                          "text": f"unknown or ambiguous quest id {bid!r}"})
                    job["done"] = True
                    job["ended"] = time.time()
                    return
                roots.add(f["root"])
                resolved.append(f["quest"]["id"])
            if len(roots) > 1:
                job["events"].append({"type": "error",
                                      "text": "selection spans multiple repos — run one " + op + " per repo"})
                job["done"] = True
                job["ended"] = time.time()
                return
            root = roots.pop()
            argv = ["python3", "-m", "court.cli", op, ",".join(resolved)]
            if op == "ship" and confirm:
                argv.append("--confirm")
        else:
            tmpl = _COURT_OP_ARITY[op]
            if op in ("collect", "studio", "ship") and found.get("quest"):
                # Selection-scoped: operate only on this quest.
                argv = ["python3", "-m", "court.cli", op,
                        found["quest"]["id"]]
                if op == "ship" and confirm:
                    argv.append("--confirm")
            else:
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
        # goad/coin/dispatch/studio wrap a full agent turn — no timeout; stop via /api/stop.
        # ship promotes castle → main (merge + manifest rollup) — allow 10 min.
        long_op = op in ("goad", "coin", "dispatch", "studio")
        try:
            rc = proc.wait(None if long_op else (600 if op == "ship" else 180))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            rc = -99
            job["events"].append({"type": "error", "text": "court op timed out"})
        if op == "raze" and rc == 0:
            # `raze` verifies and queues only — it exits 0 even when it
            # refuses (unmerged/dirty) or merely queues for teardown, and
            # the card leaves the board only once the charter is archived.
            # Chain archive when this quest actually razed (🔥 success
            # line); refusal/dirty/pillory-hold runs stay on the board.
            q = found.get("quest") or {}
            razed = any(f"🔥 Razed {q.get('id', '')}" in (e.get("text") or "")
                        for e in job["events"] if e.get("type") == "text")
            if razed and q.get("path") and os.path.isfile(q["path"]):
                argv2 = ["python3", "-m", "court.cli", "archive", q["id"]]
                job["events"].append({"type": "status",
                                      "text": "$ " + " ".join(argv2) + f"   (cwd {root})"})
                proc2 = subprocess.Popen(
                    argv2, cwd=root, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, start_new_session=True)
                rc2 = None
                try:
                    for ln in proc2.stdout:
                        ln = ln.rstrip()
                        if ln:
                            job["events"].append({
                                "type": "text",
                                "text": ln[:400] + (" …" if len(ln) > 400 else "")})
                    rc2 = proc2.wait(120)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc2.pid), signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        pass
                    rc2 = -99
                    job["events"].append({"type": "error",
                                          "text": "archive step timed out"})
                if rc2:
                    rc = rc2
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


def _manifest_path():
    return os.path.join(COURT_DIR, ".court", "config.json")


def _manifest():
    """Fresh read of the court manifest (.court/config.json); {} when absent."""
    try:
        with open(_manifest_path()) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _canon_model(v, cfg=None):
    """Bare/display model name -> qualified provider/model id (manifest
    aliases first, then the default provider prefix)."""
    m = str(v or "").strip()
    if not m:
        return ""
    if "/" in m:
        return m
    cfg = cfg if cfg is not None else _manifest()
    low = m.lower().replace(" ", "").replace("-", "").replace(".", "")
    for name, q in (cfg.get("model_aliases") or {}).items():
        norm = str(name).lower().replace(" ", "").replace("-", "").replace(".", "")
        if norm and norm == low and str(q).strip():
            return str(q).strip()
    return "openrouter/" + m


# Basic preset list: manifest model_presets win; fallback seeds the manifest
# aliases plus every configured role model so any class has basics to pick.
_DEFAULT_PRESETS = [
    "openrouter/z-ai/glm-5.3-flash",
    "openrouter/z-ai/glm-5.3",
    "openrouter/google/gemini-3.7-flash",
    "openrouter/google/gemma-4-31b-it",
    "openrouter/anthropic/claude-sonnet-4.6",
    "openrouter/openai/gpt-5.2",
]


def _presets():
    cfg = _manifest()
    out = []
    for v in (cfg.get("model_presets") or []):
        q = _canon_model(v, cfg)
        if q and q not in out:
            out.append(q)
    if not out:
        for q in (cfg.get("model_aliases") or {}).values():
            q = _canon_model(q, cfg)
            if q and q not in out:
                out.append(q)
    for role in KNOWN_ROLE_MODELS:
        q = _canon_model((cfg.get("models") or {}).get(role), cfg)
        if q and q not in out:
            out.append(q)
    for q in _DEFAULT_PRESETS:
        if q not in out:
            out.append(q)
    return out[:40]


def _aliases():
    a = _manifest().get("model_aliases")
    return dict(a) if isinstance(a, dict) else {}


def _role_models():
    models = _manifest().get("models")
    if not isinstance(models, dict):
        return {}
    return {r: str(models.get(r) or "") for r in KNOWN_ROLE_MODELS
            if str(models.get(r) or "").strip()}


_SETTINGS_LOCK = threading.Lock()

_ROLE_MODEL_RE = re.compile(r"^[\w.~@/-]{1,120}$")
_ALIAS_NAME_RE = re.compile(r"^[\w .-]{1,60}$")


def _settings_write(payload):
    """Merge validated settings into .court/config.json (atomic, .bak kept).
    Returns (http_code, payload_dict)."""
    if not isinstance(payload, dict):
        return 400, {"error": "bad request"}
    with _SETTINGS_LOCK:
        cfg = _manifest()
        models = dict(cfg.get("models")) if isinstance(cfg.get("models"), dict) else {}

        role_updates = payload.get("models")
        if role_updates is not None:
            if not isinstance(role_updates, dict):
                return 400, {"error": "models must be an object"}
            for role, upd in role_updates.items():
                if role not in KNOWN_ROLE_MODELS or not isinstance(upd, dict):
                    return 400, {"error": f"unknown role {role}"}
                model = str(upd.get("model") or "").strip()
                if not model or not _ROLE_MODEL_RE.match(model):
                    return 400, {"error": f"bad model id for {role}"}
                models[role] = model
                provider = str(upd.get("provider") or "").strip()
                if provider:
                    if not re.fullmatch(r"[\w-]{1,40}", provider):
                        return 400, {"error": f"bad provider for {role}"}
                    models[f"{role}_provider"] = provider
                    models.pop(f"{role}_provider_disabled", None)
                else:
                    models.pop(f"{role}_provider", None)
            cfg["models"] = models

        aliases = payload.get("aliases")
        if aliases is not None:
            if not isinstance(aliases, dict):
                return 400, {"error": "aliases must be an object"}
            clean = {}
            for name, val in aliases.items():
                name = str(name).strip()
                val = _canon_model(str(val).strip(), cfg)
                if not _ALIAS_NAME_RE.match(name) or not val:
                    return 400, {"error": f"bad alias {name!r}"}
                clean[name] = val
                if len(clean) > 60:
                    return 400, {"error": "too many aliases"}
            cfg["model_aliases"] = clean

        presets = payload.get("presets")
        if presets is not None:
            if not isinstance(presets, list):
                return 400, {"error": "presets must be a list"}
            clean = []
            for v in presets:
                q = _canon_model(str(v).strip(), cfg)
                if not q or not _ROLE_MODEL_RE.match(q):
                    return 400, {"error": f"bad preset {v!r}"}
                if q not in clean:
                    clean.append(q)
                if len(clean) > 60:
                    return 400, {"error": "too many presets"}
            cfg["model_presets"] = clean

        def _cmd(section, key, value):
            if value is None:
                return True
            if not isinstance(value, str):
                return False
            value = "".join(c for c in value.strip() if c >= " " or c == "\t")
            if len(value) > 500:
                return False
            blk = cfg.get(section)
            blk = dict(blk) if isinstance(blk, dict) else {}
            if value:
                blk[key] = value
            else:
                blk.pop(key, None)
            if blk:
                cfg[section] = blk
            else:
                cfg.pop(section, None)
            return True

        if not _cmd("suite", "command", payload.get("suite")):
            return 400, {"error": "bad suite command"}
        if not _cmd("harness", "command", payload.get("harness")):
            return 400, {"error": "bad harness command"}
        fresh = payload.get("freshness")
        if fresh is not None:
            if not isinstance(fresh, dict):
                return 400, {"error": "bad freshness"}
            if not _cmd("studio", "freshness_command", fresh.get("command")):
                return 400, {"error": "bad freshness command"}
            if fresh.get("max_age_hours") is not None:
                try:
                    hours = float(fresh["max_age_hours"])
                    if not 0 < hours <= 24 * 30:
                        return 400, {"error": "bad freshness max age"}
                    blk = dict(cfg.get("studio")) if isinstance(cfg.get("studio"), dict) else {}
                    blk["freshness_max_age_hours"] = hours
                    cfg["studio"] = blk
                except (TypeError, ValueError):
                    return 400, {"error": "bad freshness max age"}

        if "no_kilo_mode" in payload:
            cfg["no_kilo_mode"] = bool(payload["no_kilo_mode"])

        path = _manifest_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            if os.path.exists(path):
                with open(path) as f:
                    raw = f.read()
                with open(path + ".bak", "w") as f:
                    f.write(raw)
                indent = 2
            else:
                indent = 2
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                f.write(json.dumps(cfg, indent=indent) + "\n")
            os.replace(tmp, path)
        except Exception as exc:
            return 500, {"error": f"manifest write failed: {exc}"}
    return 200, {"ok": True, "settings": _settings_snapshot()}


def _settings_snapshot():
    cfg = _manifest()
    studio = cfg.get("studio") if isinstance(cfg.get("studio"), dict) else {}
    suite = cfg.get("suite") if isinstance(cfg.get("suite"), dict) else {}
    harness = cfg.get("harness") if isinstance(cfg.get("harness"), dict) else {}
    return {
        "roles": list(KNOWN_ROLE_MODELS),
        "models": {r: str((cfg.get("models") or {}).get(r) or "")
                   for r in KNOWN_ROLE_MODELS},
        "providers": {r: str((cfg.get("models") or {}).get(f"{r}_provider") or "")
                      for r in KNOWN_ROLE_MODELS},
        "aliases": _aliases(),
        "presets": _presets(),
        "suite": str(suite.get("command") or ""),
        "harness": str(harness.get("command") or ""),
        "freshness": {
            "command": str(studio.get("freshness_command") or ""),
            "max_age_hours": studio.get("freshness_max_age_hours", 24),
        },
        "no_kilo_mode": bool(cfg.get("no_kilo_mode")),
        "manifest_path": _manifest_path(),
    }


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
        # settings-driven default: the agent class's manifest model wins, the
        # canonical console default is the last resort
        role = agent if agent in KNOWN_ROLE_MODELS else ""
        manifest_model = _canon_model((_manifest().get("models") or {}).get(role, ""))
        cmd += ["--model", manifest_model or "openrouter/z-ai/glm-5.3-flash"]
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
    """Map command name -> md path; precedence: worktree .kilo, then the
    owning repo root's .kilo (VS Code Kilo reads the project register even
    inside a worktree), then the global user config."""
    seen = {}
    roots = []
    if directory and os.path.isdir(directory):
        roots.append(os.path.join(directory, ".kilo"))
    norm_dir = os.path.normpath(directory) if directory else ""
    for r in _repos():
        rd = os.path.normpath(r["root"])
        if norm_dir and norm_dir != rd and norm_dir.startswith(rd + os.sep):
            roots.append(os.path.join(rd, ".kilo"))
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


_ANNOTATION_BODY_CAP = 16_384
_ANNOTATION_FILE_CAP = 2_000_000
_ANNOTATION_FIELD_CAPS = {"url": 2000, "route": 500, "selector": 300,
                          "tag": 40, "text": 300, "ts": 40}


def _annotation_wt_ok(path):
    """An annotation target must be a real git checkout: an absolute, existing
    directory that contains .git itself, or nests under a known repo root
    (same roots the _worktrees/_quests scans already know)."""
    if not isinstance(path, str) or not path:
        return False
    p = os.path.normpath(path)
    if not os.path.isabs(p) or not os.path.isdir(p):
        return False
    if os.path.exists(os.path.join(p, ".git")):
        return True
    return any(p == os.path.normpath(r["root"])
               or p.startswith(os.path.normpath(r["root"]) + os.sep)
               for r in _repos())


def _annotation_file(worktree):
    return os.path.join(os.path.normpath(worktree), ".kilo",
                        "studio-annotations.jsonl")


def _annotation_rows(worktree):
    """Bounded studio-annotations.jsonl read: (items newest-first, capped at
    the last 200) plus the total line count. Tolerates a truncated final
    line; a mid-file seek cap drops the partial line it landed in."""
    raw = b""
    try:
        with open(_annotation_file(worktree), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - _ANNOTATION_FILE_CAP))
            raw = f.read(_ANNOTATION_FILE_CAP)
            if size > _ANNOTATION_FILE_CAP:
                nl = raw.find(b"\n")
                raw = raw[nl + 1:] if nl >= 0 else b""
    except OSError:
        return [], 0
    lines = [ln for ln in raw.decode("utf-8", "replace").split("\n")
             if ln.strip()]
    items = []
    for ln in reversed(lines[-200:]):
        try:
            items.append(json.loads(ln))
        except Exception:
            continue
    return items, len(lines)


def _annotation_append(rec):
    """Append one JSON line (JSONL: append-only, no read-modify-write);
    creates the .kilo dir when needed. Returns the running total."""
    path = _annotation_file(rec["worktree"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(rec) + "\n")
    _, total = _annotation_rows(rec["worktree"])
    return total


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200, cors=False):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        # CORS preflight for the studio-browser annotator (served on another
        # localhost port, posting to /api/annotation)
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            # The whole app (JS included) rides in this one document; always
            # revalidate so a restarted server is picked up on plain reload.
            self.send_header("Cache-Control", "no-cache")
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
                        "models": _models(),
                        "presets": _presets(),
                        "aliases": _aliases(),
                        "role_models": _role_models()})
        elif self.path == "/api/settings":
            self._json(_settings_snapshot())
        elif self.path == "/api/state":
            procs = _ps_procs()
            flagged, total = _processes(procs)
            repos = [
                {"key": r["key"], "name": r["name"], "root": r["root"],
                 "branches": _branches(r["root"]),
                 "worktrees": _worktrees(r["root"])}
                for r in _repos()]
            # easel/annotations reflect the files right now — worktree dicts
            # are TTL-cached and shared, so decorate fresh copies per request
            # (two stat/read calls per worktree, no extra walking)
            all_wts = []
            for r in repos:
                fresh = []
                for w in r["worktrees"]:
                    w = dict(w)
                    p = w["path"]
                    wb = os.path.join(p, ".worktree-browser")
                    if os.path.isfile(wb):
                        try:
                            with open(wb) as f:
                                data = json.loads(f.read(2048))
                            port = int(data.get("port") or 0)
                            if 0 < port < 65536:
                                w["easel"] = {
                                    "port": port,
                                    "annotator": bool(data.get("annotator")),
                                    "cdp_url": str(data.get("cdp_url") or "")}
                        except Exception:
                            pass
                    ann = os.path.join(p, ".kilo", "studio-annotations.jsonl")
                    if os.path.isfile(ann):
                        _, ann_total = _annotation_rows(p)
                        w["annotations"] = ann_total
                    fresh.append(w)
                r["worktrees"] = fresh
                all_wts.extend(fresh)
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
            # Quest liveness decoration: `_quests` is TTL-cached and shared,
            # but `_all_quests()` returns fresh dicts per request — safe to
            # decorate. Three causes light the card's working dot:
            #   - a live agent in the quest's own worktree (serf/artist/MoC
            #     evaluating in place) — matched client-side via S.active;
            #   - a live master_of_coin whose --dir is the quest worktree or
            #     a fork named after the quest id (coin forks are siblings,
            #     not subdirs of the quest worktree);
            #   - a live gatekeeper integrating the quest's stamped cogship
            #     (gatehouse worktree) or running a size-1 convoy directly
            #     in the quest worktree. Convoy liveness covers ANY agent in
            #     the gatehouse worktree — remediation serfs working a
            #     rejected convoy integrate too, not just the gatekeeper.
            quests = _all_quests()
            _norm = os.path.normpath
            moc_dirs = [_norm(a["dir"]) for a in active.values()
                        if a.get("agent") == "master_of_coin"]
            gk_dirs = [_norm(a["dir"]) for a in active.values()
                        if a.get("agent") == "gatekeeper"]
            live_cogships = {m.group(1) for a in active.values()
                             for d in [_norm(a.get("dir") or "")]
                             for m in [re.search(r"(cogship-\d+)", d)] if m}
            for q in quests:
                wt = _norm(q.get("worktree") or "")
                qid = (q.get("id") or "").lower()
                q["moc_live"] = any(
                    wt == d or (qid and qid in d.lower()) for d in moc_dirs)
                q["cogship_live"] = (
                    q.get("cogship_id") in live_cogships
                    or wt in gk_dirs)
            self._json({
                "repos": repos,
                "worktrees": all_wts,
                "sessions": _sessions(),
                "tails": _turn_tails(),
                "quests": quests,
                "today": _today_totals(),
                "active": sorted(active.values(),
                                 key=lambda a: a.get("started") or 0),
                "mcp": _mcp_inventory(procs),
                "browser": _browser_runtime().browser_status(),
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
        elif self.path.startswith("/api/session/starts"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            ids = [s for s in (qs.get("ids", [""])[0] or "").split(",") if s]
            self._json(_session_starts(ids))
        elif self.path.startswith("/api/quest"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            root = next((r["root"] for r in _repos()
                         if r["key"] == qs.get("app", [""])[0]), None)
            if root is None:
                self._json({"error": "unknown app"}, 404)
            else:
                self._json(_quest_doc(
                    root, qs.get("wt", [""])[0], qs.get("id", [""])[0]))
        elif self.path.startswith("/api/cogship"):
            # Ship manifest document for one convoy: locate the repo whose
            # quests carry this cogship stamp, then render the read-only
            # `court ship <ids>` report (bare ship never mutates — the
            # confirm branch is the only mutating path in cmd_ship).
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            cs = qs.get("cogship", [""])[0].strip()
            if not re.fullmatch(r"cogship-\d+", cs):
                self._json({"error": "bad cogship id"}, 400)
            else:
                root, ids = None, []
                for r in _repos():
                    members = [q for q in _quests(r["root"])
                               if q.get("cogship_id") == cs]
                    if members:
                        root = r["root"]
                        ids = [q["id"] for q in members]
                        break
                if root is None:
                    self._json({"error": f"no quests stamped {cs}"}, 404)
                else:
                    try:
                        pr = subprocess.run(
                            ["python3", "-m", "court.cli", "ship",
                             ",".join(ids)],
                            cwd=root, capture_output=True, text=True,
                            timeout=90)
                        text = pr.stdout or ""
                        if pr.returncode != 0 and not text:
                            text = (pr.stderr or
                                    f"court ship exited {pr.returncode}")
                    except subprocess.TimeoutExpired:
                        text = "court ship timed out after 90s"
                    except Exception as e:
                        text = f"failed: {e}"
                    self._json({"title": f"🚢 {cs} — ship manifest",
                                "ids": ids, "text": text})
        elif self.path.startswith("/api/turns"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            try:
                lim = min(int(qs.get("limit", ["60"])[0]), 200)
            except Exception:
                lim = 60
            self._json(_journal_tail(lim))
        elif self.path.startswith("/api/annotations"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            wt = qs.get("wt", [""])[0]
            if not _annotation_wt_ok(wt):
                self._json({"ok": False,
                            "error": "unknown worktree"}, 403)
            else:
                items, total = _annotation_rows(wt)
                self._json({"ok": True, "items": items, "total": total})
        elif self.path.startswith("/api/sessions"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            wt = qs.get("wt", [""])[0]
            if wt:
                self._json(_sessions_for_wt(wt))
            else:
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
        elif self.path == "/api/settings":
            try:
                body = self._read_body()
            except Exception:
                self._json({"error": "bad request"}, 400)
                return
            code, payload = _settings_write(body)
            self._json(payload, code)
        elif self.path == "/api/browser":
            self._browser()
        elif self.path == "/api/send":
            self._send()
        elif self.path == "/api/stop":
            self._stop()
        elif self.path == "/api/court":
            self._court()
        elif self.path == "/api/session/delete":
            self._session_delete()
        elif self.path == "/api/annotation":
            self._annotation_add()
        else:
            self.send_error(404)

    def _annotation_add(self):
        """Studio-browser annotator ingest (CORS-open, agent-adjacent user
        content): stored verbatim, later rendered only through esc()."""
        try:
            n = int(self.headers.get("Content-Length", 0))
        except ValueError:
            n = 0
        if n > _ANNOTATION_BODY_CAP:
            self._json({"ok": False, "error": "annotation body too large"},
                       413, cors=True)
            return
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            self._json({"ok": False, "error": "malformed JSON body"},
                       400, cors=True)
            return
        if not isinstance(body, dict):
            self._json({"ok": False, "error": "body must be a JSON object"},
                       400, cors=True)
            return
        wt = body.get("worktree")
        if not _annotation_wt_ok(wt):
            self._json({"ok": False,
                        "error": "worktree is not an existing git checkout"},
                       400, cors=True)
            return
        note = body.get("note")
        if not isinstance(note, str) or not note.strip():
            self._json({"ok": False,
                        "error": "note must be a non-empty string"},
                       400, cors=True)
            return
        rec = {"worktree": os.path.normpath(wt), "url": "", "route": "",
               "selector": "", "tag": "", "text": "", "ts": "",
               "note": note[:2000]}
        for k, cap in _ANNOTATION_FIELD_CAPS.items():
            v = body.get(k)
            rec[k] = v[:cap] if isinstance(v, str) else ""
        try:
            total = _annotation_append(rec)
        except OSError as exc:
            self._json({"ok": False,
                        "error": f"annotation write failed: {exc}"},
                       500, cors=True)
            return
        self._json({"ok": True, "total": total}, cors=True)

    def _court(self):
        try:
            body = self._read_body()
            op = str(body.get("op", ""))
            raw_qid = body.get("id")
            qid = "" if raw_qid is None else str(raw_qid).strip()
            status = str(body.get("status", ""))
            note = str(body.get("note", ""))[:200]
            raw_ids = body.get("ids")
            ids = None
            ids_sent = isinstance(raw_ids, list)
            if ids_sent:
                clean = []
                for b in raw_ids[:50]:
                    bid = str(b).strip()
                    if re.fullmatch(r"[QqEeSs]?\d[\w-]{0,60}", bid):
                        clean.append(bid)
                ids = clean or None
        except Exception:
            self._json({"error": "bad request"}, 400)
            return
        if op not in _COURT_OP_ARITY and op != "advance":
            self._json({"error": "op not allowed"}, 403)
            return
        if op == "advance" and status not in STATUS_ORDER:
            self._json({"error": "status not allowed"}, 403)
            return
        if ids_sent and op not in ("collect", "studio", "ship"):
            self._json({"error": "ids only allowed for collect/studio/ship"}, 400)
            return
        # ship is the production deploy: --confirm only rides a request that
        # carries confirm:true — set exclusively by the board's dialog-
        # acknowledged confirm button. Any other ship call (scripts, stray
        # curls, UI bugs) degrades to the read-only manifest report.
        confirm = bool(body.get("confirm")) and op == "ship"
        if ids_sent and not ids:
            # A bulk request whose selection sanitized to nothing must fail
            # loudly — never degrade to a bare pack-everything run.
            self._json({"error": "no valid quest ids in selection"}, 400)
            return
        _JOB_SEQ[0] += 1
        jid = _JOB_SEQ[0]
        job = {"events": [], "done": False, "started": time.time(),
               "dir": "", "agent": "court:" + op, "op": op,
               "qid": ",".join(ids) if ids else qid}
        _JOBS[str(jid)] = job
        threading.Thread(target=_court_op, daemon=True,
                         args=(job, op, qid, status, note, ids, confirm)).start()
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

    def _browser(self):
        """Idempotently launch the shared studio Chromium (headed, persistent
        profile, CDP) and pin the annotation overlay to the given worktree."""
        try:
            body = self._read_body()
            wt = str(body.get("wt", ""))
        except Exception:
            self._json({"error": "bad request"}, 400)
            return
        wt = os.path.normpath(wt) if wt else ""
        if not wt or wt not in _known_dirs() or not os.path.isdir(wt):
            self._json({"error": "worktree is not a known directory; refused"}, 403)
            return
        studio_browser = _browser_runtime()
        try:
            server = _ensure_runserver(wt)
            info = studio_browser.start_browser(
                headless=False, annotate_worktree=Path(wt))
        except Exception as exc:
            self._json({"error": f"browser launch failed: {exc}"}, 500)
            return
        status = studio_browser.browser_status()
        tab = None
        closed_blanks = 0
        if server["up"] and info.get("port"):
            try:
                tab = _cdp_open_tab(info["port"], server["url"])
            except Exception:
                tab = None
            try:
                closed_blanks = _cdp_close_blanks(
                    info["port"], keep_id=(tab or {}).get("id"))
            except Exception:
                closed_blanks = 0
        self._json({"ok": True, "running": True,
                    "cdp_url": info.get("cdp_url"), "port": info.get("port"),
                    "pid": info.get("pid"),
                    "annotator_pid": info.get("annotator_pid"),
                    "annotator_alive": bool(status.get("annotator_alive")),
                    "server": server, "tab": tab,
                    "closed_blanks": closed_blanks})

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
