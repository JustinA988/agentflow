"""agentflow: turn a GitHub issue into a PR, or fix a PR from its review comments,
using an AI CLI you are already logged in to.

No API keys: the agent is any local command (subscription CLIs like claude/codex, or local models).
Usage: agentflow owner/repo#123 [--agent NAME] [--test "pytest"]   (123 = issue or PR)
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
    # stream-json so output arrives live; plain -p prints only at the end
    "claude": {"cmd": ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits", "--output-format", "stream-json", "--verbose"]},
    "codex": {"cmd": ["codex", "exec", "--full-auto", "{prompt}"]},
}
CONFIG = Path.home() / ".agentflow.toml"
LOG_LIMIT = 3000  # GitHub PR bodies cap at 65k chars; keep the tail of the log only
# Only feedback from people with write access reaches the agent; anyone can comment on a public PR.
TRUSTED = {"OWNER", "MEMBER", "COLLABORATOR"}
MARKER = "<!-- agentflow -->"  # tags our own PR comments so they are never fed back as feedback


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


def readable(line):
    """Turn a Claude Code stream-json line into text; pass other agents' plain lines through. None = skip."""
    try:
        event = json.loads(line)
    except ValueError:
        return line
    if not isinstance(event, dict):
        return line
    if event.get("type") != "assistant":
        return None  # system/hook/tool_result/result events are noise here
    out = []
    for part in event.get("message", {}).get("content", []):
        if part.get("type") == "text":
            out.append(part["text"])
        elif part.get("type") == "tool_use":
            args = part.get("input", {})
            target = args.get("file_path") or args.get("command") or args.get("pattern") or ""
            out.append(f"→ {part.get('name')} {Path(target).name if 'file_path' in args else target}".rstrip())
    return "\n".join(out) or None


def feedback_since(pr, inline, since):
    """Trusted, non-empty review feedback newer than `since` (ISO time of the PR's last commit)."""
    items = [(c["createdAt"], c["authorAssociation"], c["author"]["login"], "", c["body"]) for c in pr.get("comments", [])]
    items += [(r.get("submittedAt") or "", r["authorAssociation"], r["author"]["login"], "", r["body"]) for r in pr.get("reviews", [])]
    items += [(c["created_at"], c["author_association"], c["user"]["login"], f" on {c['path']}:{c.get('line') or '?'}", c["body"])
              for c in inline]
    return [f"@{who}{where}: {body.strip()}" for when, assoc, who, where, body in sorted(items)
            if when > since and assoc in TRUSTED and body.strip() and MARKER not in body]


TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, **TEXT).stdout


def agent_and_tests(d, agent, prompt, test_cmd, log):
    """Run the agent in d, stage its changes, run tests. Returns (passed: bool|None, test_cmd, log tail)."""
    env = {**os.environ, **agent.get("env", {})}
    log("status", "Agent working")
    p = subprocess.Popen(build_cmd(agent, prompt), cwd=d, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **TEXT)
    for line in p.stdout:
        if (text := readable(line.rstrip("\n"))) is not None:
            log("agent", text)
    if p.wait() != 0:
        raise RuntimeError("agent failed (is it installed and logged in?); nothing pushed")
    run(["git", "add", "-A"], d)
    diff = run(["git", "diff", "--cached"], d)
    if not diff.strip():
        raise RuntimeError("agent made no changes; nothing pushed")
    log("diff", diff)

    test_cmd = test_cmd or detect_test_cmd(d)
    if not test_cmd:
        log("tests", "No test command found; tests not run.")
        return None, None, ""
    log("status", f"Running tests: {test_cmd}")
    t = subprocess.run(test_cmd, cwd=d, shell=True, capture_output=True, **TEXT)
    tail = (t.stdout + t.stderr)[-LOG_LIMIT:]
    log("tests", f"{'PASSED' if t.returncode == 0 else 'FAILED'}: {test_cmd}\n\n{tail}")
    return t.returncode == 0, test_cmd, tail


def test_note(passed, test_cmd, tail):
    if passed is None:
        return "No test command found; tests not run."
    if passed:
        return f"Tests passed: `{test_cmd}`"
    return f"**Tests failed:** `{test_cmd}`\n\n```\n{tail}\n```"


