# agentflow

Issue in, PR out. Uses an AI CLI you're already logged in to (Claude Code, Codex), so **no API keys**.

Needs: `git`, `gh` (authenticated), and `claude` and/or `codex` installed and signed in.

```bash
pip install -e .
agentflow owner/repo#123 --agent claude
```

It clones the repo to a temp dir, branches, runs the agent on the issue, commits, pushes, and opens a PR.

## Roadmap
- More agents and local models (Ollama etc.) via config
- PR review, triage, CI fix-up
