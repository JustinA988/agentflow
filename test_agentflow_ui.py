import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

import agentflow_ui as ui


@pytest.fixture
def server():
    ui.Handler.agents = {"claude": {}}
    s = ThreadingHTTPServer(("127.0.0.1", 0), ui.Handler)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield s.server_address[1]
    s.shutdown()


def req(port, method, path, body=None, host=None, ctype="application/json"):
    c = http.client.HTTPConnection("127.0.0.1", port)
    headers = {"Host": host or f"localhost:{ui.PORT}", "Content-Type": ctype}
    c.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = c.getresponse()
    return r.status, r.read().decode()


def test_page_and_runs(server):
    assert req(server, "GET", "/")[0] == 200
    status, body = req(server, "GET", "/api/runs")
    assert status == 200 and json.loads(body)["agents"] == ["claude"]


def test_rejects_foreign_host_and_non_json_post(server):
    assert req(server, "GET", "/", host="evil.example:8765")[0] == 403
    assert req(server, "POST", "/api/run", {"ref": "a/b#1"}, ctype="text/plain")[0] == 403


def test_rejects_bad_input(server):
    assert req(server, "POST", "/api/run", {"ref": "nope"})[0] == 400
    assert req(server, "POST", "/api/run", {"ref": "a/b#1", "agent": "ghost"})[0] == 400


def test_items_endpoint(server, monkeypatch):
    fake = {"issue": '[{"number": 3, "title": "bug", "author": {"login": "a"}}]',
            "pr": '[{"number": 4, "title": "fix", "author": {"login": "b"}}]'}
    monkeypatch.setattr(ui, "run", lambda cmd: fake[cmd[1]])
    status, body = req(server, "GET", "/api/items?repo=octo/cat")
    assert status == 200 and [(i["kind"], i["number"]) for i in json.loads(body)["items"]] == [("pr", 4), ("issue", 3)]
    assert req(server, "GET", "/api/items?repo=--help")[0] == 400


def test_history_roundtrip(tmp_path):
    ui.runs[:] = [{"id": 1, "status": "done"}]
    ui.save_history(tmp_path / "h.json")
    assert ui.load_history(tmp_path / "h.json") == ui.runs
    assert ui.load_history(tmp_path / "missing.json") == []