def issue_to_pr(repo, num, agent, test_cmd, log):
    issue = json.loads(run(["gh", "issue", "view", str(num), "-R", repo, "--json", "title,body"]))
    with tempfile.TemporaryDirectory() as d:
        log("status", f"Cloning {repo}")
        run(["gh", "repo", "clone", repo, d])
        run(["git", "checkout", "-b", f"agentflow/issue-{num}"], d)
        prompt = f"Resolve this GitHub issue by editing files in the current repo.\n\n{issue['title']}\n\n{issue['body']}"
        passed, test_cmd, tail = agent_and_tests(d, agent, prompt, test_cmd, log)
        log("status", "Pushing and opening PR")
        run(["git", "commit", "-m", f"{issue['title']}\n\nCloses #{num}"], d)
        run(["git", "push", "-u", "origin", f"agentflow/issue-{num}"], d)
        body = f"Closes #{num}\n\n{test_note(passed, test_cmd, tail)}"
        return run(["gh", "pr", "create", "--title", issue["title"], "--body", body,
                    *(["--draft"] if passed is False else [])], d).strip()


def fix_pr(repo, num, agent, test_cmd, log):
    pr = json.loads(run(["gh", "pr", "view", str(num), "-R", repo, "--json",
                         "title,url,headRefName,isCrossRepository,comments,reviews,commits"]))
    if pr["isCrossRepository"]:
        # shortcut: fork PRs need push access to the fork; add when someone needs it
        raise RuntimeError("PRs from forks are not supported yet")
    inline = [c for page in json.loads(run(["gh", "api", "--paginate", "--slurp", f"repos/{repo}/pulls/{num}/comments"])) for c in page]
    feedback = feedback_since(pr, inline, pr["commits"][-1]["committedDate"])
    if not feedback:
        raise RuntimeError("no new feedback from collaborators since the last commit; nothing to do")
    log("status", f"{len(feedback)} new comment(s) to address")
    with tempfile.TemporaryDirectory() as d:
        log("status", f"Cloning {repo}")
        run(["gh", "repo", "clone", repo, d])
        run(["gh", "pr", "checkout", str(num)], d)
        prompt = (f"You are updating the existing pull request \"{pr['title']}\" in the current repo. "
                  "Address this review feedback by editing files.\n\n" + "\n\n".join(feedback))
        passed, test_cmd, tail = agent_and_tests(d, agent, prompt, test_cmd, log)
        log("status", "Pushing to the PR")
        run(["git", "commit", "-m", f"Address review feedback\n\n{len(feedback)} comment(s) on #{num}"], d)
        run(["git", "push"], d)
        if passed is not None:  # draft while tests fail, ready once they pass; errors just mean it already is
            subprocess.run(["gh", "pr", "ready", str(num), *([] if passed else ["--undo"])], cwd=d, capture_output=True)
        run(["gh", "pr", "comment", str(num), "--body",
             f"{MARKER}\nagentflow addressed {len(feedback)} comment(s).\n\n{test_note(passed, test_cmd, tail)}"], d)
        return pr["url"]


def resolve(ref, agent, test_cmd=None, log=lambda kind, text: print(text)):
    """Issue -> new PR; PR -> push fixes for its review comments. log(kind, text) gets progress events:
    status, agent, diff, tests, pr. Raises RuntimeError or CalledProcessError on failure."""
    repo, num = parse_ref(ref)
    is_pr = subprocess.run(["gh", "pr", "view", str(num), "-R", repo, "--json", "number"], capture_output=True).returncode == 0
    log("status", f"Reading {'PR' if is_pr else 'issue'} {ref}")
    url = (fix_pr if is_pr else issue_to_pr)(repo, num, agent, test_cmd, log)
    log("pr", url)
    return url


def main():
    if sys.argv[1:2] == ["ui"]:
        import agentflow_ui
        return agentflow_ui.serve()
    agents = load_agents()
    p = argparse.ArgumentParser(prog="agentflow", epilog="Run 'agentflow ui' for the local web UI.")
    p.add_argument("ref", help="owner/repo#number: an issue (opens a PR) or a PR (addresses new review comments)")
    p.add_argument("--agent", choices=agents, default="claude")
    p.add_argument("--test", help="test command to run before pushing (default: auto-detect)")
    a = p.parse_args()
    try:
        resolve(a.ref, agents[a.agent], a.test)
    except (RuntimeError, subprocess.CalledProcessError) as e:
        sys.exit(f"error: {e}" + (f"\n{e.stderr}" if getattr(e, "stderr", None) else ""))


if __name__ == "__main__":
    main()
