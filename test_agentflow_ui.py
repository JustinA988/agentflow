import http.client
import json
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

import agentflow_ui as ui

# A stand-in agent: prints a reply and creates a file, like a real agent editing the project.
FAKE = {"cmd": [sys.executable, "-c", "print('hello from fake'); open('new.txt', 'w').write('x')", "{prompt}"]}
# A stand-in team member: answers planning prompts with a plan, and builds (writes its own file) once given the plan.
TEAMY = {"cmd": [sys.executable, "-c", """import os, sys
p = sys.argv[-1]
if 'agreed plan' in p:
    open(f'part-{os.getpid()}.txt', 'w').write('x'); print('built')
else:
    print('FINAL PLAN' if 'drafted this plan' in p else 'PLAN: split it')""", "{prompt}"]}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    for name, file in [("CHATS", "chats.json"), ("MCP_FILE", "mcp.json"), ("MCP_ACTIVE", "mcp-active.json"), ("WORK", "work"), ("SCRATCH", "projects")]:
        monkeypatch.setattr(ui, name, tmp_path / "home" / file)
    ui.chats.clear()
    ui.Handler.agents = {"claude": {"cmd": ["claude", "-p", "{prompt}"], "model_args": ["--model", "{model}"]}, "fake": FAKE}
    yield tmp_path


@pytest.fixture
def server():
    s = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s.server_address[1]
    s.shutdown()


