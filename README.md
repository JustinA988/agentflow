# agentflow

Issue in, PR out. Uses an AI CLI you're already logged in to (Claude Code, Codex) or a local model, so **no API keys**.

Needs: Python 3.11+, `git`, `gh` (authenticated), and an agent CLI installed and signed in.

```bash
pip install -e .
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

## Web UI

```bash
agentflow ui
```

Open http://localhost:8765 to start runs and watch progress, agent output, a color-coded diff, test results and
PR links. Run history is saved to `~/.agentflow-history.json`. The server only listens on 127.0.0.1.

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

## Roadmap
- PR review, fix PR feedback, CI fix-up
