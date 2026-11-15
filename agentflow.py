"""agentflow: turn a GitHub issue into a PR using an AI CLI you are already logged in to.

No API keys: the agent is any local command (subscription CLIs like claude/codex).
Usage: agentflow owner/repo#123 [--agent claude|codex]
"""
import argparse
import json
import re
import subprocess
import sys
import tempfile

# shortcut: built-in agents only; add local/other models by extending this dict, or a config file later.
AGENTS = {
    "claude": ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits"],
    "codex": ["codex", "exec", "--full-auto", "{prompt}"],
}


def parse_ref(ref):
    m = re.fullmatch(r"([\w.-]+/[\w.-]+)#(\d+)", ref)
    if not m:
        raise ValueError(f"expected owner/repo#number, got {ref!r}")
    return m.group(1), int(m.group(2))


def build_cmd(agent, prompt):
    return [a.replace("{prompt}", prompt) for a in AGENTS[agent]]


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout


def issue_to_pr(ref, agent):
    repo, num = parse_ref(ref)
    issue = json.loads(run(["gh", "issue", "view", str(num), "-R", repo, "--json", "title,body"]))
    branch = f"agentflow/issue-{num}"
    with tempfile.TemporaryDirectory() as d:
        run(["gh", "repo", "clone", repo, d])
        run(["git", "checkout", "-b", branch], d)
        prompt = f"Resolve this GitHub issue by editing files in the current repo.\n\n{issue['title']}\n\n{issue['body']}"
        subprocess.run(build_cmd(agent, prompt), cwd=d, check=True)
        if not run(["git", "status", "--porcelain"], d).strip():
            sys.exit("agent made no changes; no PR opened")
        run(["git", "add", "-A"], d)
        run(["git", "commit", "-m", f"{issue['title']}\n\nCloses #{num}"], d)
        run(["git", "push", "-u", "origin", branch], d)
        return run(["gh", "pr", "create", "--title", issue["title"], "--body", f"Closes #{num}"], d).strip()


def main():
    p = argparse.ArgumentParser(prog="agentflow")
    p.add_argument("ref", help="owner/repo#number")
    p.add_argument("--agent", choices=AGENTS, default="claude")
    a = p.parse_args()
    print(issue_to_pr(a.ref, a.agent))


if __name__ == "__main__":
    main()