def req(port, method, path, body=None, host=None, ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", port)
    headers = {"Host": host or f"localhost:{ui.PORT}", "Content-Type": ctype}
    c.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = c.getresponse()
    return r.status, json.loads(r.read().decode()) if path.startswith("/api") else None


def git_repo(path):
    path.mkdir()
    for cmd in (["git", "init", "-q"], ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"]):
        subprocess.run(cmd, cwd=path, check=True)
    return path


def test_page_and_state(server):
    assert req(server, "GET", "/")[0] == 200
    status, body = req(server, "GET", "/api/state")
    assert status == 200 and [a["name"] for a in body["agents"]] == ["claude", "fake"]
    assert body["agents"][0]["takes_model"] and not body["agents"][0]["needs_model"]


def test_rejects_foreign_host_and_non_json_post(server):
    assert req(server, "GET", "/", host="evil.example:8765")[0] == 403
    assert req(server, "POST", "/api/chats", {"kind": "local"}, ctype="text/plain")[0] == 403


def test_new_chat_validation(server, tmp_path):
    assert req(server, "POST", "/api/chats", {"kind": "github", "target": "--help"})[0] == 400
    assert req(server, "POST", "/api/chats", {"kind": "github", "target": "a/b", "agent": "ghost"})[0] == 400
    assert req(server, "POST", "/api/chats", {"kind": "local", "target": str(tmp_path / "missing")})[0] == 400
    assert req(server, "POST", "/api/chats", {"kind": "local", "target": str(tmp_path), "agent": "fake", "model": "x"})[0] == 400
    status, chat = req(server, "POST", "/api/chats", {"kind": "github", "target": "octo/cat", "agent": "claude", "model": "sonnet"})
    assert status == 200 and chat["dir"].endswith(chat["id"]) and chat["team"][0]["model"] == "sonnet"
    team = lambda *names: {"kind": "new", "name": "x", "team": [{"name": n} for n in names]}
    for bad in (team(), team("fake", "fake"), team("ghost"), team(*["fake"] * 5)):
        assert req(server, "POST", "/api/chats", bad)[0] == 400


def test_chat_turn_runs_agent_and_tracks_changes(server, tmp_path):
    repo = git_repo(tmp_path / "proj")
    _, chat = req(server, "POST", "/api/chats", {"kind": "local", "target": str(repo), "agent": "fake"})
    assert req(server, "POST", "/api/send", {"id": chat["id"], "text": "/issue 3"})[0] == 400  # GitHub chats only
    assert req(server, "POST", "/api/send", {"id": chat["id"], "text": "make a file"})[0] == 200
    for _ in range(100):
        _, chat = req(server, "GET", f"/api/chat?id={chat['id']}")
        if chat["status"] == "idle":
            break
        time.sleep(0.1)
    reply = chat["messages"][-1]
    assert chat["title"] == "make a file" and reply["status"] == "done" and reply["text"] == "hello from fake"
    assert reply["files"] == 1 and "new.txt" in chat["diff"]
    assert json.loads(ui.CHATS.read_text())[0]["messages"][-1]["text"] == "hello from fake"  # saved to disk


def test_load_chats_marks_interrupted_runs_stopped():
    ui.CHATS.parent.mkdir(parents=True)
    ui.CHATS.write_text(json.dumps([{"id": "a", "status": "running", "messages": [{"role": "agent", "status": "running"}]}]))
    c = ui.load_chats()["a"]
    assert c["status"] == "idle" and c["messages"][0]["status"] == "stopped"


def test_with_history():
    msgs = [{"role": "user", "text": "hi"}, {"role": "agent", "text": "hello"}]
    assert ui.with_history([], "next") == "next"
    p = ui.with_history(msgs, "next")
    assert "User: hi\n\nAssistant: hello" in p and p.endswith("New message from the user:\nnext")
    assert len(ui.with_history([{"role": "user", "text": "x" * 50000}], "q")) < ui.HISTORY_CHARS + 100


def test_mcp_parse_and_edit(server):
    one = {"command": "npx", "args": ["-y", "srv"]}
    assert ui.parse_mcp(json.dumps({"mcpServers": {"a": one}})) == {"a": one}
    assert ui.parse_mcp(json.dumps({"servers": {"b": {"url": "http://x"}}})) == {"b": {"url": "http://x"}}
    assert ui.parse_mcp(json.dumps({"c": one})) == {"c": one}
    for bad in ("nope", "[]", "{}", '{"x": {"args": []}}'):
        with pytest.raises(ValueError):
            ui.parse_mcp(bad)
    assert req(server, "POST", "/api/mcp", {"action": "add", "text": json.dumps({"mcpServers": {"a": one, "b": one}})})[0] == 200
    assert req(server, "POST", "/api/mcp", {"action": "toggle", "name": "b"})[1]["mcp"]["b"]["disabled"] is True
    path = ui.active_mcp(ui.load_mcp())
    assert json.loads(open(path).read()) == {"mcpServers": {"a": one}}  # disabled server left out, flag stripped
    req(server, "POST", "/api/mcp", {"action": "delete", "name": "a"})
    assert ui.active_mcp(ui.load_mcp()) is None
    assert req(server, "POST", "/api/mcp", {"action": "delete", "name": "zzz"})[0] == 400


def wait_idle(port, cid):
    for _ in range(150):
        _, chat = req(port, "GET", f"/api/chat?id={cid}")
        if chat["status"] == "idle":
            return chat
        time.sleep(0.1)
    raise AssertionError("chat never finished")


def test_team_plans_then_builds_at_once_in_a_new_project(server):
    ui.Handler.agents.update(a=TEAMY, b=TEAMY)
    body = {"kind": "new", "name": "Todo App!", "team": [{"name": "a", "role": "design"}, {"name": "b", "role": "coding"}]}
    _, chat = req(server, "POST", "/api/chats", body)
    folder = Path(chat["dir"])
    assert folder.parent == ui.SCRATCH and folder.name.startswith("todo-app-") and (folder / ".git").is_dir()
    req(server, "POST", "/api/send", {"id": chat["id"], "text": "build it"})
    msgs = wait_idle(server, chat["id"])["messages"][1:]
    assert [(m["agent"], m["label"], m["text"]) for m in msgs[:2]] == [("a", "plan", "PLAN: split it"), ("b", "plan review", "FINAL PLAN")]
    assert sorted((m["agent"], m["label"], m["text"]) for m in msgs[2:]) == [("a", "design", "built"), ("b", "coding", "built")]
    assert all(m["status"] == "done" for m in msgs) and msgs[-1]["files"] == 2  # both built, in parallel, in one folder
