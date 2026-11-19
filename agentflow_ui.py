"""agentflow app: chat with any coding agent about a GitHub repo or a folder on your PC, all in one window.

Binds to 127.0.0.1 only. `agentflow app` opens it in its own window; `agentflow ui` serves http://localhost:8765
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from agentflow import (DIFF_LIMIT, LOG_LIMIT, TEXT, agent_env, build_cmd, detect_test_cmd, exe_path, final_answer,
                       load_agents, parse_ref, readable, report_failure, resolve, run, safe_cmd, session_of, test_note,
                       watch, with_model)

PORT = 8765
HOME = Path.home() / ".agentflow"
CHATS, MCP_FILE, MCP_ACTIVE, WORK = HOME / "chats.json", HOME / "mcp.json", HOME / "mcp-active.json", HOME / "work"
SCRATCH = Path.home() / "agentflow-projects"  # where "start from scratch" projects go
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # git's empty tree: a brand-new repo can diff before its first commit
MAX_TEAM = 4
ALLOWED_HOSTS = {f"localhost:{PORT}", f"127.0.0.1:{PORT}"}
HISTORY_CHARS = 12000  # earlier turns replayed to agents that can't resume a session
STEPS_KEPT = 400
SLASH = re.compile(r"/(issue|fix|review)\s+#?(\d+)(\s+--trust)?")
chats, procs, lock = {}, {}, threading.RLock()


# ---------- storage ----------

def load_chats(path=None):
    try:
        data = json.loads((path or CHATS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    for c in data:  # whatever was running died with the last app session
        c["status"] = "idle"
        for m in c["messages"]:
            if m.get("status") == "running":
                m["status"] = "stopped"
    return {c["id"]: c for c in data}


def save_chats(path=None):
    path = path or CHATS
    with lock:
        data = json.dumps(list(chats.values()))
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(path)  # never leave a half-written chats file


def load_mcp():
    try:
        servers = json.loads(MCP_FILE.read_text(encoding="utf-8")).get("mcpServers", {})
        return servers if isinstance(servers, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def parse_mcp(text):
    """MCP servers from pasted JSON: {"mcpServers": {...}} (Claude/Cursor), {"servers": {...}} (VS Code) or {name: {...}}."""
    try:
        data = json.loads(text)
    except ValueError as e:
        raise ValueError(f"that isn't valid JSON ({e})")
    servers = (data.get("mcpServers") or data.get("servers") or data) if isinstance(data, dict) else None
    if not isinstance(servers, dict) or not servers or not all(
            isinstance(s, dict) and (s.get("command") or s.get("url") or s.get("httpUrl")) for s in servers.values()):
        raise ValueError('expected MCP servers, like {"mcpServers": {"name": {"command": "npx", "args": ["..."]}}}')
    return servers


def save_mcp(servers):
    MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
    MCP_FILE.write_text(json.dumps({"mcpServers": servers}, indent=2), encoding="utf-8")


def active_mcp(servers):
    """Write the enabled servers in the {"mcpServers": ...} format agents read. None if there are none."""
    on = {n: {k: v for k, v in s.items() if k != "disabled"} for n, s in servers.items() if not s.get("disabled")}
    if not on:
        return None
    MCP_ACTIVE.parent.mkdir(parents=True, exist_ok=True)
    MCP_ACTIVE.write_text(json.dumps({"mcpServers": on}), encoding="utf-8")
    return str(MCP_ACTIVE)


def edit_mcp(body):
    servers, name, action = load_mcp(), body.get("name"), body.get("action")
    if action == "add":
        servers.update(parse_mcp(body.get("text", "")))
    elif name not in servers:
        raise ValueError(f"no MCP server named {name!r}")
    elif action == "toggle":
        servers[name]["disabled"] = not servers[name].get("disabled")
    elif action == "delete":
        del servers[name]
    else:
        raise ValueError("unknown MCP action")
    save_mcp(servers)
    return {"mcp": servers}


# ---------- chats ----------

def get(cid):
    if cid not in chats:
        raise ValueError("that chat no longer exists")
    return chats[cid]


def git_head(d):
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True, **TEXT)
    return r.stdout.strip() if r.returncode == 0 else None


def check_team(team, agents):
    """[{name, model, role}]: 1 to MAX_TEAM different agents, each with a model it accepts."""
    if not isinstance(team, list) or not 1 <= len(team) <= MAX_TEAM:
        raise ValueError(f"pick 1 to {MAX_TEAM} agents")
    out = []
    for m in team:
        name = m.get("name") if isinstance(m, dict) else None
        if name not in agents:
            raise ValueError(f"unknown agent {name!r}")
        model, role = str(m.get("model") or "").strip(), str(m.get("role") or "").strip()[:200]
        with_model(agents[name], model or None)
        out.append({"name": name, "model": model, "role": role})
    if len({m["name"] for m in out}) < len(out):
        raise ValueError("each agent can only be on the team once")
    return out


def team_of(chat):  # chats saved before teams existed have a single agent/model
    return chat.get("team") or [{"name": chat["agent"], "model": chat.get("model", ""), "role": ""}]


def new_chat(kind, target, team, agents, name=""):
    team = check_team(team, agents)
    cid = uuid.uuid4().hex[:10]
    if kind == "new":  # no project picked: start from scratch in a fresh folder named after the first message
        slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower())[:40].strip("-") or "project"
        p = SCRATCH / f"{slug}-{cid[:4]}"
        p.mkdir(parents=True)
        run(["git", "init", "-q"], p)
        kind, target, d, base = "local", str(p), str(p), EMPTY_TREE
    elif kind == "github":
        parse_ref(f"{target}#1")  # validates owner/repo before it ever reaches gh
        d, base = str(WORK / cid), None  # cloned on the first message
    elif kind == "local":
        p = Path(target).expanduser()
        if not target or not p.is_dir():
            raise ValueError(f"folder not found: {target or '(none)'}")
        d = target = str(p.resolve())
        base = git_head(d)
    else:
        raise ValueError("pick a GitHub repo or a folder first")
    now = time.time()
    chat = {"id": cid, "title": "New chat", "kind": kind, "target": target, "dir": d, "base": base, "branch": None,
            "team": team, "agent": team[0]["name"], "sessions": {}, "pr": None,
            "status": "idle", "diff": "", "created": now, "updated": now, "messages": []}
    with lock:
        chats[cid] = chat
    save_chats()
    return chat


def set_team(cid, team, agents):
    team = check_team(team, agents)
    with lock:
        chat = get(cid)
        chat["team"], chat["agent"] = team, team[0]["name"]
    save_chats()
    return chat


def with_history(messages, text):
    """Agents that can't resume a session get the conversation so far (newest kept) with each message."""
    past = "\n\n".join(f"{'User' if m['role'] == 'user' else m.get('agent') or 'Assistant'}: {m['text']}"
                       for m in messages if m.get("text"))[-HISTORY_CHARS:]
    return f"Conversation so far:\n{past}\n\nNew message from the user:\n{text}" if past else text


def new_msg(chat, agent, label=""):
    msg = {"role": "agent", "agent": agent, "label": label, "text": "", "steps": [], "status": "running"}
    with lock:
        chat["messages"].append(msg)
        chat["updated"] = time.time()
    return msg


def run_part(msg, fn):
    """Run fn and record how it ended on msg. Returns the exception, if any."""
    try:
        fn()
        if msg["status"] == "running":
            msg["status"] = "done"
    except Exception as e:  # every failure lands in the chat instead of a dead thread
        msg["status"] = "error"
        if isinstance(e, FileNotFoundError):
            msg["error"] = f"couldn't start {msg['agent']}: is it installed? Settings shows how to install it."
        elif isinstance(e, subprocess.CalledProcessError):
            msg["error"] = f"{' '.join(map(str, e.cmd[:3]))} failed: {(e.stderr or '').strip() or e}"
        else:
            msg["error"] = str(e)
        return e


def start(chat, job, notify=None):
    """Add a running message from the lead agent and run job(msg) in the background."""
    msg = new_msg(chat, team_of(chat)[0]["name"])
    with lock:
        chat["status"] = "running"

    def work():
        try:
            if (e := run_part(msg, lambda: job(msg))) and notify:
                report_failure(notify, e)
        finally:
            with lock:
                chat["status"], chat["updated"] = "idle", time.time()
                procs.pop(chat["id"], None)
            save_chats()

    threading.Thread(target=work, daemon=True).start()
    return chat


def send(cid, text, agents, notify=None):
    text = (text or "").strip()
    with lock:
        chat = get(cid)
        if chat["status"] == "running":
            raise ValueError("still working on the last message")
        if not text:
            raise ValueError("type a message first")
        m = SLASH.fullmatch(text)
        if m and chat["kind"] != "github":
            raise ValueError("/issue, /fix and /review work in GitHub repo chats")
        past = list(chat["messages"])
        chat["messages"].append({"role": "user", "text": text})
        if chat["title"] == "New chat":
            chat["title"] = f"{m.group(1).title()} #{m.group(2)}" if m else text.splitlines()[0][:60]
    if m:
        return start(chat, lambda msg: slash_turn(chat, msg, m, agents), notify)
    if len(team_of(chat)) > 1:
        return start(chat, lambda msg: team_turn(chat, msg, text, past, agents))
    role = team_of(chat)[0]["role"]
    prompt = f"(Your role in this project: {role}.)\n\n{text}" if role else text
    return start(chat, lambda msg: agent_turn(chat, msg, prompt, past, agents))


