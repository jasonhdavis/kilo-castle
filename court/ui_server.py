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
.dupwarn{background:#3d2320;color:var(--red);border-radius:5px;padding:4px 8px;
margin-bottom:6px;font-size:11px}
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
 c.innerHTML=(wts.map(w=>{
  const sess=S.sessions.filter(s=>s.directory&&s.directory.startsWith(w.path));
  return cardWt(w,sess);
 }).join('')||'<div class="card">select a section on the left</div>')+cardMcp();
}
function cardMcp(){
 const list=S.mcp||[];
 let h='<div class="card"><h3>MCP SERVERS</h3>';
 for(const d of list.filter(m=>m.type==='local'&&m.enabled&&m.count>=2)){
  h+=`<div class="dupwarn">duplicate MCP spawns detected: ${esc(d.name)} x${d.count} (~${mb(d.rss)} MB each)</div>`;
 }
 h+='<table><tr><th>name</th><th>scope</th><th>type</th><th>enabled</th><th>running</th><th></th></tr>'+
  list.map(m=>{
   const run=m.running?`<span style="color:var(--green)">yes</span> <span class="rss">${mb(m.rss)}</span>`
    :'<span style="color:var(--dim)">no</span>';
   const btn=`<button title="takes effect for sessions started after the change" onclick="mcpToggle(${
    JSON.stringify(m.file).replace(/"/g,'&quot;')},${JSON.stringify(m.name)})">${m.enabled?'disable':'enable'}</button>`;
   return `<tr><td>${esc(m.name)}</td><td style="color:var(--dim)">${esc(m.scope)}</td><td>${esc(m.type)}</td>`+
    `<td>${m.enabled?'<span style="color:var(--green)">yes</span>':'no'}</td><td>${run}</td><td>${btn}</td></tr>`;
  }).join('')+'</table>'+
  `<div class="meta" style="margin-top:6px">toggles edit the config file (a .bak copy is kept) and `+
  `take effect for sessions started after the change</div></div>`;
 return h;
}
async function mcpToggle(file,name){
 const m=(S.mcp||[]).find(x=>x.file===file&&x.name===name);
 if(!m)return;
 const r=await fetch('/api/mcp',{method:'POST',body:JSON.stringify({file,name,enabled:!m.enabled})});
 if(!r.ok)alert('refused: '+(await r.text()));else poll();
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
            procs = _ps_procs()
            flagged, total = _processes(procs)
            self._json({
                "repo_name": os.path.basename(COURT_DIR),
                "repo_root": COURT_DIR,
                "quests": _quests(),
                "worktrees": _worktrees(),
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
        else:
            self.send_error(404)

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
