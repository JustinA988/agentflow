"""Local web UI for agentflow: start runs, watch agent output, diff, tests, and history.

Binds to 127.0.0.1 only. `agentflow ui` then open http://localhost:8765
"""
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from urllib.parse import parse_qs, urlsplit

from agentflow import load_agents, parse_ref, resolve, run

PORT = 8765
HISTORY = Path.home() / ".agentflow-history.json"
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
runs, lock = [], threading.Lock()


def load_history(path=HISTORY):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_history(path=HISTORY):
    with lock:
        data = json.dumps(runs)
    path.write_text(data, encoding="utf-8")


def list_items(repo):
    """Open issues and PRs for repo, newest first."""
    parse_ref(f"{repo}#1")  # validates owner/repo before it reaches gh
    fields = ["--json", "number,title,author", "--limit", "50"]
    out = []
    for kind in ("issue", "pr"):
        for i in json.loads(run(["gh", kind, "list", "-R", repo, *fields])):
            out.append({"kind": kind, "number": i["number"], "title": i["title"], "author": i["author"]["login"]})
    return sorted(out, key=lambda i: -i["number"])


def start_run(ref, agent_name, test_cmd, agents, history=HISTORY):
    parse_ref(ref)  # validate before starting a thread
    if agent_name not in agents:
        raise ValueError(f"unknown agent {agent_name!r}")
    with lock:
        r = {"id": max((x["id"] for x in runs), default=0) + 1, "ref": ref, "agent": agent_name, "status": "running",
             "started": time.time(), "pr": None, "error": None, "events": []}
        runs.insert(0, r)

    def log(kind, text):
        with lock:
            r["events"].append([kind, text])

    def work():
        try:
            r["pr"] = resolve(ref,agents[agent_name], test_cmd or None, log)
            r["status"] = "draft" if any(k == "tests" and t.startswith("FAILED") for k, t in r["events"]) else "done"
        except subprocess.CalledProcessError as e:
            r["status"], r["error"] = "failed", f"{' '.join(map(str, e.cmd[:3]))} failed: {e.stderr or e}"
        except Exception as e:  # surface every failure in the UI instead of a dead thread
            r["status"], r["error"] = "failed", str(e)
        save_history(history)

    threading.Thread(target=work, daemon=True).start()
    return r


