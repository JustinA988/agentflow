# agentflow

Issue in, PR out. Uses an AI CLI you're already logged in to (Claude Code, Codex) or a local model, so **no API keys**.

Needs: Python 3.11+, `git`, `gh` (authenticated), and an agent CLI installed and signed in.

```bash
pip install git+https://github.com/JustinA988/agentflow
agentflow owner/repo#123 --agent claude
agentflow owner/repo#123 --test "npm run test:unit"
```

It clones the repo to a temp dir, branches, runs the agent on the issue, runs the tests, commits, pushes, and opens a PR.
If tests fail, the PR opens as a **draft** with the failure log. Without `--test`, the test command is auto-detected
(npm, cargo, go, pytest); if none is found the PR says tests were not run.

Issues written by someone without write access to the repo are refused, since anyone can open an issue on a
public repo and the agent edits files and pushes under your account. Read the issue first, then pass
`--trust-author` (or tick **Trust author** in the web UI) to run it anyway.

## Fix a PR from review comments and failing CI

Point agentflow at a PR instead of an issue:

```bash
agentflow owner/repo#45
```

It checks out the PR branch, gives the agent every review comment posted since the PR's last commit plus the logs of
any failing checks (GitHub Actions logs are fetched; other CI systems get a link), runs the tests,
pushes, marks the PR draft or ready based on the test result, and comments with a summary. Only comments from the
repo's owner, members and collaborators are used, so strangers can't steer the agent. PRs from forks aren't supported yet.

## Review a PR

```bash
agentflow owner/repo#45 --review
```

The agent reads the diff and surrounding code in a throwaway checkout and posts a review on the PR (bugs, risks,
suggestions). Nothing is pushed. Run `agentflow owner/repo#45` afterwards and the review counts as feedback, so
the agent fixes what it found. In the web UI, tick **Review only**.

## Watch mode: label it and walk away

```bash
agentflow watch owner/repo [owner/repo ...] [--agent ollama --model qwen3.5:9b] [--interval 60]
agentflow ui owner/repo            # same, with runs showing up in the web UI
```

agentflow creates two labels in each watched repo and checks every `--interval` seconds:

| Label | On an issue | On a PR |
|---|---|---|
| `agentflow` | opens a PR | fixes new review comments and failing CI |
| `agentflow-review` | (ignored) | posts a code review |

The label is removed when a run starts, so each request runs once; re-add it to run again. If a run fails,
agentflow comments the error on the issue/PR. Only people with triage or write access can add labels, so a
labeled issue runs even if a stranger wrote it: adding the label is your OK. It runs on your machine using your
logins, so it only works while your computer is on.

## Web UI

```bash
agentflow ui
```

Open http://localhost:8765 to start runs and watch progress, agent output, a color-coded diff, test results and
PR links. Run history is saved to `~/.agentflow-history.json`. The server only listens on 127.0.0.1.

## Desktop app

```bash
agentflow app [owner/repo ...]
```

Same UI in its own window (an Edge/Chrome app window, so nothing extra to install). Closing the window stops
agentflow, after any run in progress finishes. Opening it again while it's running just opens another window.

Windows Desktop shortcut that starts it with no console window (run once in PowerShell):

```powershell
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$env:USERPROFILE\Desktop\agentflow.lnk")
$s.TargetPath = "conhost.exe"; $s.Arguments = "--headless python -m agentflow app"; $s.Save()
```

## Local models (free, offline)

Runs entirely on your machine with [Ollama](https://ollama.com): no subscription, no key, no internet.
Claude Code does the file editing; your local model does the thinking.

```bash
ollama pull qwen3.5:9b
agentflow owner/repo#123 --agent ollama --model qwen3.5:9b
```

In the web UI, pick `ollama` and type the model name. Any Ollama model with tool calling works; bigger models make
fewer mistakes. It uses `claude --bare`, so your global Claude Code setup (CLAUDE.md, hooks, plugins) stays out of the
way, and the model only gets file tools (no shell).

## Custom agents

Add any CLI that edits files in the current directory to `~/.agentflow.toml`. `{prompt}` is replaced with the issue
text and `{model}` with `--model`.

```toml
[agents.gemini]
cmd = ["gemini", "-p", "{prompt}", "--yolo"]

[agents.lmstudio]  # any Anthropic-compatible local server
cmd = ["claude", "--bare", "-p", "{prompt}", "--model", "{model}", "--permission-mode", "acceptEdits"]
env = { ANTHROPIC_BASE_URL = "http://localhost:1234", ANTHROPIC_API_KEY = "local" }
```

Then `agentflow owner/repo#123 --agent gemini`.

## Safety

The agent edits files with your permissions and agentflow pushes with your `gh` login, so treat every run like
giving someone your keyboard:

- Issue authors and PR commenters without write access are ignored unless you opt in (`--trust-author`).
- Claude Code runs with `acceptEdits`; Codex runs with `--full-auto` (its sandbox). Pick agents you trust.
- Work happens in a temp clone, never in your own checkout. PRs from forks are refused.
- The web UI listens on 127.0.0.1 only and rejects other Host headers and non-JSON posts.

## Limits

PRs from forks aren't supported. Pending CI checks are ignored (only finished failures count). Diffs over 40 KB are
cut short for review. The web UI lists the newest 50 issues and 50 PRs.

## Development

```bash
pip install pytest -e .
python -m pytest
```
