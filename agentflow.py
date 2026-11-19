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
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path, PureWindowsPath

# cmd runs one turn; optional: model_args (added when a model is picked), resume (continue a chat session),
# mcp / mcp_env (hand the agent a {"mcpServers": ...} file). Everything here uses the CLI's own sign-in.
BUILTIN_AGENTS = {
    # stream-json so output arrives live; plain -p prints only at the end
    "claude": {"cmd": ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits", "--output-format", "stream-json", "--verbose"],
               "model_args": ["--model", "{model}"], "resume": ["--resume", "{session}"], "mcp": ["--mcp-config", "{mcp}"]},
    "codex": {"cmd": ["codex", "exec", "--full-auto", "{prompt}"], "model_args": ["-m", "{model}"]},
    # Claude Code as the harness, any Ollama model as the brain. --bare skips the user's global CLAUDE.md, hooks
    # and plugins, which swamp small models; file tools only. Ollama ignores the key's value; it is not a real key.
    "ollama": {
        "cmd": ["claude", "--bare", "-p", "{prompt}", "--model", "{model}", "--tools", "Read,Edit,Write,Glob,Grep",
                "--strict-mcp-config", "--permission-mode", "acceptEdits", "--output-format", "stream-json", "--verbose"],
        "env": {"ANTHROPIC_BASE_URL": "http://localhost:11434", "ANTHROPIC_API_KEY": "ollama"},
        "resume": ["--resume", "{session}"], "mcp": ["--mcp-config", "{mcp}"],
    },
    "cursor": {"cmd": ["cursor-agent", "-p", "--force", "--output-format", "stream-json", "{prompt}"],
               "model_args": ["--model", "{model}"], "resume": ["--resume", "{session}"]},
    # Gemini and Qwen Code read a "system settings" file that may hold mcpServers; pointing it at ours adds them.
    "gemini": {"cmd": ["gemini", "--yolo", "-p", "{prompt}"], "model_args": ["-m", "{model}"],
               "mcp_env": {"GEMINI_CLI_SYSTEM_SETTINGS_PATH": "{mcp}"}},
    "qwen": {"cmd": ["qwen", "--yolo", "-p", "{prompt}"], "model_args": ["-m", "{model}"],
             "mcp_env": {"QWEN_CODE_SYSTEM_SETTINGS_PATH": "{mcp}"}},
    "aider": {"cmd": ["aider", "--yes-always", "--no-auto-commits", "--no-pretty", "--no-check-update", "--message", "{prompt}"],
              "model_args": ["--model", "{model}"]},
    "opencode": {"cmd": ["opencode", "run", "{prompt}"], "model_args": ["-m", "{model}"]},
    "copilot": {"cmd": ["copilot", "--allow-all-tools", "-p", "{prompt}"], "model_args": ["--model", "{model}"],
                "mcp": ["--additional-mcp-config", "@{mcp}"]},
}
ALIASES = {"cursor-agent": "agent"}  # Cursor's installer may name its binary either way
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
    """Set the agent's model. A {model} slot in cmd makes it required; model_args make it optional."""
    slot = any("{model}" in str(x) for x in agent["cmd"])
    if slot and not model:
        raise ValueError("this agent needs a model, e.g. --model qwen3.5:9b (see `ollama list`)")
    if model and not slot and not agent.get("model_args"):
        raise ValueError("this agent doesn't take a model; add \"{model}\" to its cmd in ~/.agentflow.toml")
    return {**agent, "model": model}


def build_cmd(agent, prompt, session=None, mcp=None):
    cmd = list(agent["cmd"])
    if agent.get("model") and "{model}" not in " ".join(cmd):
        cmd += agent.get("model_args", [])
    if session:
        cmd += agent.get("resume", [])
    if mcp:
        cmd += agent.get("mcp", [])
    fill = {"{model}": agent.get("model") or "", "{session}": session or "", "{mcp}": str(mcp or "")}
    out = []
    for a in cmd:
        for k, v in fill.items():
            a = a.replace(k, v)
        out.append(a.replace("{prompt}", prompt))  # prompt last, so "{model}" typed in an issue is never substituted
    return out


def agent_env(agent, mcp=None):
    env = {**os.environ, **agent.get("env", {})}
    if mcp:
        env.update({k: v.replace("{mcp}", str(mcp)) for k, v in agent.get("mcp_env", {}).items()})
    return env


def exe_path(name):
    return shutil.which(name) or (name in ALIASES and shutil.which(ALIASES[name])) or None


def safe_cmd(cmd):
    """Resolve the agent's program. npm installs CLIs on Windows as .cmd shims, and starting one goes through
    cmd.exe, which re-parses the prompt (quotes, &, |) as shell syntax: a crafted issue could run commands.
    So run the shim's node script directly instead."""
    exe = exe_path(cmd[0]) or cmd[0]
    if os.name != "nt" or not exe.lower().endswith((".cmd", ".bat")):
        return [exe, *cmd[1:]]
    m = re.search(r'"%dp0%\\([^"]+)"\s+%\*',Path(exe).read_text(encoding="utf-8", errors="replace"))
    if not m:
        raise RuntimeError(f"{exe} is a batch file agentflow can't run safely; point ~/.agentflow.toml at the real program")
    here = Path(exe).parent
    node = here / "node.exe" if (here / "node.exe").is_file() else (shutil.which("node") or "node")
    return [str(node), str(here / m.group(1)), *cmd[1:]]


