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


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout


def issue_to_pr(ref, agent, test_cmd=None):
    repo, num = parse_ref(ref)
    issue = json.loads(run(["gh", "issue", "view", str(num), "-R", repo, "--json", "title,body"]))
    branch = f"agentflow/issue-{num}"
    with tempfile.TemporaryDirectory() as d:
        run(["gh", "repo", "clone", repo, d])
        run(["git", "checkout", "-b", branch], d)
        prompt = f"Resolve this GitHub issue by editing files in the current repo.\n\n{issue['title']}\n\n{issue['body']}"
        env = {**os.environ, **agent.get("env", {})}
        if subprocess.run(build_cmd(agent, prompt), cwd=d, env=env).returncode != 0:
            sys.exit("agent failed (is it installed and logged in?); no PR opened")
        if not run(["git", "status", "--porcelain"], d).strip():
            sys.exit("agent made no changes; no PR opened")

        body, draft = f"Closes #{num}", []
        test_cmd = test_cmd or detect_test_cmd(d)
        if test_cmd:
            t = subprocess.run(test_cmd, cwd=d, shell=True, text=True, capture_output=True)
            if t.returncode == 0:
                body += f"\n\nTests passed: `{test_cmd}`"
            else:
                draft = ["--draft"]
                log = (t.stdout + t.stderr)[-LOG_LIMIT:]
                body += f"\n\n**Tests failed:** `{test_cmd}`\n\n```\n{log}\n```"
        else:
            body += "\n\nNo test command found; tests not run."

        run(["git", "add", "-A"], d)
        run(["git", "commit", "-m", f"{issue['title']}\n\nCloses #{num}"], d)
        run(["git", "push", "-u", "origin", branch], d)
        return run(["gh", "pr", "create", "--title", issue["title"], "--body", body, *draft], d).strip()


def main():
    agents = load_agents()
    p = argparse.ArgumentParser(prog="agentflow")
    p.add_argument("ref", help="owner/repo#number")
    p.add_argument("--agent", choices=agents, default="claude")
    p.add_argument("--test", help="test command to run before opening the PR (default: auto-detect)")
    a = p.parse_args()
    print(issue_to_pr(a.ref, agents[a.agent], a.test))


if __name__ == "__main__":
    main()