def step(msg, text):
    with lock:
        msg["steps"].append(text)
        del msg["steps"][:-STEPS_KEPT]


def prepare(chat, msg):
    """The chat's working folder: the user's own folder, or our clone of the GitHub repo on its own branch."""
    if chat["kind"] == "local" or (Path(chat["dir"]) / ".git").is_dir():
        return chat["dir"]
    step(msg, f"Cloning {chat['target']}")
    shutil.rmtree(WORK / chat["id"], ignore_errors=True)  # leftovers of a failed clone
    run(["gh", "repo", "clone", chat["target"], chat["dir"]])
    chat["branch"] = f"agentflow/chat-{chat['id']}"
    run(["git", "checkout", "-b", chat["branch"]], chat["dir"])
    chat["base"] = git_head(chat["dir"])
    return chat["dir"]


def agent_turn(chat, msg, text, past, agents, member=None, track=True):
    member = member or team_of(chat)[0]
    agent = with_model(agents[member["name"]], member["model"] or None)
    d = prepare(chat, msg)
    sessions = chat.setdefault("sessions", {})  # one per agent, so teammates never resume each other's session
    session = sessions.get(member["name"]) if agent.get("resume") else None
    mcp = active_mcp(load_mcp())
    cmd = safe_cmd(build_cmd(agent, text if session else with_history(past, text), session, mcp))
    p = subprocess.Popen(cmd, cwd=d, env=agent_env(agent, mcp), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **TEXT)
    with lock:
        procs.setdefault(chat["id"], []).append(p)
    lines = []
    for line in p.stdout:
        lines.append(line.rstrip("\n"))
        if sid := session_of(lines[-1]):
            sessions[member["name"]] = sid
        if t := readable(lines[-1]):
            step(msg, t)
    code = p.wait()
    msg["text"] = final_answer(lines)
    if msg.get("stopped"):
        msg["status"] = "stopped"
    elif code:
        if session:  # a stale session is a common cause; next message starts fresh with the history
            sessions.pop(member["name"], None)
        msg["status"], msg["error"] = "error", f"{member['name']} exited with code {code}" + (
            "" if msg["text"] else ". Is it installed and signed in?")
    if track:
        update_changes(chat, msg)


PLAN_PROMPT = """You are {me} on a team of AI coding agents sharing one project folder: {team}.
The user asked: {text}

{task}
Don't edit any files yet. Reply with only the plan."""
FIRST_PLAN = ("Before anyone edits files, write a short plan that splits this work between the team. Give each agent "
              "its tasks and the files it owns, so no two agents edit the same file. Follow any roles the user gave.")
REVIEW_PLAN = ("Your teammates drafted this plan:\n\n{plan}\n\nCheck it from your side: is your part clear and doable, "
               "and is anything missing or overlapping? Reply with the final plan, changed where needed.")
WORK_PROMPT = """You are {me} on a team of AI coding agents ({team}) working at the same time in this project folder.
The user asked: {text}

The team's agreed plan:
{plan}

Do only your part of the plan, and don't edit files the plan gives to another agent. When you're done, reply with a
short summary of what you did."""


def team_turn(chat, msg, text, past, agents):
    """The team agrees on a plan in turn (the lead drafts, each teammate revises it), then all build their part at once.
    Agents coordinate only through the plan, which gives each one its own files so they don't overwrite each other."""
    team = team_of(chat)
    names = ", ".join(m["name"] + (f" ({m['role']})" if m["role"] else "") for m in team)
    me = lambda m: m["name"] + (f", in charge of {m['role']}" if m["role"] else "")
    plan = ""
    for i, member in enumerate(team):
        m = msg if i == 0 else new_msg(chat, member["name"])
        m["label"] = "plan review" if plan else "plan"
        task = REVIEW_PLAN.format(plan=plan) if plan else FIRST_PLAN
        prompt = PLAN_PROMPT.format(me=me(member), team=names, text=text, task=task)
        run_part(m, lambda: agent_turn(chat, m, prompt, past, agents, member, track=False))
        if m["status"] != "done":
            return  # stopped or failed: nobody builds on half a plan
        plan = m["text"] or plan
    if not plan:
        raise RuntimeError("the team didn't produce a plan")
    workers = []
    for member in team:
        wm = new_msg(chat, member["name"], member["role"] or "build")
        prompt = WORK_PROMPT.format(me=me(member), team=names, text=text, plan=plan)
        job = lambda wm=wm, member=member, prompt=prompt: agent_turn(chat, wm, prompt, past, agents, member, track=False)
        workers.append(threading.Thread(target=run_part, args=(wm, job), daemon=True))
    for t in workers:
        t.start()
    for t in workers:
        t.join()
    update_changes(chat, wm)


def slash_turn(chat, msg, m, agents):
    """/issue N, /fix N, /review N: the classic one-shot automations, logged into this chat."""
    kind, num, trust = m.group(1), m.group(2), bool(m.group(3))
    lead = team_of(chat)[0]
    agent = with_model(agents[lead["name"]], lead["model"] or None)
    names = {"tests": lambda t: t.splitlines()[0], "diff": lambda t: "Changes staged", "pr": lambda t: f"PR: {t}"}
    try:
        url = resolve(f"{chat['target']}#{num}", agent, None, lambda k, t: step(msg, names.get(k, str)(t)),
                      trust, review=kind == "review")
    except RuntimeError as e:
        raise RuntimeError(f"{e}. Here, send: /{kind} {num} --trust") if "trust-author" in str(e) else e
    msg["text"] = f"{'Posted the review' if kind == 'review' else 'Done'}: {url}"


def update_changes(chat, msg):
    """Everything changed since the chat began (git diff vs its starting commit, plus new untracked files)."""
    if not chat["base"]:
        return
    d = chat["dir"]
    diff = subprocess.run(["git", "diff", chat["base"]], cwd=d, capture_output=True, **TEXT).stdout
    new = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=d,
                         capture_output=True, **TEXT).stdout.splitlines()
    text = (diff[:DIFF_LIMIT] + ("\n... (diff cut short)" if len(diff) > DIFF_LIMIT else "")
            + "".join(f"\nnew file (untracked): {f}" for f in new)).strip()
    if text != chat["diff"]:
        msg["files"] = len(re.findall(r"^diff --git ", diff, re.M)) + len(new)
    chat["diff"] = text


def make_pr(cid):
    with lock:
        chat = get(cid)
        if chat["kind"] != "github" or not chat["base"]:
            raise ValueError("send a message first; PRs are for GitHub repo chats")
        if chat["status"] == "running":
            raise ValueError("wait for the agent to finish first")

    def job(msg):
        msg["agent"] = "agentflow"
        d = chat["dir"]
        run(["git", "add", "-A"], d)
        if run(["git", "diff", "--cached", "--name-only"], d).strip():
            run(["git", "commit", "-m", chat["title"]], d)
        since = "@{u}" if chat["pr"] else chat["base"]  # new commits since the last push, or since the chat began
        ahead = run(["git", "rev-list", "--count", since + "..HEAD"], d).strip()
        if ahead == "0":
            raise RuntimeError("nothing new to send" if chat["pr"] else "no changes yet; ask the agent to change something first")
        test_cmd, passed, tail = detect_test_cmd(d), None, ""
        if test_cmd:
            step(msg, f"Running tests: {test_cmd}")
            t = subprocess.run(test_cmd, cwd=d, shell=True, capture_output=True, **TEXT)
            passed, tail = t.returncode == 0, (t.stdout + t.stderr)[-LOG_LIMIT:]
            step(msg, "Tests passed" if passed else "Tests failed")
        step(msg, "Pushing")
        run(["git", "push", "-u", "origin", chat["branch"]], d)
        note = test_note(passed, test_cmd, tail)
        if chat["pr"]:
            msg["text"] = f"Pushed to {chat['pr']}\n\n{note}"
            return
        last = next((x["text"] for x in reversed(chat["messages"])
                     if x["role"] == "agent" and x.get("agent") != "agentflow" and x.get("text")), "")
        body = f"{last[:LOG_LIMIT]}\n\n{note}\n\n_Made with agentflow._"
        chat["pr"] = run(["gh", "pr", "create", "--title", chat["title"], "--body", body,
                          *(["--draft"] if passed is False else [])], d).strip()
        msg["text"] = f"Opened {'a draft ' if passed is False else 'a '}PR: {chat['pr']}\n\n{note}"

    return start(chat, job)


def stop(cid):
    with lock:
        chat = get(cid)
        running = list(procs.get(cid, []))
        for m in chat["messages"]:
            if m.get("status") == "running":
                m["stopped"] = True
    for p in running:
        if os.name == "nt":  # kill the agent's whole process tree, not just the launcher
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
        else:
            p.kill()
    return chat


