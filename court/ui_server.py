"""Zero-dependency localhost web console for the court pipeline.

Reads: .court quest files, git worktrees, the local kilo session DB (read-only),
and the live process table. The only mutating endpoint is /api/reap, which
terminates a process whose parent is a verified kilo process.
"""

import json
import os
import re
import signal
import sqlite3
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COURT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KILO_DB = os.path.expanduser("~/.local/share/kilo/kilo.db")
CHILD_PATTERNS = ("mcp", "npx", "npm", "chromium", "chrome", "pytest", "playwright")
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
.empty-note{color:var(--faint);text-align:center;padding:30px;font-size:12px}
</style></head><body>
<header><div class="brand"><span class="glyph">♜</span>CASTLE <em>CONSOLE</em></div>
<div class="vdiv"></div><div id="totals" style="display:flex;gap:8px"></div>
<div class="spacer"></div><div id="clock"></div></header>
<main><nav id="nav"></nav><div id="content"></div></main>
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
  `<span class="chip">quests <b>${S.quests.length}</b></span>
   <span class="chip">sessions <b>${S.sessions.length}</b></span>
   <span class="chip rss">kilo RSS <b>${mb(S.proc_total_rss)}</b></span>`+
  (flagged?`<span class="chip" style="border-color:rgba(248,81,73,.4)"><b style="color:var(--red)">${flagged} flagged</b></span>`:'');
 document.getElementById('clock').textContent=new Date().toLocaleTimeString();
 const nav=document.getElementById('nav');
 const groups={};
 for(const q of S.quests){(groups[q.status||'OPEN']??=[]).push(q)}
 let h=`<div class="castle ${selWt===null?'sel':''}" onclick="pick(null)">
 <div class="name"><span class="dot"></span>CASTLE</div>
 <div class="sub">${esc(S.repo_name)} · trunk</div></div>`;
 for(const st of ${json.dumps(STATUS_ORDER)}){
  if(!groups[st])continue;
  h+=`<h2>${st} · ${groups[st].length}</h2>`;
  for(const q of groups[st]){
   const dirty=q.dirty?'<span class="badge">dirty</span>':'';
   h+=`<div class="q" onclick="pick(${JSON.stringify(q.worktree||'').replace(/"/g,'&quot;')})">
   <div class="row1"><span class="st ${esc(st)}">${st.slice(0,4)}</span>${esc(q.id)}${dirty}</div>
   <div class="row2">${esc(q.title)}</div></div>`;
  }
 }
 nav.innerHTML=h;
 const c=document.getElementById('content');
 if(selWt==='CASTLE'){selWt=S.repo_root}
 const wts=S.worktrees.filter(w=>selWt===null||w.path===selWt);
 c.innerHTML=wts.map(w=>{
  const sess=S.sessions.filter(s=>s.directory&&s.directory.startsWith(w.path));
  return cardWt(w,sess);
 }).join('')||'<div class="card empty">select a section on the left</div>';
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
async function openSess(id){selSess=id;render();
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
poll();setInterval(poll,5000);
</script></body></html>"""


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


def _worktrees():
    try:
        r = subprocess.run(
            ["git", "-C", COURT_DIR, "worktree", "list", "--porcelain"],
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
            cur["branch"] = ln[7:]
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


def _sessions(limit=60):
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


def _processes():
    try:
        r = subprocess.run(
            ["ps", "axo", "pid=,ppid=,rss=,etime=,args="],
            capture_output=True, text=True, timeout=5)
    except Exception:
        return [], 0
    procs, total, by_pid = [], 0, {}
    for ln in r.stdout.splitlines():
        parts = ln.strip().split(None, 4)
        if len(parts) < 5:
            continue
        p = {"pid": int(parts[0]), "ppid": int(parts[1]), "rss": int(parts[2]) * 1024,
             "etime": parts[3], "args": parts[4]}
        by_pid[p["pid"]] = p
        procs.append(p)
        if "kilo" in p["args"] and ("serve" in p["args"] or "run" in p["args"]):
            total += p["rss"]
    for p in procs:
        low = p["args"].lower()
        parent = by_pid.get(p["ppid"])
        p["kilo_child"] = bool(parent and "kilo" in parent["args"])
        p["flag"] = p["kilo_child"] and any(pat in low for pat in CHILD_PATTERNS)
    flagged = [p for p in procs if p["flag"]]
    return flagged, total


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
        elif self.path == "/api/state":
            flagged, total = _processes()
            self._json({
                "repo_name": os.path.basename(COURT_DIR),
                "repo_root": COURT_DIR,
                "quests": _quests(),
                "worktrees": _worktrees(),
                "sessions": _sessions(),
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
        if self.path != "/api/reap":
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            pid = json.loads(self.rfile.read(n)).get("pid")
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
