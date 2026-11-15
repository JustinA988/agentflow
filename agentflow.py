"""agentflow: turn a GitHub issue into a PR using an AI CLI you are already logged in to.

No API keys: the agent is any local command (subscription CLIs like claude/codex, or local models).
Usage: agentflow owner/repo#123 [--agent NAME] [--test "pytest"]
Custom agents: ~/.agentflow.toml (see README).
"""
import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

BUILTIN_AGENTS = {
    "claude": {"cmd": ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"]},
    "codex": {"cmd": ["codex", "exec", "--full-auto", "{prompt}"]},
}
CONFIG = Path.home() / ".agentflow.toml"
LOG_LIMIT = 3000  # GitHub PR bodies cap at 65k chars; keep the tail of the log only


def load_agents(path=CONFIG):
    agents = dict(BUILTIN_AGENTS)
    if path.exists():
        for name, a in tomllib.loads(path.read_text()).get("agents", {}).items():
            if not isinstance(a.get("cmd"), list) or not any("{prompt}" in str(x) for x in a["cmd"]):
                raise ValueError(f"agent {name!r} in {path}: cmd must be a list containing \"{{prompt}}\"")
            agents[name] = a
    return agents


def parse_ref(ref):
    m = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", ref)
    if not m:
        raise ValueError(f"expected owner/repo#number, got {ref!r}")
    return m.group(1), int(m.group(2))


def build_cmd(agent, prompt):
    return [a.replace("{prompt}", prompt) for a in agent["cmd"]]


def detect_test_cmd(repo):
    repo = Path(repo)
    if (repo / "package.json").exists():
        return "npm test"
    if (repo / "Cargo.toml").exists():
        return "cargo test"
    if (repo / "go.mod").exists():
        return "go test ./..."
    if any((repo / f).exists() for f in ("pyproject.toml", "setup.py", "pytest.ini")) or list(repo.glob("test_*.py")) or (repo / "tests").is_dir():
        return "python -m pytest"
    return None


TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, **TEXT).stdout


def issue_to_pr(ref, agent, test_cmd=None, log=lambda kind, text: print(text)):
    """log(kind, text) gets progress events: status, agent, diff, tests, pr. Raises RuntimeError on failure."""
    repo, num = parse_ref(ref)
    log("status", f"Reading issue {ref}")
    issue = json.loads(run(["gh", "issue", "view", str(num), "-R", repo, "--json", "title,body"]))
    branch = f"agentflow/issue-{num}"
    with tempfile.TemporaryDirectory() as d:
        log("status", f"Cloning {repo}")
        run(["gh", "repo", "clone", repo, d])
        run(["git", "checkout", "-b", branch], d)
        prompt = f"Resolve this GitHub issue by editing files in the current repo.\n\n{issue['title']}\n\n{issue['body']}"
        env = {**os.environ, **agent.get("env", {})}
        log("status", "Agent working")
        p = subprocess.Popen(build_cmd(agent, prompt), cwd=d, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **TEXT)
        for line in p.stdout:
            log("agent", line.rstrip("\n"))
        if p.wait() != 0:
            raise RuntimeError("agent failed (is it installed and logged in?); no PR opened")
        run(["git", "add", "-A"], d)
        diff = run(["git", "diff", "--cached"], d)
        if not diff.strip():
            raise RuntimeError("agent made no changes; no PR opened")
        log("diff", diff)

        body, draft = f"Closes #{num}", []
        test_cmd = test_cmd or detect_test_cmd(d)
        if test_cmd:
            log("status", f"Running tests: {test_cmd}")
            t = subprocess.run(test_cmd, cwd=d, shell=True, capture_output=True, **TEXT)
            tail = (t.stdout + t.stderr)[-LOG_LIMIT:]
            if t.returncode == 0:
                body += f"\n\nTests passed: `{test_cmd}`"
                log("tests", f"PASSED: {test_cmd}\n\n{tail}")
            else:
                draft = ["--draft"]
                body += f"\n\n**Tests failed:** `{test_cmd}`\n\n```\n{tail}\n```"
                log("tests", f"FAILED: {test_cmd} (PR will be a draft)\n\n{tail}")
        else:
            body += "\n\nNo test command found; tests not run."
            log("tests", "No test command found; tests not run.")

        log("status", "Pushing and opening PR")
        run(["git", "commit", "-m", f"{issue['title']}\n\nCloses #{num}"], d)
        run(["git", "push", "-u", "origin", branch], d)
        url = run(["gh", "pr", "create", "--title", issue["title"], "--body", body, *draft], d).strip()
        log("pr", url)
        return url


def main():
    if sys.argv[1:2] == ["ui"]:
        import agentflow_ui
        return agentflow_ui.serve()
    agents = load_agents()
    p = argparse.ArgumentParser(prog="agentflow", epilog="Run 'agentflow ui' for the local web UI.")
    p.add_argument("ref", help="owner/repo#number")
    p.add_argument("--agent", choices=agents, default="claude")
    p.add_argument("--test", help="test command to run before opening the PR (default: auto-detect)")
    a = p.parse_args()
    try:
        issue_to_pr(a.ref, agents[a.agent], a.test)
    except (RuntimeError, subprocess.CalledProcessError) as e:
        sys.exit(f"error: {e}" + (f"\n{e.stderr}" if getattr(e, "stderr", None) else ""))


if __name__ == "__main__":
    main()
