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
    # Claude Code as the harness, any Ollama model as the brain. --bare skips the user's global CLAUDE.md, hooks
    # and plugins, which swamp small models; file tools only. Ollama ignores the key's value; it is not a real key.
    "ollama": {
        "cmd": ["claude", "--bare", "-p", "{prompt}", "--model", "{model}", "--tools", "Read,Edit,Write,Glob,Grep",
                "--strict-mcp-config", "--permission-mode", "acceptEdits", "--output-format", "stream-json", "--verbose"],
        "env": {"ANTHROPIC_BASE_URL": "http://localhost:11434", "ANTHROPIC_API_KEY": "ollama"},
    },
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


def with_model(agent, model):
    """Fill the agent's {model} slot. Agents with a slot need a model; agents without one can't take one."""
    takes_model = any("{model}" in str(x) for x in agent["cmd"])
    if takes_model and not model:
        raise ValueError("this agent needs a model, e.g. --model qwen3.5:9b (see `ollama list`)")
    if model and not takes_model:
        raise ValueError("this agent doesn't take a model; add \"{model}\" to its cmd in ~/.agentflow.toml")
    return {**agent, "model": model}


def build_cmd(agent, prompt):
    # prompt last, so a "{model}" typed inside an issue is never substituted
    return [a.replace("{model}", agent.get("model") or "").replace("{prompt}", prompt) for a in agent["cmd"]]


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


def failed_checks(checks):
    """Unique failing checks as (label, GitHub Actions job id or None, link). Push and PR events duplicate jobs."""
    seen, out = set(), []
    for c in checks:
        label = f"{c.get('workflow') or ''} / {c['name']}".strip(" /")
        if c["bucket"] != "fail" or label in seen:
            continue
        seen.add(label)
        m = re.search(r"/actions/runs/\d+/job/(\d+)", c.get("link") or "")
        out.append((label, m and m.group(1), c.get("link")))
    return out


def clean_log(text):
    """Drop the 'job<TAB>step<TAB>timestamp ' prefix gh puts on every Actions log line."""
    return "\n".join(re.sub(r"^[^\t]*\t[^\t]*\t\S+ ?", "", line) for line in text.splitlines())


TEXT = {"text": True, "encoding": "utf-8", "errors": "replace"}


def run(cmd, cwd=None):
    return subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, **TEXT).stdout


def final_answer(lines):
    """The agent's final reply: Claude Code's result event if present, else every non-tool line it printed."""
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "result" and event.get("result"):
            return event["result"].strip()
    texts = (readable(line) for line in lines)
    return "\n".join(t for t in texts if t and not t.startswith("→ ")).strip()


def run_agent(d, agent, prompt, log):
    """Run the agent in d, streaming readable output to log. Returns its final answer."""
    env = {**os.environ, **agent.get("env", {})}
    log("status", "Agent working")
    p = subprocess.Popen(build_cmd(agent, prompt), cwd=d, env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **TEXT)
    lines = []
    for line in p.stdout:
        lines.append(line.rstrip("\n"))
        if (text := readable(lines[-1])) is not None:
            log("agent", text)
    if p.wait() != 0:
        raise RuntimeError("agent failed (is it installed and logged in?); nothing pushed")
    return final_answer(lines)


def agent_and_tests(d, agent, prompt, test_cmd, log):
    """Run the agent in d, stage its changes, run tests. Returns (passed: bool|None, test_cmd, log tail)."""
    run_agent(d, agent, prompt, log)
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


def check_author(issue, trust):
    """Refuse issues from people without write access unless the user vouched for this one."""
    if issue["author_association"] not in TRUSTED and not trust:
        raise RuntimeError(f"issue author @{issue['user']['login']} is not a collaborator ({issue['author_association']}); "
                           "read the issue, then rerun with --trust-author (or tick 'Trust author') if it is safe")


def issue_to_pr(repo, num, agent, test_cmd, log, trust=False):
    issue = json.loads(run(["gh", "api", f"repos/{repo}/issues/{num}"]))
    check_author(issue, trust)
    issue["body"] = issue["body"] or ""
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


