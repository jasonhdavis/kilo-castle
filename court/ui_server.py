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
<html><head><meta charset="utf-8"><title>Court Console</title>
<style>
:root{--bg:#11141a;--panel:#1a1f29;--edge:#2a3140;--ink:#d7dde7;--dim:#8b93a3;
--accent:#e8b84b;--red:#e06c60;--green:#7fbf7f;--blue:#6ea3d8}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--ink);font:13px/1.45 -apple-system,Menlo,monospace;
display:grid;grid-template-rows:auto 1fr;height:100vh}
header{display:flex;align-items:center;gap:16px;padding:8px 14px;background:var(--panel);
border-bottom:1px solid var(--edge)}
header h1{font-size:14px;color:var(--accent);letter-spacing:1px}
header .totals{color:var(--dim)} header .totals b{color:var(--ink)}
main{display:grid;grid-template-columns:290px 1fr;overflow:hidden}
nav{overflow-y:auto;border-right:1px solid var(--edge);padding:10px}
nav .castle{background:var(--panel);border:1px solid var(--accent);border-radius:6px;
padding:8px 10px;margin-bottom:10px;cursor:pointer}
nav .castle.sel{outline:2px solid var(--accent)}
nav h2{font-size:10px;text-transform:uppercase;color:var(--dim);letter-spacing:1px;
margin:12px 4px 4px}
nav .q{padding:5px 8px;border-radius:5px;cursor:pointer;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}
nav .q:hover{background:var(--panel)}
nav .q .st{font-size:10px;padding:0 4px;border-radius:3px;margin-right:6px}
.st.WORKING{background:#3d3d1f;color:var(--accent)}
.st.TRIBUTE_READY{background:#1f3d2a;color:var(--green)}
.st.GATE{background:#1f2a3d;color:var(--blue)}
.st.PLANNED,.st.OPEN{background:#2a2a33;color:var(--dim)}
.st.READY_TO_RAZE{background:#3d2320;color:var(--red)}
#content{overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:14px}
.card{background:var(--panel);border:1px solid var(--edge);border-radius:8px;padding:10px 12px}
.card h3{font-size:12px;color:var(--accent);margin-bottom:6px;font-weight:600}
.card .meta{color:var(--dim);font-size:11px;margin-bottom:8px}
.tabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px}
.tab{padding:3px 10px;border:1px solid var(--edge);border-radius:5px;cursor:pointer;
font-size:11px;color:var(--dim);max-width:240px;white-space:nowrap;overflow:hidden;
text-overflow:ellipsis}
.tab.on{color:var(--bg);background:var(--accent);border-color:var(--accent)}
.sesslist{display:none}
.sesslist.on{display:block}
table{width:100%;border-collapse:collapse;font-size:11px}
td,th{padding:3px 8px;text-align:left;border-bottom:1px solid var(--edge)}
th{color:var(--dim);font-weight:500}
.rss{color:var(--blue)}
.badge{font-size:10px;padding:1px 6px;border-radius:3px;background:#3d2320;color:var(--red);
margin-left:6px}
button{background:none;border:1px solid var(--edge);color:var(--dim);border-radius:4px;
padding:1px 8px;cursor:pointer;font-size:10px}
button:hover{border-color:var(--red);color:var(--red)}
#drawer{position:fixed;right:0;top:0;bottom:0;width:46%;background:var(--panel);
border-left:1px solid var(--edge);display:none;flex-direction:column;z-index:5}
#drawer.on{display:flex}
#drawer .hd{padding:10px 14px;border-bottom:1px solid var(--edge);display:flex;
justify-content:space-between;align-items:center}
#drawer .hd b{color:var(--accent)} #drawer .hd span{cursor:pointer;color:var(--dim)}
#drawer .body{overflow-y:auto;padding:10px 14px;white-space:pre-wrap;font-size:12px}
.msg{margin-bottom:10px;padding:8px 10px;border-radius:6px;background:var(--bg)}
.msg.user{border-left:3px solid var(--accent)}
.msg.assistant{border-left:3px solid var(--blue)}
.msg .who{color:var(--dim);font-size:10px;margin-bottom:3px}
</style></head><body>
<header><h1>CASTLE CONSOLE</h1><div class="totals" id="totals"></div>
<div style="flex:1"></div><div class="totals" id="clock"></div></header>
<main><nav id="nav"></nav><div id="content"></div></main>
<div id="drawer"><div class="hd"><b id="dtitle"></b><span onclick="closeDrawer()">close ✕</span></div>
<div class="body" id="dbody"></div></div>
<script>
let S=null, selWt=null, selSess=null;
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const mb=r=>r==null?'—':(r/1048576).toFixed(0)+' MB';
async function poll(){try{S=await (await fetch('/api/state')).json();render()}catch(e){}}
function render(){
 const t=document.getElementById('totals');
 t.innerHTML=`quests <b>${S.quests.length}</b> · sessions <b>${S.sessions.length}</b> · kilo RSS <b>${mb(S.proc_total_rss)}</b>`;
 document.getElementById('clock').textContent=new Date().toLocaleTimeString();
 const nav=document.getElementById('nav');
 const groups={};
 for(const q of S.quests){(groups[q.status||'OPEN']??=[]).push(q)}
 let h=`<div class="castle ${selWt==='CASTLE'?'sel':''}" onclick="pick('CASTLE')">
 <b style="color:var(--accent)">🏰 CASTLE</b><div style="color:var(--dim);font-size:10px">${esc(S.repo_name)} trunk</div></div>`;
 for(const st of ${json.dumps(STATUS_ORDER)}){
  if(!groups[st])continue;
  h+=`<h2>${st} (${groups[st].length})</h2>`;
  for(const q of groups[st]){
   const dirty=q.dirty?'<span class="badge">dirty</span>':'';
   h+=`<div class="q" onclick="pick(${JSON.stringify(q.worktree||'').replace(/"/g,'&quot;')})">
   <span class="st ${esc(st)}">${st.slice(0,4)}</span>${esc(q.id)}${dirty}<br>
   <span style="color:var(--dim);font-size:10px">${esc(q.title)}</span></div>`;
  }
 }
 nav.innerHTML=h;
 const c=document.getElementById('content');
 const wts=S.worktrees.filter(w=>selWt===null||w.path===selWt||selWt==='CASTLE'&&w.path===S.repo_root);
 c.innerHTML=wts.map(w=>{
  const sess=S.sessions.filter(s=>s.directory&&s.directory.startsWith(w.path));
  return cardWt(w,sess);
 }).join('')||'<div class="card">select a section on the left</div>';
}
function cardWt(w,sess){
 let h=`<div class="card"><h3>${esc(w.branch||w.path)}</h3>
 <div class="meta">${esc(w.path)} ${w.dirty?'<span class="badge">dirty</span>':''}</div>`;
 if(!sess.length)return h+'<div class="meta">no sessions</div></div>';
 h+='<div class="tabs">'+sess.map((s,i)=>
  `<div class="tab ${selSess===s.id?'on':''}" onclick="openSess('${s.id}')">${esc(s.agent||'?')}: ${esc(s.title||s.id)}</div>`).join('')+'</div>';
 h+='<table><tr><th>session</th><th>agent</th><th>model</th><th>tokens in/out</th><th>updated</th></tr>'+
  sess.map(s=>`<tr style="cursor:pointer" onclick="openSess('${s.id}')"><td>${esc((s.title||s.id).slice(0,42))}</td><td>${esc(s.agent||'')}</td><td style="color:var(--dim)">${esc((s.model||'').replace('openrouter/',''))}</td><td>${s.tokens_input??'—'}/${s.tokens_output??'—'}</td><td style="color:var(--dim)">${ago(s.time_updated)}</td></tr>`).join('')+'</table></div>';
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