def session_of(line):
    """Session id from a stream-json line (Claude Code, Cursor), used to resume the chat next turn."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    return event.get("session_id") if isinstance(event, dict) else None


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
            out.append(f"→ {part.get('name')} {PureWindowsPath(target).name if 'file_path' in args else target}".rstrip())
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
    log("status", "Agent working")
    p = subprocess.Popen(safe_cmd(build_cmd(agent, prompt)), cwd=d, env=agent_env(agent), stdin=subprocess.DEVNULL,
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


# Labels are the watch-mode trigger: only people with triage/write access can add them, so a label is a vetted request.
LABELS = {"agentflow": ("5319e7", "agentflow: issue -> PR, PR -> fix comments and CI", False),
          "agentflow-review": ("1d76db", "agentflow: post a code review on this PR", True)}


def claim_labeled(repo):
    """Open issues/PRs carrying an agentflow label, as (ref, review). Removes the label so each request runs once."""
    jobs = []
    for label, (_, _, review) in LABELS.items():
        for item in json.loads(run(["gh", "api", f"repos/{repo}/issues?labels={label}&state=open&per_page=100"])):
            run(["gh", "api", "-X", "DELETE", f"repos/{repo}/issues/{item['number']}/labels/{label}"])
            jobs.append((f"{repo}#{item['number']}", review))
    return jobs


def report_failure(ref, error):
    """Tell the requester on the issue/PR why a watched run failed; they can re-add the label to retry."""
    repo, num = parse_ref(ref)
    detail = getattr(error, "stderr", None) or str(error)
    subprocess.run(["gh", "api", f"repos/{repo}/issues/{num}/comments", "-f",
                    f"body={MARKER}\nagentflow run failed: {detail[-LOG_LIMIT:]}\n\nRe-add the label to retry."],
                   capture_output=True)


def watch(repos, launch, interval=60, log=print):
    """Poll repos forever; call launch(ref, review) for every newly labeled issue/PR."""
    for repo in repos:
        parse_ref(f"{repo}#1")
        for label, (color, desc, _) in LABELS.items():  # make sure the labels exist so people can apply them
            run(["gh", "label", "create", label, "-R", repo, "--force", "--color", color, "--description", desc])
    log(f"Watching {', '.join(repos)} every {interval}s for labels: {', '.join(LABELS)}  (Ctrl+C to stop)")
    while True:
        for repo in repos:
            try:
                jobs = claim_labeled(repo)
            except subprocess.CalledProcessError as e:  # network blip or lost access: report and keep watching
                log(f"{repo}: {(e.stderr or str(e)).strip()}")
                continue
            for ref, review in jobs:
                launch(ref, review)
        time.sleep(interval)


def add_run_options(p, agents):
    p.add_argument("--agent", choices=agents, default="claude")
    p.add_argument("--model", help="model for agents that take one, e.g. --agent ollama --model qwen3.5:9b")
    p.add_argument("--test", help="test command to run before pushing (default: auto-detect)")


def main():
    agents = load_agents()
    if sys.argv[1:2] in (["ui"], ["app"], ["watch"]):
        cmd = sys.argv[1]
        p = argparse.ArgumentParser(prog=f"agentflow {cmd}")
        p.add_argument("repos", nargs="+" if cmd == "watch" else "*", metavar="owner/repo",
                       help="repos to watch for agentflow labels" + ("" if cmd == "watch" else " (optional)"))
        add_run_options(p, agents)
        p.add_argument("--interval", type=int, default=60, help="seconds between checks (default 60)")
        a = p.parse_args(sys.argv[2:])
        try:
            agent = with_model(agents[a.agent], a.model)
        except ValueError as e:
            sys.exit(f"error: {e}")
        if cmd in ("ui", "app"):
            import agentflow_ui
            return agentflow_ui.serve(watch_repos=a.repos, agent_name=a.agent, model=a.model, test_cmd=a.test,
                                      interval=a.interval, app=cmd == "app")

        def launch(ref, review):  # one at a time, so runs never race on the same repo
            print(f"\n== {ref} ({'review' if review else 'run'})")
            try:
                resolve(ref, agent, a.test, trust=True, review=review)
            except Exception as e:  # one bad run must not stop the watcher
                print(f"error: {e}")
                report_failure(ref, e)

        try:
            watch(a.repos, launch, a.interval)
        except KeyboardInterrupt:
            pass
        return

    p = argparse.ArgumentParser(prog="agentflow",
                                epilog="Also: 'agentflow app' (desktop window) or 'agentflow ui' (web UI) [owner/repo ...], "
                                       "'agentflow watch owner/repo ...' to act on labeled issues/PRs.")
    p.add_argument("ref", help="owner/repo#number: an issue (opens a PR) or a PR (addresses new review comments)")
    add_run_options(p, agents)
    p.add_argument("--trust-author", action="store_true", help="run on an issue from a non-collaborator (read it first)")
    p.add_argument("--review", action="store_true", help="post a code review on the PR instead of changing it")
    a = p.parse_args()
    try:
        resolve(a.ref, with_model(agents[a.agent], a.model), a.test, trust=a.trust_author, review=a.review)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as e:
        sys.exit(f"error: {e}" + (f"\n{e.stderr}" if getattr(e, "stderr", None) else ""))


if __name__ == "__main__":
    main()