def fix_pr(repo, num, agent, test_cmd, log, trust=False):  # trust unused: comments are filtered per author
    pr = json.loads(run(["gh", "pr", "view", str(num), "-R", repo, "--json",
                         "title,url,headRefName,isCrossRepository,comments,reviews,commits"]))
    if pr["isCrossRepository"]:
        # shortcut: fork PRs need push access to the fork; add when someone needs it
        raise RuntimeError("PRs from forks are not supported yet")
    inline = [c for page in json.loads(run(["gh", "api", "--paginate", "--slurp", f"repos/{repo}/pulls/{num}/comments"])) for c in page]
    feedback = feedback_since(pr, inline, pr["commits"][-1]["committedDate"])
    # gh exits non-zero whenever checks fail, are pending, or don't exist; the JSON on stdout is all we need
    checks = subprocess.run(["gh", "pr", "checks", str(num), "-R", repo, "--json", "name,bucket,link,workflow"],
                            capture_output=True, **TEXT).stdout
    ci = []
    for label, job, link in failed_checks(json.loads(checks or "[]")):
        logtext = clean_log(run(["gh", "run", "view", "--job", job, "-R", repo, "--log-failed"]))[-LOG_LIMIT:] if job \
            else f"(not a GitHub Actions check, no log available; see {link})"
        ci.append(f"Failing check \"{label}\":\n```\n{logtext}\n```")
    if not feedback and not ci:
        raise RuntimeError("no new feedback from collaborators and no failing checks; nothing to do")
    summary = f"{len(feedback)} comment(s) and {len(ci)} failing check(s)"
    log("status", f"To address: {summary}")
    with tempfile.TemporaryDirectory() as d:
        log("status", f"Cloning {repo}")
        run(["gh", "repo", "clone", repo, d])
        run(["gh", "pr", "checkout", str(num)], d)
        prompt = (f"You are updating the existing pull request \"{pr['title']}\" in the current repo. "
                  "Edit files to address the review feedback and fix the failing CI checks below.\n\n"
                  + "\n\n".join(feedback + ci))
        passed, test_cmd, tail = agent_and_tests(d, agent, prompt, test_cmd, log)
        log("status", "Pushing to the PR")
        run(["git", "commit", "-m", f"Address review feedback and CI\n\n{summary} on #{num}"], d)
        run(["git", "push"], d)
        if passed is not None:  # draft while tests fail, ready once they pass; errors just mean it already is
            subprocess.run(["gh", "pr", "ready", str(num), *([] if passed else ["--undo"])], cwd=d, capture_output=True)
        run(["gh", "pr", "comment", str(num), "--body",
             f"{MARKER}\nagentflow addressed {summary}.\n\n{test_note(passed, test_cmd, tail)}"], d)
        return pr["url"]


DIFF_LIMIT = 40000  # keep the prompt within a small local model's context


def review_pr(repo, num, agent, log):
    """Agent reviews the PR without changing it; the review is posted as a PR comment-review.
    It is deliberately not tagged with MARKER, so a later fix run treats it as feedback to address."""
    pr = json.loads(run(["gh", "pr", "view", str(num), "-R", repo, "--json", "title,body,url"]))
    diff = run(["gh", "pr", "diff", str(num), "-R", repo])
    if len(diff) > DIFF_LIMIT:
        diff = diff[:DIFF_LIMIT] + "\n... (diff truncated)"
    with tempfile.TemporaryDirectory() as d:  # throwaway checkout: any edits the agent makes are discarded
        log("status", f"Cloning {repo}")
        run(["gh", "repo", "clone", repo, d])
        run(["gh", "pr", "checkout", str(num)], d)
        prompt = (f"Review the pull request \"{pr['title']}\", which is checked out in the current repo. Do NOT edit "
                  "any files. Read surrounding code as needed. Reply with only the review, in Markdown: bugs first, "
                  "then risks, then small suggestions, citing file:line. Say plainly if it looks good.\n\n"
                  f"PR description:\n{pr['body'] or '(none)'}\n\nDiff:\n```diff\n{diff}\n```")
        review = run_agent(d, agent, prompt, log)
    if not review:
        raise RuntimeError("agent returned an empty review; nothing posted")
    log("status", "Posting review")
    run(["gh", "pr", "review", str(num), "-R", repo, "--comment", "--body", f"**agentflow review**\n\n{review}"])
    return pr["url"]


def resolve(ref, agent, test_cmd=None, log=lambda kind, text: print(text), trust=False, review=False):
    """Issue -> new PR; PR -> push fixes for its review comments and failing CI; review=True -> post a review.
    log(kind, text) gets progress events: status, agent, diff, tests, pr. Raises RuntimeError or CalledProcessError."""
    repo, num = parse_ref(ref)
    is_pr = subprocess.run(["gh", "pr", "view", str(num), "-R", repo, "--json", "number"], capture_output=True).returncode == 0
    log("status", f"Reading {'PR' if is_pr else 'issue'} {ref}")
    if review:
        if not is_pr:
            raise RuntimeError(f"{ref} is an issue; --review works on PRs only")
        url = review_pr(repo, num, agent, log)
        log("pr", url)
        return url
    url = (fix_pr if is_pr else issue_to_pr)(repo, num, agent, test_cmd, log, trust)
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
    p.add_argument("--model", help="model for agents that take one, e.g. --agent ollama --model qwen3.5:9b")
    p.add_argument("--trust-author", action="store_true", help="run on an issue from a non-collaborator (read it first)")
    p.add_argument("--review", action="store_true", help="post a code review on the PR instead of changing it")
    a = p.parse_args()
    try:
        resolve(a.ref, with_model(agents[a.agent], a.model), a.test, trust=a.trust_author, review=a.review)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as e:
        sys.exit(f"error: {e}" + (f"\n{e.stderr}" if getattr(e, "stderr", None) else ""))


if __name__ == "__main__":
    main()