class Handler(BaseHTTPRequestHandler):
    agents = {}

    def send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        # Host check blocks DNS rebinding; JSON-only POST forces a CORS preflight we never answer (blocks CSRF).
        return self.headers.get("Host") in ALLOWED_HOSTS

    def do_GET(self):
        if not self.trusted():
            return self.send(403, {"error": "bad host"})
        if self.path == "/":
            return self.send(200, PAGE, "text/html")
        if self.path == "/api/runs":
            with lock:
                return self.send(200, {"runs": runs, "agents": list(self.agents)})
        url = urlsplit(self.path)
        if url.path == "/api/items":
            try:
                return self.send(200, {"items": list_items(parse_qs(url.query).get("repo", [""])[0].strip())})
            except ValueError as e:
                return self.send(400, {"error": str(e)})
            except subprocess.CalledProcessError as e:
                return self.send(400, {"error": (e.stderr or str(e)).strip()})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.trusted() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(403, {"error": "forbidden"})
        if self.path != "/api/run":
            return self.send(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            r = start_run(body.get("ref", "").strip(), body.get("agent", "claude"), body.get("test", "").strip(), self.agents)
        except ValueError as e:
            return self.send(400, {"error": str(e)})
        self.send(200, {"id": r["id"]})

    def log_message(self, *args):
        pass  # keep the console quiet; the page shows everything


def serve(port=PORT):
    Handler.agents = load_agents()
    runs.extend(r for r in load_history() if r.get("status") != "running")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"agentflow UI: http://localhost:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>agentflow</title>
<style>
:root{--bg:#f7f7f8;--panel:#fff;--text:#1b1b1f;--muted:#666;--border:#ddd;--add:#e6f4ea;--addt:#14532d;--del:#fdecea;--delt:#7f1d1d;--accent:#2563eb;--ok:#15803d;--bad:#b91c1c;--warn:#b45309;--code:#f1f1f3}
@media (prefers-color-scheme:dark){:root{--bg:#111214;--panel:#1a1b1e;--text:#e8e8ea;--muted:#9a9aa2;--border:#2c2d31;--add:#12321f;--addt:#9ae6b4;--del:#3b1515;--delt:#fca5a5;--accent:#60a5fa;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--code:#141518}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,sans-serif}
header{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
h1{font-size:18px;margin:0 12px 0 0}form{display:flex;gap:8px;flex-wrap:wrap;flex:1}
input,select,button{font:inherit;padding:6px 10px;border:1px solid var(--border);border-radius:6px;background:var(--panel);color:var(--text)}
input[name=ref]{min-width:240px;flex:1}button{background:var(--accent);color:#fff;border:0;cursor:pointer}
main{display:grid;grid-template-columns:280px 1fr;min-height:calc(100vh - 60px)}
@media (max-width:760px){main{grid-template-columns:1fr}}
aside{border-right:1px solid var(--border);overflow:auto}
#rf{padding:10px 16px;border-bottom:1px solid var(--border);flex:none}#rf input{flex:1;min-width:0}
.item{display:flex;gap:8px;align-items:flex-start;justify-content:space-between;padding:8px 16px;border-bottom:1px solid var(--border)}
.item button{flex:none;padding:2px 10px}.tag{font-size:11px;border:1px solid var(--border);border-radius:4px;padding:0 4px;margin-right:4px}
.pad{padding:8px 16px;margin:0}.side{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:14px 16px 4px}nav button{display:block;width:100%;text-align:left;background:none;color:var(--text);border-radius:0;border-bottom:1px solid var(--border);padding:10px 16px}
nav button[aria-current=true]{background:var(--panel)}.muted{color:var(--muted);font-size:12px}
.running{color:var(--accent)}.done{color:var(--ok)}.failed{color:var(--bad)}.draft{color:var(--warn)}
section{padding:16px;overflow:auto}h2{font-size:15px;margin:20px 0 6px}
pre{background:var(--code);border:1px solid var(--border);border-radius:6px;padding:10px;margin:0;overflow:auto;max-height:420px;white-space:pre-wrap;word-break:break-word;font:12px/1.45 ui-monospace,Consolas,monospace}
.add{background:var(--add);color:var(--addt);display:block}.del{background:var(--del);color:var(--delt);display:block}.hunk{color:var(--accent);display:block}
#err{color:var(--bad)}a{color:var(--accent)}
</style></head><body>
<header><h1>agentflow</h1>
<form id="f"><input id="ref" name="ref" placeholder="owner/repo#123" required aria-label="Issue or PR (owner/repo#number)" title="Issue: opens a PR. PR: fixes new review comments.">
<select name="agent" id="agent" aria-label="Agent"></select>
<input name="test" placeholder="test command (auto)" aria-label="Test command">
<button>Run</button><span id="err" role="alert"></span></form></header>
<main><aside>
<form id="rf"><input id="repo" placeholder="owner/repo" aria-label="Repository to list issues and PRs from" required><button>Load</button></form>
<div id="items" aria-label="Open issues and PRs"></div>
<h3 class="side">History</h3><nav id="list" aria-label="Run history"></nav></aside><section id="detail"><p class="muted">Start a run or pick one from the history.</p></section></main>
<script>
let sel=null,data={runs:[]},lastKey='';
const $=s=>document.querySelector(s),el=(t,c,txt)=>{const e=document.createElement(t);if(c)e.className=c;if(txt!=null)e.textContent=txt;return e};
$('#f').onsubmit=async e=>{e.preventDefault();$('#err').textContent='';
 const b=Object.fromEntries(new FormData(e.target));
 const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
 const j=await r.json();if(!r.ok)return $('#err').textContent=j.error;sel=j.id;poll()};
function diffPre(text){const p=el('pre');for(const line of text.split('\n')){
 const c=line.startsWith('+')&&!line.startsWith('+++')?'add':line.startsWith('-')&&!line.startsWith('---')?'del':line.startsWith('@@')?'hunk':'';
 p.append(c?el('span',c,line):document.createTextNode(line+'\n'))}return p}
function render(){
 const key=JSON.stringify([data.runs.map(r=>[r.id,r.status,r.events.length]),sel]);if(key===lastKey)return;lastKey=key;
 const ag=$('#agent');if(!ag.options.length)for(const a of data.agents)ag.append(new Option(a,a));
 const list=$('#list');list.replaceChildren(...data.runs.map(r=>{const b=el('button');b.setAttribute('aria-current',r.id===sel);
  b.append(el('div',null,r.ref),el('div','muted '+r.status,`${r.status} · ${r.agent} · ${new Date(r.started*1000).toLocaleString()}`));
  b.onclick=()=>{sel=r.id;render()};return b}));
 const r=data.runs.find(r=>r.id===sel);if(!r)return;const d=$('#detail');d.replaceChildren();
 const h=el('h2',null,`${r.ref} — `);h.append(el('span',r.status,r.status));d.append(h);
 if(r.pr){const a=el('a',null,r.pr);a.href=r.pr;a.target='_blank';d.append(el('p',null,'PR: '),a)}
 if(r.error)d.append(el('pre','del',r.error));
 const ev=k=>r.events.filter(e=>e[0]===k).map(e=>e[1]);
 d.append(el('h2',null,'Progress'),el('pre',null,ev('status').join('\n')||'…'));
 d.append(el('h2',null,'Agent output'),el('pre',null,ev('agent').join('\n')||'(none yet)'));
 const diff=ev('diff')[0];if(diff)d.append(el('h2',null,'Changes'),diffPre(diff));
 const t=ev('tests')[0];if(t)d.append(el('h2',null,'Tests'),el('pre',t.startsWith('FAILED')?'del':t.startsWith('PASSED')?'add':'',t));
}
async function poll(){try{data=await (await fetch('/api/runs')).json();if(sel==null&&data.runs[0])sel=data.runs[0].id;render()}catch(e){}}
poll();setInterval(poll,1000);
$('#rf').onsubmit=async e=>{e.preventDefault();const repo=$('#repo').value.trim();try{localStorage.setItem('af-repo',repo)}catch(_){}
 const box=$('#items');box.replaceChildren(el('p','muted pad','Loading…'));
 const r=await fetch('/api/items?repo='+encodeURIComponent(repo)),j=await r.json();
 if(!r.ok)return box.replaceChildren(el('p','failed pad',j.error));
 box.replaceChildren(...(j.items.length?j.items.map(i=>{const row=el('div','item'),t=el('div');
  t.append(el('span','tag',i.kind==='pr'?'PR':'Issue'),`#${i.number} ${i.title}`,el('div','muted','@'+i.author));
  const b=el('button',null,'Run');b.setAttribute('aria-label',`Run agent on ${i.kind==='pr'?'PR':'issue'} #${i.number}`);
  b.onclick=()=>{$('#ref').value=`${repo}#${i.number}`;$('#f').requestSubmit()};row.append(t,b);return row})
  :[el('p','muted pad','No open issues or PRs.')]))};
try{$('#repo').value=localStorage.getItem('af-repo')||''}catch(_){}
if($('#repo').value)$('#rf').requestSubmit();
</script></body></html>"""