def delete(cid):
    with lock:
        chat = get(cid)
        if chat["status"] == "running":
            raise ValueError("stop the agent before deleting this chat")
        del chats[cid]
    if chat["kind"] == "github":  # our own clone only; a local folder is the user's and is never touched
        shutil.rmtree(WORK / cid, ignore_errors=True)
    save_chats()
    return {}


def list_repos():
    out = run(["gh", "repo", "list", "--limit", "200", "--json", "nameWithOwner,description,isPrivate"])
    return json.loads(out)


def browse():
    """Native folder picker (tkinter) in its own process so it never blocks the server. '' if cancelled."""
    code = ("import tkinter as t, tkinter.filedialog as f; r = t.Tk(); r.withdraw(); r.attributes('-topmost', True); "
            "print(f.askdirectory(title='Choose a project folder') or '')")
    return subprocess.run([sys.executable, "-c", code], capture_output=True, **TEXT).stdout.strip()


def state(agents, watching):
    with lock:
        summary = [{k: c[k] for k in ("id", "title", "kind", "target", "agent", "status", "updated", "pr")}
                   for c in sorted(chats.values(), key=lambda c: -c["updated"])]
    return {"chats": summary, "watching": watching, "mcp": load_mcp(),
            "agents": [{"name": n, "installed": bool(exe_path(a["cmd"][0])),
                        "needs_model": "{model}" in " ".join(a["cmd"]),
                        "takes_model": "{model}" in " ".join(a["cmd"]) or bool(a.get("model_args")),
                        "mcp": bool(a.get("mcp") or a.get("mcp_env"))} for n, a in agents.items()]}


# ---------- server ----------

