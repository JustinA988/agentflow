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

## Fix a PR from review comments

Point agentflow at a PR instead of an issue:

```bash
agentflow owner/repo#45
```

It checks out the PR branch, gives the agent every review comment posted since the PR's last commit, runs the tests,
pushes, marks the PR draft or ready based on the test result, and comments with a summary. Only comments from the
repo's owner, members and collaborators are used, so strangers can't steer the agent. PRs from forks aren't supported yet.

## Web UI

```bash
agentflow ui
```

Open http://localhost:8765 to start runs and watch progress, agent output, a color-coded diff, test results and
PR links. Run history is saved to `~/.agentflow-history.json`. The server only listens on 127.0.0.1.

## Custom agents and local models

Add any CLI that edits files in the current directory to `~/.agentflow.toml`. `{prompt}` is replaced with the issue text.

```toml
# Claude Code driving a local Ollama model (no login, no key)
[agents.local]
cmd = ["claude", "-p", "{prompt}", "--permission-mode", "acceptEdits", "--model", "qwen3.5:9b"]
env = { ANTHROPIC_BASE_URL = "http://localhost:11434", ANTHROPIC_AUTH_TOKEN = "ollama", ANTHROPIC_API_KEY = "" }

[agents.gemini]
cmd = ["gemini", "-p", "{prompt}", "--yolo"]
```

Then `agentflow owner/repo#123 --agent local`.

## Roadmap
- PR review, fix PR feedback, CI fix-up