class Handler(BaseHTTPRequestHandler):
    agents, watching = {}, []

    def send(self, code, body, ctype="application/json"):
        with lock:  # chats are mutated by agent threads; serialize a consistent snapshot
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def trusted(self):
        # Host check blocks DNS rebinding; JSON-only POST forces a CORS preflight we never answer (blocks CSRF).
        return self.headers.get("Host") in ALLOWED_HOSTS

    def answer(self, fn):
        try:
            return self.send(200, fn())
        except (ValueError, RuntimeError) as e:
            return self.send(400, {"error": str(e)})
        except subprocess.CalledProcessError as e:
            return self.send(400, {"error": (e.stderr or str(e)).strip()})

    def do_GET(self):
        if not self.trusted():
            return self.send(403, {"error": "bad host"})
        url = urlsplit(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            return self.send(200, PAGE, "text/html")
        routes = {"/api/state": lambda: state(self.agents, self.watching),
                  "/api/chat": lambda: get(q.get("id", [""])[0]),
                  "/api/repos": lambda: {"repos": list_repos()}}
        if url.path in routes:
            return self.answer(routes[url.path])
        self.send(404, {"error": "not found"})

    def do_POST(self):
        if not self.trusted() or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.send(403, {"error": "forbidden"})
        try:
            b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not isinstance(b, dict):
                raise ValueError
        except ValueError:
            return self.send(400, {"error": "bad JSON"})
        routes = {
            "/api/chats": lambda: new_chat(b.get("kind"), (b.get("target") or "").strip(),
                                           b["team"] if "team" in b else [{"name": b.get("agent", "claude"), "model": b.get("model")}],
                                           self.agents, b.get("name", "")),
            "/api/send": lambda: send(b.get("id"), b.get("text"), self.agents),
            "/api/team": lambda: set_team(b.get("id"), b.get("team"), self.agents),
            "/api/stop": lambda: stop(b.get("id")),
            "/api/pr": lambda: make_pr(b.get("id")),
            "/api/delete": lambda: delete(b.get("id")),
            "/api/mcp": lambda: edit_mcp(b),
            "/api/browse": lambda: {"path": browse()},
        }
        if self.path not in routes:
            return self.send(404, {"error": "not found"})
        self.answer(routes[self.path])

    def log_message(self, *args):
        pass  # keep the console quiet; the page shows everything


def app_browser():
    """A Chromium browser that can open a page as its own app window (Edge ships with Windows)."""
    found = [shutil.which(b) for b in ("msedge", "chrome", "chromium", "google-chrome")]
    found += [os.path.join(os.environ.get(v, ""), "Microsoft", "Edge", "Application", "msedge.exe")
              for v in ("ProgramFiles(x86)", "ProgramFiles")]
    return next((f for f in found if f and os.path.isfile(f)), None)


def open_window(url):
    """Show the UI in its own window and return when the window is closed."""
    exe = app_browser()
    if not exe:  # no app-capable browser: use the default one and stop with Ctrl+C
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return
    # Its own profile makes this a separate browser process that lives exactly as long as the window.
    subprocess.run([exe, f"--app={url}", f"--user-data-dir={Path.home() / '.agentflow-app'}",
                    "--no-first-run", "--no-default-browser-check", "--window-size=1320,880"])


def serve(port=PORT, watch_repos=(), agent_name="claude", model=None, test_cmd=None, interval=60, app=False):
    Handler.agents = load_agents()
    Handler.watching = list(watch_repos)
    chats.update(load_chats())
    url = f"http://localhost:{port}"
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        if not app:
            raise
        return open_window(url)  # already running: just open another window onto it
    print(f"agentflow: {url}  (Ctrl+C to stop)")
    if watch_repos:
        def launch(ref, review):  # each labeled issue/PR becomes its own chat
            repo, num = parse_ref(ref)
            chat = new_chat("github", repo, [{"name": agent_name, "model": model or ""}], Handler.agents)
            send(chat["id"], f"/{'review' if review else 'issue'} {num} --trust", Handler.agents, notify=ref)
        threading.Thread(target=watch, args=(watch_repos, launch, interval), daemon=True).start()
    if app:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        open_window(url)
        while any(c["status"] == "running" for c in list(chats.values())):  # closing the window must not kill a push
            time.sleep(2)
        return
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>agentflow</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Cdefs%3E%3ClinearGradient id='g' x1='0' y1='0' x2='1' y2='1'%3E%3Cstop offset='0' stop-color='%230a84ff'/%3E%3Cstop offset='1' stop-color='%235e5ce6'/%3E%3C/linearGradient%3E%3C/defs%3E%3Crect width='32' height='32' rx='8' fill='url(%23g)'/%3E%3Cpath d='M9 22l7-12 7 12' fill='none' stroke='white' stroke-width='3.2' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
:root{color-scheme:dark;--bg:#000;--side:#0b0b0c;--panel:#141415;--elev:#1c1c1e;--elev2:#2c2c2e;--elev3:#3a3a3c;
--line:rgba(255,255,255,.07);--line2:rgba(255,255,255,.13);--text:#f5f5f7;--muted:#98989d;--faint:#6e6e73;
--accent:#0a84ff;--accent2:#409cff;--green:#30d158;--red:#ff453a;--orange:#ff9f0a;
--font:-apple-system,BlinkMacSystemFont,"SF Pro Text","SF Pro Display","Inter","Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;
--mono:"SF Mono",ui-monospace,"Cascadia Code",Menlo,Consolas,monospace}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--text);font:15px/1.55 var(--font);-webkit-font-smoothing:antialiased;overflow:hidden}
button,input,textarea{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none;padding:0}
button:disabled{cursor:default}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:8px}
svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;flex:none}
::-webkit-scrollbar{width:10px;height:10px}::-webkit-scrollbar-thumb{background:#2a2a2c;border-radius:10px;border:3px solid transparent;background-clip:padding-box}
#app{display:flex;height:100vh}

#side{width:270px;flex:none;background:var(--side);border-right:1px solid var(--line);display:flex;flex-direction:column;padding:14px 10px 10px}
.side-top{display:flex;align-items:center;justify-content:space-between;padding:2px 4px 14px 8px}
.brand{font-weight:600;letter-spacing:-.01em;display:flex;align-items:center;gap:9px}
.logo{width:22px;height:22px;border-radius:7px;background:linear-gradient(135deg,#0a84ff,#5e5ce6);display:grid;place-items:center}
.logo svg{width:13px;height:13px;stroke:#fff;stroke-width:3}
.icon-btn{width:32px;height:32px;border-radius:9px;display:grid;place-items:center;color:var(--muted);transition:background .12s,color .12s}
.icon-btn:hover{background:var(--elev);color:var(--text)}
#chatList{flex:1;overflow-y:auto;margin:0 -4px;padding:0 4px}
.group{font-size:11.5px;color:var(--faint);font-weight:600;padding:14px 10px 5px}
.chat-row{position:relative}
.chat-item{display:flex;align-items:center;gap:9px;padding:8px 34px 8px 10px;border-radius:10px;color:var(--muted);text-align:left;width:100%;transition:background .12s}
.chat-item:hover{background:var(--elev)}
.chat-row.on .chat-item{background:var(--elev2);color:var(--text)}
.chat-item svg{width:15px;height:15px;color:var(--faint)}
.chat-item .t{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:14px}
.dot{width:7px;height:7px;border-radius:50%;background:var(--accent);box-shadow:0 0 0 3px rgba(10,132,255,.22);animation:pulse 1.4s ease-in-out infinite;flex:none}
.chat-row .del{position:absolute;right:4px;top:4px;width:28px;height:28px;opacity:0}
.chat-row:hover .del,.chat-row .del:focus-visible{opacity:1}
.side-bottom{border-top:1px solid var(--line);padding-top:8px;margin-top:6px}
.watching{font-size:12px;color:var(--green);padding:6px 10px;display:flex;gap:7px;align-items:center}
.watching .dot{background:var(--green);box-shadow:0 0 0 3px rgba(48,209,88,.2)}
.side-link{display:flex;gap:10px;align-items:center;padding:9px 10px;border-radius:10px;color:var(--muted);width:100%;font-size:14px}
.side-link:hover{background:var(--elev);color:var(--text)}

main{flex:1;min-width:0;display:flex;flex-direction:column;position:relative}
#top{height:54px;flex:none;display:flex;align-items:center;gap:10px;padding:0 16px 0 20px;border-bottom:1px solid var(--line)}
#title{font-weight:600;font-size:14.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.chip{display:inline-flex;align-items:center;gap:7px;height:30px;padding:0 12px;border-radius:999px;background:var(--elev);border:1px solid var(--line);color:var(--muted);font-size:13px;white-space:nowrap;max-width:280px;transition:background .12s,color .12s}
.chip span{overflow:hidden;text-overflow:ellipsis}
button.chip:hover{color:var(--text);background:var(--elev2)}
.chip svg{width:15px;height:15px}
.spacer{flex:1}
.btn{display:inline-flex;align-items:center;gap:6px;height:32px;padding:0 15px;border-radius:999px;font-size:13px;font-weight:500;background:var(--elev2);color:var(--text);text-decoration:none;white-space:nowrap;transition:background .12s}
.btn:hover{background:var(--elev3)}
.btn.primary{background:var(--accent);color:#fff}.btn.primary:hover{background:var(--accent2)}
.btn:disabled{opacity:.4}
.btn svg{width:15px;height:15px}
#thread{flex:1;overflow-y:auto}
.col{max-width:800px;margin:0 auto;padding:32px 28px 28px}

.msg{margin:0 0 30px;animation:rise .2s ease-out}
.user{display:flex;justify-content:flex-end}
.bubble{background:var(--elev2);border-radius:22px;padding:10px 17px;max-width:78%;white-space:pre-wrap;overflow-wrap:anywhere}
.who{font-size:12.5px;color:var(--faint);margin-bottom:7px;display:flex;align-items:center;gap:7px;font-weight:500}
.who .logo{width:18px;height:18px;border-radius:6px}.who .logo svg{width:10px;height:10px}
.md{overflow-wrap:anywhere}
.md p{margin:0 0 12px}.md>:last-child{margin-bottom:0}
.md h4{margin:20px 0 8px;font-size:15.5px;font-weight:600}
.md ul,.md ol{margin:0 0 12px;padding-left:22px}.md li{margin:4px 0}
.md code{font:13px var(--mono);background:var(--elev);border:1px solid var(--line);padding:1px 6px;border-radius:6px}
.md pre{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 16px;overflow:auto;margin:0 0 12px}
.md pre code{background:none;border:0;padding:0;font-size:12.5px;line-height:1.6}
.md a{color:var(--accent2);text-decoration:none}.md a:hover{text-decoration:underline}
.working{display:inline-block;font-size:14px;font-weight:500;background:linear-gradient(90deg,var(--faint) 0%,var(--text) 45%,var(--faint) 90%);background-size:200% 100%;-webkit-background-clip:text;background-clip:text;color:transparent;animation:shimmer 1.8s linear infinite}
.log{font:12.5px/1.65 var(--mono);color:var(--muted);border-left:2px solid var(--elev3);padding:2px 0 2px 14px;margin-top:8px;white-space:pre-wrap;overflow-wrap:anywhere}
.live{max-height:230px;overflow:hidden;display:flex;flex-direction:column;justify-content:flex-end;-webkit-mask-image:linear-gradient(to bottom,transparent,#000 50px);mask-image:linear-gradient(to bottom,transparent,#000 50px)}
details.steps{margin-top:12px}
details.steps summary{cursor:pointer;color:var(--faint);font-size:12.5px;list-style:none;display:inline-flex;gap:5px;align-items:center;user-select:none}
details.steps summary::-webkit-details-marker{display:none}
details.steps summary svg{width:13px;height:13px;transition:transform .15s}
details.steps[open] summary svg{transform:rotate(90deg)}
details.steps summary:hover{color:var(--muted)}
.error{color:var(--red);font-size:13.5px;margin-top:8px;white-space:pre-wrap}
.note{color:var(--faint);font-size:13.5px}
.pill{margin-top:12px;display:inline-flex;align-items:center;gap:8px;padding:7px 13px;border-radius:12px;background:var(--elev);border:1px solid var(--line);font-size:13px;color:var(--muted);transition:background .12s,color .12s}
.pill:hover{color:var(--text);background:var(--elev2)}.pill svg{width:15px;height:15px}

#composerWrap{flex:none;padding:0 28px 18px}
#composer{max-width:800px;margin:0 auto;background:var(--elev);border:1px solid var(--line2);border-radius:26px;padding:14px 12px 10px 18px;box-shadow:0 12px 48px rgba(0,0,0,.55);transition:border-color .15s,box-shadow .15s}
#composer:focus-within{border-color:rgba(10,132,255,.45);box-shadow:0 12px 48px rgba(0,0,0,.55),0 0 0 4px rgba(10,132,255,.08)}
#input{width:100%;border:0;outline:0;background:none;resize:none;max-height:260px;min-height:26px;padding:0 4px 0 0;font-size:15.5px;line-height:1.55;display:block}
#input::placeholder{color:var(--faint)}
.row{display:flex;align-items:center;gap:8px;margin-top:10px}
#send{width:34px;height:34px;border-radius:50%;background:var(--text);color:#000;display:grid;place-items:center;flex:none;transition:opacity .15s,transform .1s}
#send:disabled{opacity:.22}
#send:not(:disabled):active{transform:scale(.92)}
#send svg{stroke-width:2.5;width:17px;height:17px}
.hint{text-align:center;font-size:11.5px;color:var(--faint);margin-top:10px}

#welcome{display:none}
main.empty #top,main.empty #thread{display:none}
main.empty #welcome{display:flex;flex:1;flex-direction:column;justify-content:flex-end;align-items:center;padding:0 28px 30px;text-align:center}
main.empty #composerWrap{flex:1}
#welcome h1{font-size:36px;font-weight:600;letter-spacing:-.03em;margin:0 0 8px;background:linear-gradient(180deg,#fff 30%,#8e8e93);-webkit-background-clip:text;background-clip:text;color:transparent}
#welcome p{color:var(--muted);margin:0}
.suggest{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;max-width:800px;margin:0 auto 14px}

.pop{position:fixed;z-index:20;width:360px;max-height:calc(100% - 24px);overflow-y:auto;max-width:calc(100% - 16px);background:rgba(28,28,30,.88);-webkit-backdrop-filter:blur(40px) saturate(180%);backdrop-filter:blur(40px) saturate(180%);border:1px solid var(--line2);border-radius:16px;box-shadow:0 24px 70px rgba(0,0,0,.65);padding:8px;display:none}
.pop.open{display:block;animation:pop .14s ease-out}
.seg{display:flex;background:rgba(118,118,128,.18);border-radius:10px;padding:2px;margin:2px 2px 8px}
.seg button{flex:1;height:28px;border-radius:8px;font-size:13px;color:var(--muted);font-weight:500}
.seg button.on{background:#636366;color:var(--text);box-shadow:0 2px 6px rgba(0,0,0,.3)}
.field{width:100%;height:36px;border-radius:10px;background:rgba(0,0,0,.35);border:1px solid var(--line2);padding:0 12px;outline:0;font-size:14px}
.field:focus{border-color:var(--accent)}
.field:disabled{opacity:.5}
.list{max-height:290px;overflow-y:auto;margin-top:6px}
.opt{display:flex;align-items:center;gap:10px;width:100%;text-align:left;padding:8px 10px;border-radius:9px;font-size:14px;min-width:0}
.opt span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.opt:hover,.opt.on{background:rgba(255,255,255,.08)}
.opt small{color:var(--faint);margin-left:auto;font-size:12px;flex:none}
.opt.off{color:var(--faint)}
.opt svg{width:15px;height:15px;color:var(--muted)}
.pop-label{font-size:11.5px;color:var(--faint);font-weight:600;padding:8px 10px 4px}
.pop .btn{width:100%;justify-content:center;height:36px;border-radius:10px}
.member{display:flex;gap:6px;padding:0 8px 8px 35px}.member .field{height:30px;font-size:13px;padding:0 10px}
.pop-note{font-size:12px;color:var(--faint);padding:8px 10px 4px;line-height:1.45}
.lbl{color:var(--muted);background:var(--elev);border:1px solid var(--line);border-radius:6px;padding:0 7px;font-size:11.5px}

#drawer{position:fixed;top:0;right:0;bottom:0;width:min(760px,100%);background:var(--panel);border-left:1px solid var(--line2);transform:translateX(100%);transition:transform .28s cubic-bezier(.2,.8,.2,1);z-index:15;display:flex;flex-direction:column}
#drawer.open{transform:none;box-shadow:-30px 0 90px rgba(0,0,0,.7)}
.drawer-head{height:54px;flex:none;display:flex;align-items:center;padding:0 12px 0 20px;border-bottom:1px solid var(--line);font-weight:600;font-size:14.5px}
#diff{flex:1;overflow:auto;margin:0;padding:10px 0 30px;font:12.5px/1.6 var(--mono)}
#diff div{padding:0 20px;white-space:pre}
#diff .a{background:rgba(48,209,88,.11);color:#86efac}#diff .d{background:rgba(255,69,58,.11);color:#fca5a5}
#diff .h{color:var(--text);font-weight:600;margin-top:16px;padding-top:8px;padding-bottom:4px;border-top:1px solid var(--line);font-family:var(--font)}
#diff .m{color:var(--faint)}

dialog{background:var(--panel);color:var(--text);border:1px solid var(--line2);border-radius:22px;padding:0;width:min(660px,calc(100% - 32px));max-height:calc(100% - 64px);box-shadow:0 40px 120px rgba(0,0,0,.8);overflow:hidden}
dialog[open]{display:flex;flex-direction:column;animation:pop .18s ease-out}
dialog::backdrop{background:rgba(0,0,0,.6);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px)}
.dlg-head{display:flex;align-items:center;gap:12px;padding:16px 14px 6px 22px}
.dlg-head h2{font-size:18px;margin:0;font-weight:600;letter-spacing:-.01em}
.dlg-head .seg{margin:0 auto;width:250px}
.dlg-body{padding:10px 22px 22px;overflow-y:auto}
.card{background:var(--elev);border-radius:14px;border:1px solid var(--line);overflow:hidden}
.card-row{display:flex;align-items:center;gap:12px;padding:12px 14px;border-top:1px solid var(--line);min-width:0}
.card-row:first-child{border-top:0}
.card-row .grow{flex:1;min-width:0}
.card-row .name{font-weight:500;font-size:14px}
.card-row .sub{color:var(--faint);font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.card-row code{font:12px var(--mono);color:var(--muted);user-select:all}
.status{font-size:12px;padding:3px 10px;border-radius:999px;background:rgba(48,209,88,.14);color:var(--green);white-space:nowrap;font-weight:500}
.status.no{background:var(--elev2);color:var(--faint)}
.switch{appearance:none;-webkit-appearance:none;width:44px;height:26px;border-radius:13px;background:var(--elev3);position:relative;cursor:pointer;flex:none;transition:background .2s;margin:0}
.switch::after{content:"";position:absolute;top:2px;left:2px;width:22px;height:22px;border-radius:50%;background:#fff;transition:transform .2s;box-shadow:0 2px 5px rgba(0,0,0,.35)}
.switch:checked{background:var(--green)}.switch:checked::after{transform:translateX(18px)}
.drop{margin-top:14px;border:1.5px dashed var(--line2);border-radius:14px;padding:12px 14px;transition:border-color .15s,background .15s}
.drop.over{border-color:var(--accent);background:rgba(10,132,255,.07)}
.drop textarea{width:100%;min-height:120px;background:none;border:0;outline:0;resize:vertical;font:12.5px/1.55 var(--mono);color:var(--text)}
.drop textarea::placeholder{color:var(--faint)}
.drop .row{margin-top:6px}
.muted{color:var(--muted);font-size:13px;margin:12px 2px 0}
.empty-card{padding:22px;text-align:center;color:var(--faint);font-size:13.5px}
.toast{position:fixed;bottom:26px;left:50%;transform:translateX(-50%);background:rgba(44,44,46,.95);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border:1px solid var(--line2);padding:10px 18px;border-radius:14px;font-size:13.5px;z-index:40;box-shadow:0 12px 40px rgba(0,0,0,.6);display:none;max-width:min(560px,90%)}
#menuBtn{display:none}
@keyframes shimmer{to{background-position:-200% 0}}
@keyframes pulse{50%{opacity:.35}}
@keyframes pop{from{opacity:0;transform:translateY(4px) scale(.985)}}
@keyframes rise{from{opacity:0;transform:translateY(4px)}}
@media (max-width:760px){#side{position:fixed;inset:0 auto 0 0;z-index:25;transform:translateX(-100%);transition:transform .2s}#side.open{transform:none;box-shadow:20px 0 60px #000}#menuBtn{display:grid;position:fixed;top:11px;left:10px;z-index:5}#top{padding-left:52px}.col{padding:22px 16px}#composerWrap{padding:0 12px 12px}main.empty #welcome{padding-top:60px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
</style></head>
<body>
<div id="app">
<aside id="side" aria-label="Chats">
  <div class="side-top">
    <span class="brand"><span class="logo"><svg viewBox="0 0 24 24"><path d="M6 17l6-10 6 10"/></svg></span>agentflow</span>
    <button class="icon-btn" id="newBtn" title="New chat" aria-label="New chat"><svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg></button>
  </div>
  <nav id="chatList"></nav>
  <div class="side-bottom">
    <div class="watching" id="watching" hidden></div>
    <button class="side-link" id="settingsBtn"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>Settings</button>
  </div>
</aside>
<main id="main" class="empty">
  <button class="icon-btn" id="menuBtn" aria-label="Show chats"><svg viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button>
  <header id="top">
    <div id="title"></div><span class="chip" id="projChip"></span>
    <div class="spacer"></div>
    <button class="btn" id="changesBtn" hidden><svg viewBox="0 0 24 24"><path d="M12 3v18M3 12h18"/></svg>Changes</button>
    <a class="btn" id="prLink" target="_blank" rel="noopener" hidden>View PR</a>
    <button class="btn primary" id="prBtn" hidden></button>
  </header>
  <section id="thread"><div class="col" id="msgs"></div></section>
  <div id="welcome"><h1>What should we build?</h1><p>Start a new project, or pick a GitHub repo or a folder. Choose one agent, or a few to work as a team.</p></div>
  <div id="composerWrap">
    <div class="suggest" id="suggest"></div>
    <form id="composer">
      <textarea id="input" rows="1" placeholder="Ask anything, or describe a change…" aria-label="Message"></textarea>
      <div class="row">
        <button type="button" class="chip" id="projBtn"></button>
        <button type="button" class="chip" id="agentBtn"></button>
        <div class="spacer"></div>
        <button type="submit" id="send" aria-label="Send" disabled></button>
      </div>
    </form>
    <div class="hint" id="hint">Enter to send · Shift+Enter for a new line</div>
  </div>
</main>
</div>
<div class="pop" id="projPop" role="dialog" aria-label="Choose project"></div>
<div class="pop" id="agentPop" role="dialog" aria-label="Choose agent"></div>
<aside id="drawer" aria-label="Changes">
  <div class="drawer-head"><span id="diffTitle">Changes</span><div class="spacer"></div><button class="icon-btn" id="closeDiff" aria-label="Close"><svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>
  <div id="diff"></div>
</aside>
<dialog id="settings" aria-label="Settings">
  <div class="dlg-head"><h2>Settings</h2><div class="seg" id="setSeg"><button data-tab="agents">Agents</button><button data-tab="mcp">MCP servers</button></div>
    <button class="icon-btn" id="closeSettings" aria-label="Close"><svg viewBox="0 0 24 24"><path d="M18 6 6 18M6 6l12 12"/></svg></button></div>
  <div class="dlg-body" id="setBody"></div>
</dialog>
<div class="toast" id="toast" role="status"></div>
<script>
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const I = {
  github:'<svg viewBox="0 0 24 24"><path d="M9 19c-4.3 1.4-4.3-2.5-6-3m12 5v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.6 4.6 0 0 0-1.3-3.2 4.2 4.2 0 0 0-.1-3.2s-1.1-.3-3.5 1.3a12.3 12.3 0 0 0-6.2 0C6.5 2.8 5.4 3.1 5.4 3.1a4.2 4.2 0 0 0-.1 3.2A4.6 4.6 0 0 0 4 9.5c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V21"/></svg>',
  folder:'<svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  spark:'<svg viewBox="0 0 24 24"><path d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8z"/><path d="M19 17l.7 1.8 1.8.7-1.8.7L19 22l-.7-1.8-1.8-.7 1.8-.7z"/></svg>',
  up:'<svg viewBox="0 0 24 24"><path d="M12 19V5M5 12l7-7 7 7"/></svg>',
  stop:'<svg viewBox="0 0 24 24"><rect x="7" y="7" width="10" height="10" rx="2.5" fill="currentColor" stroke="none"/></svg>',
  chev:'<svg viewBox="0 0 24 24"><path d="m9 6 6 6-6 6"/></svg>',
  trash:'<svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4h6v3"/></svg>',
  file:'<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
  pr:'<svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M6 8.5v7M18 15.5V9a3 3 0 0 0-3-3h-4m2-2-2 2 2 2"/></svg>',
  plus:'<svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg>',
  check:'<svg viewBox="0 0 24 24" style="color:var(--accent)"><path d="m5 12 5 5 9-10"/></svg>',
  logo:'<span class="logo"><svg viewBox="0 0 24 24"><path d="M6 17l6-10 6 10"/></svg></span>'
};
const ABOUT = {claude:'Claude Code · your Claude plan', codex:'OpenAI Codex · your ChatGPT plan', ollama:'Local models through Ollama · free, offline',
  cursor:'Cursor Agent · your Cursor plan', gemini:'Gemini CLI · your Google account', qwen:'Qwen Code · your Qwen account',
  aider:'Aider · any model you set up', opencode:'OpenCode · any provider', copilot:'GitHub Copilot CLI · your Copilot plan'};
const INSTALL = {claude:'npm i -g @anthropic-ai/claude-code', codex:'npm i -g @openai/codex', ollama:'install Claude Code + ollama.com',
  cursor:"irm 'https://cursor.com/install?win32=true' | iex", gemini:'npm i -g @google/gemini-cli', qwen:'npm i -g @qwen-code/qwen-code',
  aider:'python -m pip install aider-install && aider-install', opencode:'npm i -g opencode-ai', copilot:'npm i -g @github/copilot'};
const SUGGEST = ['Explain how this project is organized', 'Find a bug and fix it', 'Add tests for the main module', 'Review the code for security issues'];

const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem('af.' + k); localStorage.setItem('af.' + k, v); } catch (e) { return null; } };
let S = {chats: [], agents: [], mcp: {}, watching: []}, cur = null, sig = '', repos = null, projTab = 'github', setTab = 'agents', stick = true;
const openSteps = new Set();
let pending = {project: null, team: null};
try { pending.project = JSON.parse(ls('project') || 'null'); pending.team = JSON.parse(ls('team') || 'null'); } catch (e) {}
pending.team = pending.team || [{name: ls('agent') || 'claude', model: ls('model') || '', role: ''}];
const teamOf = () => cur ? (cur.team || [{name: cur.agent, model: cur.model || '', role: ''}]) : pending.team;

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
  const j = await r.json().catch(() => ({error: r.statusText}));
  if (!r.ok) throw new Error(j.error || 'Something went wrong');
  return j;
}
function toast(t) { const el = $('#toast'); el.textContent = t; el.style.display = 'block'; clearTimeout(toast.t); toast.t = setTimeout(() => el.style.display = 'none', 5000); }
const base = p => p.split(/[\\/]/).filter(Boolean).pop() || p;
const projLabel = (kind, target) => (kind === 'github' ? I.github : I.folder) + `<span>${esc(kind === 'github' ? target : base(target))}</span>`;
const agentInfo = name => S.agents.find(a => a.name === name) || {};

// ---------- markdown (escaped first, so agent output can never inject HTML) ----------
function inline(s) {
  return esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
}
function blocks(t) {
  let out = '', list = null, para = [];
  const flush = () => {
    if (para.length) { out += `<p>${para.map(inline).join('<br>')}</p>`; para = []; }
    if (list) { out += `<${list.tag}>${list.items.map(x => `<li>${inline(x)}</li>`).join('')}</${list.tag}>`; list = null; }
  };
  for (const line of t.split('\n')) {
    let m;
    if (!line.trim()) { flush(); continue; }
    if ((m = line.match(/^#{1,6}\s+(.*)/))) { flush(); out += `<h4>${inline(m[1])}</h4>`; continue; }
    if ((m = line.match(/^\s*([-*•]|\d+[.)])\s+(.*)/))) {
      const tag = /\d/.test(m[1]) ? 'ol' : 'ul';
      if (para.length || (list && list.tag !== tag)) flush();
      list = list || {tag, items: []}; list.items.push(m[2]); continue;
    }
    if (list) flush();
    para.push(line);
  }
  flush(); return out;
}
const md = src => String(src).split(/```[^\n]*\n?([\s\S]*?)(?:```|$)/g)
  .map((p, i) => i % 2 ? `<pre><code>${esc(p.replace(/\n$/, ''))}</code></pre>` : blocks(p)).join('');

// ---------- rendering ----------
function renderList() {
  const now = Date.now() / 1000, used = new Set();
  const groups = [['Today', c => now - c.updated < 86400], ['Previous 7 days', c => now - c.updated < 7 * 86400], ['Older', () => true]];
  let html = '';
  for (const [name, test] of groups) {
    const items = S.chats.filter(c => !used.has(c.id) && test(c));
    if (!items.length) continue;
    items.forEach(c => used.add(c.id));
    html += `<div class="group">${name}</div>` + items.map(c => `<div class="chat-row${cur && cur.id === c.id ? ' on' : ''}">
      <button class="chat-item" data-open="${c.id}" title="${esc(c.target)}">${c.kind === 'github' ? I.github : I.folder}<span class="t">${esc(c.title)}</span>${c.status === 'running' ? '<span class="dot"></span>' : ''}</button>
      <button class="icon-btn del" data-del="${c.id}" aria-label="Delete chat" title="Delete">${I.trash}</button></div>`).join('');
  }
  $('#chatList').innerHTML = html || '<div class="group" style="font-weight:400">Your chats will show up here.</div>';
  $('#watching').hidden = !S.watching.length;
  $('#watching').innerHTML = `<span class="dot"></span>Watching ${esc(S.watching.join(', '))}`;
}

function renderComposer() {
  const running = !!cur && cur.status === 'running';
  const proj = cur ? {kind: cur.kind, target: cur.target} : pending.project;
  $('#projBtn').hidden = !!cur;
  $('#projBtn').innerHTML = proj ? projLabel(proj.kind, proj.target) : I.plus + '<span>New project</span>';
  $('#agentBtn').innerHTML = I.spark + `<span>${teamOf().map(m => esc(m.name) + (m.role ? ' · ' + esc(m.role) : m.model ? ' · ' + esc(m.model) : '')).join(' + ')}</span>`;
  const send = $('#send');
  send.innerHTML = running ? I.stop : I.up;
  send.setAttribute('aria-label', running ? 'Stop' : 'Send');
  send.disabled = !running && !$('#input').value.trim();
  $('#input').placeholder = cur ? 'Reply…' : 'Ask anything, or describe a change…';
  $('#suggest').innerHTML = cur ? '' : SUGGEST.map(s => `<button type="button" class="chip" data-suggest="${esc(s)}"><span>${esc(s)}</span></button>`).join('');
  $('#hint').textContent = cur && cur.kind === 'github' ? 'Enter to send · /issue 12, /fix 13, /review 13 run the GitHub automations' : 'Enter to send · Shift+Enter for a new line';
}

function msgHtml(m, i) {
  if (m.role === 'user') return `<div class="msg user"><div class="bubble">${esc(m.text)}</div></div>`;
  const steps = m.steps || [], tools = steps.filter(s => s.startsWith('→')).length;
  let h = `<div class="msg"><div class="who">${m.agent === 'agentflow' ? I.logo : I.spark}${esc(m.agent)}${m.label ? `<span class="lbl">${esc(m.label)}</span>` : ''}</div>`;
  if (m.status === 'running') {
    h += `<div class="working">${m.stopped ? 'Stopping…' : steps.length ? 'Working…' : 'Thinking…'}</div>`;
    if (steps.length) h += `<div class="log live">${esc(steps.slice(-16).join('\n'))}</div>`;
    return h + '</div>';
  }
  if (m.text) h += `<div class="md">${md(m.text)}</div>`;
  else if (!m.error && m.status !== 'stopped') h += '<div class="note">No reply.</div>';
  if (m.status === 'stopped') h += '<div class="note">Stopped.</div>';
  if (m.error) h += `<div class="error">${esc(m.error)}</div>`;
  if (steps.length) h += `<details class="steps" data-i="${i}"${openSteps.has(cur.id + i) ? ' open' : ''}><summary>${I.chev}${tools ? `Used ${tools} tool${tools > 1 ? 's' : ''}` : 'Show log'}</summary><div class="log">${esc(steps.join('\n'))}</div></details>`;
  if (m.files) h += `<div><button class="pill" data-diff>${I.file}${m.files} file${m.files > 1 ? 's' : ''} changed · View</button></div>`;
  return h + '</div>';
}

function render() {
  $('#main').classList.toggle('empty', !cur);
  renderList(); renderComposer();
  if (!cur) { sig = ''; return; }
  $('#title').textContent = cur.title;
  $('#projChip').innerHTML = projLabel(cur.kind, cur.target);
  $('#projChip').title = cur.target;
  $('#changesBtn').hidden = !cur.diff;
  $('#prLink').hidden = !cur.pr; $('#prLink').href = cur.pr || '#';
  const pr = $('#prBtn');
  pr.hidden = cur.kind !== 'github' || !cur.base;
  pr.innerHTML = I.pr + (cur.pr ? 'Push changes' : 'Create PR');
  pr.disabled = cur.status === 'running';
  const s = JSON.stringify([cur.id, cur.messages.length, cur.messages.at(-1), cur.status]);
  if (s === sig) return;
  sig = s;
  const t = $('#thread'), atBottom = t.scrollHeight - t.scrollTop - t.clientHeight < 120;
  $('#msgs').innerHTML = cur.messages.map(msgHtml).join('') ||
    `<div class="note" style="text-align:center;padding-top:20vh">${esc(cur.kind === 'github' ? 'Ask about ' + cur.target + ', or tell the agent what to change.' : 'Ask about ' + base(cur.target) + ', or tell the agent what to change.')}</div>`;
  if (atBottom || stick) t.scrollTop = t.scrollHeight;
  stick = false;
}

// ---------- data ----------
async function refreshState() {
  try { S = await api('/api/state'); } catch (e) { return; }
  renderList();
  const s = cur && S.chats.find(c => c.id === cur.id);
  if (s && (s.status !== cur.status || s.updated !== cur.updated || s.pr !== cur.pr)) poll();
  if ($('#settings').open && setTab === 'agents') fillSettings();
}
async function poll() {
  clearTimeout(poll.t);
  if (!cur) return;
  try { cur = await api('/api/chat?id=' + cur.id); render(); } catch (e) { cur = null; render(); return; }
  if (cur.status === 'running') poll.t = setTimeout(poll, 600);
}
async function openChat(id) {
  closePops(); $('#side').classList.remove('open');
  try { cur = await api('/api/chat?id=' + id); } catch (e) { return toast(e.message); }
  ls('chat', id); stick = true; sig = ''; render(); poll(); $('#input').focus();
}
function newChat() { cur = null; ls('chat', ''); closePops(); $('#side').classList.remove('open'); render(); $('#input').focus(); }

// ---------- composer ----------
const input = $('#input');
function grow() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight, 260) + 'px'; renderComposer(); }
input.addEventListener('input', grow);
input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); if (!(cur && cur.status === 'running')) $('#composer').requestSubmit(); } });
$('#composer').addEventListener('submit', async e => {
  e.preventDefault();
  if (cur && cur.status === 'running') { try { await api('/api/stop', {id: cur.id}); } catch (err) { toast(err.message); } return poll(); }
  const text = input.value.trim();
  if (!text) return;
  try {
    if (!cur) {
      cur = await api('/api/chats', pending.project ? {...pending.project, team: pending.team} : {kind: 'new', name: text, team: pending.team});
      ls('chat', cur.id);
    }
    input.value = ''; grow();
    cur = await api('/api/send', {id: cur.id, text});
    stick = true; sig = ''; render(); poll(); refreshState();
  } catch (err) { toast(err.message); }
});
$('#suggest').addEventListener('click', e => { const b = e.target.closest('[data-suggest]'); if (b) { input.value = b.dataset.suggest; grow(); input.focus(); } });

// ---------- popovers ----------
function closePops() { document.querySelectorAll('.pop.open').forEach(p => p.classList.remove('open')); }
function openPop(which) {
  const pop = $('#' + which + 'Pop'), btn = $('#' + which + 'Btn');
  if (pop.classList.contains('open')) return closePops();
  closePops();
  which === 'proj' ? fillProj() : fillAgent();
  pop.classList.add('open');
  const r = btn.getBoundingClientRect(), above = r.top > 340;  // anchor the near edge so a list that loads later grows away from the button
  pop.style.top = above ? 'auto' : r.bottom + 10 + 'px';
  pop.style.bottom = above ? innerHeight - r.top + 10 + 'px' : 'auto';
  pop.style.left = Math.max(8, Math.min(r.left, innerWidth - pop.offsetWidth - 8)) + 'px';
}
$('#projBtn').onclick = () => openPop('proj');
$('#agentBtn').onclick = () => openPop('agent');
document.addEventListener('mousedown', e => { if (!e.target.closest('.pop, #projBtn, #agentBtn')) closePops(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') { closePops(); $('#drawer').classList.remove('open'); } });

function chooseProject(p) { pending.project = p; ls('project', JSON.stringify(p)); closePops(); renderComposer(); input.focus(); }
function fillProj() {
  const seen = new Set(), recent = S.chats.filter(c => !seen.has(c.kind + c.target) && seen.add(c.kind + c.target)).slice(0, 4);
  let h = `<button class="opt${pending.project ? '' : ' on'}" data-new>${I.plus}<span>New project · start from scratch</span>${pending.project ? '' : '<small>✓</small>'}</button>
    <div class="seg" style="margin-top:6px"><button data-tab="github" class="${projTab === 'github' ? 'on' : ''}">GitHub repo</button><button data-tab="folder" class="${projTab === 'folder' ? 'on' : ''}">Folder on this PC</button></div>`;
  if (projTab === 'github') {
    h += `<input class="field" id="repoQ" placeholder="Search your repos, or type owner/repo" autocomplete="off"><div class="list" id="repoList"></div>`;
  } else {
    h += `<button class="btn primary" id="browseBtn">${I.folder}Choose a folder…</button>
      <div class="pop-label">Or paste a path</div><div style="display:flex;gap:6px;padding:0 2px"><input class="field" id="pathIn" placeholder="C:\\code\\my-app"><button class="btn" id="pathGo" style="width:auto">Use</button></div>`;
  }
  if (recent.length) h += '<div class="pop-label">Recent</div>' + recent.map(c => `<button class="opt" data-kind="${c.kind}" data-target="${esc(c.target)}">${c.kind === 'github' ? I.github : I.folder}<span>${esc(c.target)}</span></button>`).join('');
  $('#projPop').innerHTML = h;
  if (projTab === 'github') { renderRepos(); $('#repoQ').focus(); if (!repos) loadRepos(); }
}
async function loadRepos() {
  try { repos = (await api('/api/repos')).repos; } catch (e) { repos = []; toast('Couldn\'t list your repos: ' + e.message); }
  if ($('#repoList')) renderRepos();
}
function renderRepos() {
  const q = ($('#repoQ')?.value || '').trim().toLowerCase();
  let items = (repos || []).filter(r => r.nameWithOwner.toLowerCase().includes(q)).slice(0, 60)
    .map(r => `<button class="opt" data-kind="github" data-target="${esc(r.nameWithOwner)}">${I.github}<span>${esc(r.nameWithOwner)}</span>${r.isPrivate ? '<small>private</small>' : ''}</button>`).join('');
  if (/^[\w.-]+\/[\w.-]+$/.test(q) && !(repos || []).some(r => r.nameWithOwner.toLowerCase() === q))
    items = `<button class="opt" data-kind="github" data-target="${esc($('#repoQ').value.trim())}">${I.github}<span>Use ${esc($('#repoQ').value.trim())}</span></button>` + items;
  $('#repoList').innerHTML = items || `<div class="pop-label" style="font-weight:400">${repos ? 'No matching repos' : 'Loading your repos…'}</div>`;
}
$('#projPop').addEventListener('input', e => { if (e.target.id === 'repoQ') renderRepos(); });
$('#projPop').addEventListener('keydown', e => {
  if (e.key !== 'Enter') return;
  if (e.target.id === 'repoQ') { const first = $('#repoList .opt'); if (first) first.click(); }
  if (e.target.id === 'pathIn') $('#pathGo').click();
});
$('#projPop').addEventListener('click', async e => {
  const t = e.target.closest('button'); if (!t) return;
  if (t.dataset.tab) { projTab = t.dataset.tab; return fillProj(); }
  if (t.dataset.kind) return chooseProject({kind: t.dataset.kind, target: t.dataset.target});
  if ('new' in t.dataset) return chooseProject(null);
  if (t.id === 'pathGo') { const p = $('#pathIn').value.trim(); if (p) chooseProject({kind: 'local', target: p}); }
  if (t.id === 'browseBtn') {
    t.disabled = true; t.textContent = 'Waiting for the folder picker…';
    try { const r = await api('/api/browse', {}); if (r.path) return chooseProject({kind: 'local', target: r.path}); }
    catch (err) { toast(err.message); }
    fillProj();
  }
});

function fillAgent() {
  const team = teamOf();
  let h = '<div class="pop-label">Agents · pick one, or several to work as a team</div>';
  for (const x of S.agents) {
    const i = team.findIndex(m => m.name === x.name), m = team[i];
    const tag = m ? (team.length > 1 ? (i === 0 ? 'lead' : '') : '') : x.installed ? '' : 'not installed';
    h += `<button class="opt${m ? ' on' : ''}${x.installed ? '' : ' off'}" data-agent="${x.name}" aria-pressed="${!!m}">${m ? I.check : I.spark}<span>${esc(x.name)}</span><small>${tag}</small></button>`;
    if (m) h += `<div class="member"><input class="field" data-field="role" data-name="${x.name}" value="${esc(m.role)}" placeholder="Role (optional), e.g. design" aria-label="${x.name} role">` +
      (x.takes_model ? `<input class="field" data-field="model" data-name="${x.name}" value="${esc(m.model)}" placeholder="${x.needs_model ? 'Model (required)' : 'Model'}" aria-label="${x.name} model" style="width:120px;flex:none">` : '') + '</div>';
  }
  h += team.length > 1 ? '<div class="pop-note">They plan together first and split the work (each gets its own files), then build their parts at the same time.</div>'
    : '<div class="pop-note">Pick a second agent to make a team.</div>';
  $('#agentPop').innerHTML = h;
}
async function saveTeam(team) {
  try {
    if (cur) cur = await api('/api/team', {id: cur.id, team});
    else { pending.team = team; ls('team', JSON.stringify(team)); }
  } catch (e) {
    toast(e.message);
    if (cur) cur.team = team;  // keep it on screen so a missing model can still be typed in
  }
  renderComposer(); fillAgent();
}
$('#agentPop').addEventListener('click', e => {
  const b = e.target.closest('[data-agent]'); if (!b) return;
  const name = b.dataset.agent, team = teamOf().map(m => ({...m})), i = team.findIndex(m => m.name === name);
  if (i >= 0) { if (team.length === 1) return toast('Keep at least one agent.'); team.splice(i, 1); }
  else {
    if (team.length >= 4) return toast('Up to 4 agents at a time.');
    if (!agentInfo(name).installed) toast(`${name} isn't installed yet. Settings → Agents shows how.`);
    team.push({name, model: '', role: ''});
  }
  saveTeam(team);
});
$('#agentPop').addEventListener('change', e => {
  const f = e.target.dataset.field; if (!f) return;
  const team = teamOf().map(m => ({...m}));
  team.find(m => m.name === e.target.dataset.name)[f] = e.target.value.trim();
  saveTeam(team);
});
$('#agentPop').addEventListener('keydown', e => { if (e.key === 'Enter' && e.target.dataset.field) e.target.blur(); });

// ---------- chat list, header, drawer ----------
$('#chatList').addEventListener('click', async e => {
  const o = e.target.closest('[data-open]'), d = e.target.closest('[data-del]');
  if (o) return openChat(o.dataset.open);
  if (!d) return;
  const c = S.chats.find(x => x.id === d.dataset.del);
  if (!confirm(`Delete "${c.title}"?` + (c.kind === 'github' ? '\n\nThis also deletes agentflow\'s copy of the repo. Anything not pushed in a PR is lost.' : '\n\nYour folder and its files are not touched.'))) return;
  try { await api('/api/delete', {id: c.id}); } catch (err) { return toast(err.message); }
  if (cur && cur.id === c.id) newChat();
  refreshState();
});
$('#newBtn').onclick = newChat;
$('#menuBtn').onclick = () => $('#side').classList.toggle('open');
$('#prBtn').onclick = async () => { try { cur = await api('/api/pr', {id: cur.id}); stick = true; render(); poll(); } catch (e) { toast(e.message); } };
function openDiff() {
  const files = (cur.diff.match(/^diff --git /gm) || []).length + (cur.diff.match(/^new file \(untracked\)/gm) || []).length;
  $('#diffTitle').textContent = `Changes in this chat · ${files} file${files === 1 ? '' : 's'}`;
  $('#diff').innerHTML = cur.diff.split('\n').map(l => {
    if (l.startsWith('diff --git')) return `<div class="h">${esc(l.replace(/^diff --git a\/(.*?) b\/.*$/, '$1'))}</div>`;
    if (l.startsWith('new file (untracked)')) return `<div class="h">${esc(l.slice(22))} <span style="color:var(--green);font-weight:400">new</span></div>`;
    const c = /^(\+\+\+|---|@@|index |new file mode|deleted file mode|similarity|rename )/.test(l) ? 'm' : l[0] === '+' ? 'a' : l[0] === '-' ? 'd' : '';
    return `<div class="${c}">${esc(l) || ' '}</div>`;
  }).join('');
  $('#drawer').classList.add('open');
}
$('#changesBtn').onclick = openDiff;
$('#closeDiff').onclick = () => $('#drawer').classList.remove('open');
$('#msgs').addEventListener('click', e => { if (e.target.closest('[data-diff]')) openDiff(); });
$('#msgs').addEventListener('toggle', e => { const d = e.target; if (d.dataset && d.dataset.i !== undefined) d.open ? openSteps.add(cur.id + d.dataset.i) : openSteps.delete(cur.id + d.dataset.i); }, true);

// ---------- settings ----------
function fillSettings() {
  document.querySelectorAll('#setSeg button').forEach(b => b.classList.toggle('on', b.dataset.tab === setTab));
  const body = $('#setBody');
  if (setTab === 'agents') {
    body.innerHTML = '<div class="card">' + S.agents.map(a => `<div class="card-row"><div class="grow"><div class="name">${esc(a.name)}</div>
      <div class="sub">${a.installed ? esc(ABOUT[a.name] || 'Custom agent from ~/.agentflow.toml') : 'Install: <code>' + esc(INSTALL[a.name] || 'see the tool\'s docs') + '</code>'}</div></div>
      <span class="status${a.installed ? '' : ' no'}">${a.installed ? 'Ready' : 'Not installed'}</span></div>`).join('') + '</div>' +
      '<p class="muted">Agents use their own sign-in (your subscription), so there are no API keys here. After installing one, run it once in a terminal to sign in.</p>';
    return;
  }
  const names = Object.keys(S.mcp), shared = S.agents.filter(a => a.mcp).map(a => a.name).join(', ');
  if (!body.querySelector('#mcpText')) body.innerHTML = `<div id="mcpList"></div>
    <div class="drop" id="drop"><textarea id="mcpText" spellcheck="false" placeholder='Paste MCP JSON or drop a .json file here, for example:
{"mcpServers": {"github": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"]}}}'></textarea>
    <div class="row"><span class="note" style="font-size:12.5px">Works with the JSON from any MCP server's README.</span><div class="spacer"></div><button class="btn primary" id="mcpAdd">Add</button></div></div>
    <p class="muted">Turned-on servers are given to ${esc(shared)}. Cursor, Codex, Aider and OpenCode use their own MCP settings.</p>`;
  $('#mcpList').innerHTML = '<div class="card">' + (names.length ? names.map(n => { const s = S.mcp[n];
    const what = s.command ? [s.command, ...(s.args || [])].join(' ') : (s.url || s.httpUrl || '');
    return `<div class="card-row"><div class="grow"><div class="name">${esc(n)}</div><div class="sub"><code>${esc(what)}</code></div></div>
      <input type="checkbox" class="switch" data-toggle="${esc(n)}" aria-label="Turn ${esc(n)} on or off" ${s.disabled ? '' : 'checked'}>
      <button class="icon-btn" data-remove="${esc(n)}" aria-label="Remove ${esc(n)}">${I.trash}</button></div>`; }).join('')
    : '<div class="empty-card">No MCP servers yet. Add one below.</div>') + '</div>';
}
async function mcp(body) { try { S.mcp = (await api('/api/mcp', body)).mcp; fillSettings(); return true; } catch (e) { toast(e.message); } }
$('#settingsBtn').onclick = () => { fillSettings(); $('#settings').showModal(); };
$('#closeSettings').onclick = () => $('#settings').close();
$('#settings').addEventListener('click', e => { if (e.target === $('#settings')) $('#settings').close(); });
$('#setSeg').onclick = e => { const b = e.target.closest('[data-tab]'); if (b) { setTab = b.dataset.tab; $('#setBody').innerHTML = ''; fillSettings(); } };
$('#setBody').addEventListener('change', e => { if (e.target.dataset.toggle) mcp({action: 'toggle', name: e.target.dataset.toggle}); });
$('#setBody').addEventListener('click', async e => {
  const r = e.target.closest('[data-remove]');
  if (r && confirm(`Remove the MCP server "${r.dataset.remove}"?`)) mcp({action: 'delete', name: r.dataset.remove});
  if (e.target.id === 'mcpAdd') { const t = $('#mcpText'); if (t.value.trim() && await mcp({action: 'add', text: t.value})) { t.value = ''; toast('MCP server added.'); } }
});
$('#setBody').addEventListener('dragover', e => { const d = e.target.closest('#drop'); if (d) { e.preventDefault(); d.classList.add('over'); } });
$('#setBody').addEventListener('dragleave', e => { const d = e.target.closest('#drop'); if (d) d.classList.remove('over'); });
$('#setBody').addEventListener('drop', async e => {
  const d = e.target.closest('#drop'); if (!d) return;
  e.preventDefault(); d.classList.remove('over');
  const f = e.dataTransfer.files[0];
  if (f) $('#mcpText').value = await f.text();
});

// ---------- start ----------
refreshState().then(() => { const last = ls('chat'); if (last && S.chats.some(c => c.id === last)) openChat(last); else render(); });
setInterval(refreshState, 2500);
input.focus();
</script>
</body></html>
"""
