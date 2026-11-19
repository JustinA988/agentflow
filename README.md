# agentflow

One app to chat with any AI coding agent about a GitHub repo or a folder on your PC, and turn the result into a PR.
It drives the AI CLIs you're already signed in to (Claude Code, Codex, Cursor, Gemini, Qwen, Aider, OpenCode,
Copilot) or a local model, so **no API keys**.

Needs: Python 3.11+, `git`, `gh` (authenticated), and at least one agent CLI installed and signed in.

```bash
pip install git+https://github.com/JustinA988/agentflow
agentflow app
```

## The app

A dark chat window, like Claude or ChatGPT:

- **Pick a project**: one of your GitHub repos (agentflow works on its own copy, on its own branch) or any folder on
  your PC (the agent edits those files directly).
- **Pick an agent** and, optionally, a model. Switch any time, even mid-chat.
- **Chat.** The agent reads and edits code and replies; follow-up messages keep the conversation's context.
  You see its steps live, every change as a color-coded diff, and a Stop button.
- **Create PR** (GitHub chats): commits, runs the tests, pushes and opens the PR (a draft if tests fail).
  Later changes go to the same PR with **Push changes**.
- **`/issue 12`, `/fix 13`, `/review 13`** in a GitHub chat run the one-shot automations below.
- **Settings → MCP servers**: paste or drop the JSON from any MCP server's README, and switch each server on or off.
  Turned-on servers are handed to Claude, Ollama, Gemini, Qwen and Copilot. Cursor, Codex, Aider and OpenCode keep
  using their own MCP settings.
- **Settings → Agents** shows which agents are installed, and the install command for the rest.

Chats are saved in `~/.agentflow/`. Closing the window stops agentflow (after any running agent finishes); opening it
again while it runs just opens another window. It's an Edge/Chrome app window, so there's nothing else to install.
`agentflow ui` serves the same thing at http://localhost:8765 (127.0.0.1 only) for any browser.

Windows Desktop shortcut that starts it with no console window (run once in PowerShell):

```powershell
$s = (New-Object -ComObject WScript.Shell).CreateShortcut("$env:USERPROFILE\Desktop\agentflow.lnk")
$s.TargetPath = "conhost.exe"; $s.Arguments = "--headless python -m agentflow app"; $s.Save()
```

| Agent | Install | Signs in with |
|---|---|---|
| `claude` | `npm i -g @anthropic-ai/claude-code` | your Claude plan |
| `codex` | `npm i -g @openai/codex` | your ChatGPT plan |
| `cursor` | [cursor.com/cli](https://cursor.com/cli) | your Cursor plan |
| `gemini` | `npm i -g @google/gemini-cli` | your Google account |
| `qwen` | `npm i -g @qwen-code/qwen-code` | your Qwen account |
| `aider` | `python -m pip install aider-install && aider-install` | any model you configure |
| `opencode` | `npm i -g opencode-ai` | any provider |
| `copilot` | `npm i -g @github/copilot` | your Copilot plan |
| `ollama` | Claude Code + [Ollama](https://ollama.com) | nothing: local and free |

## Issue to PR from the command line

```bash
agentflow owner/repo#123 --agent claude
agentflow owner/repo#123 --test "npm run test:unit"
```

It clones the repo to a temp dir, branches, runs the agent on the issue, runs the tests, commits, pushes, and opens a PR.
If tests fail, the PR opens as a **draft** with the failure log. Without `--test`, the test command is auto-detected
(npm, cargo, go, pytest); if none is found the PR says tests were not run.

Issues written by someone without write access to the repo are refused, since anyone can open an issue on a
public repo and the agent edits files and pushes under your account. Read the issue first, then pass
`--trust-author` (in the app: `/issue 123 --trust`) to run it anyway.

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
the agent fixes what it found. In the app: `/review 45`.

## Watch mode: label it and walk away

```bash
agentflow watch owner/repo [owner/repo ...] [--agent ollama --model qwen3.5:9b] [--interval 60]
agentflow app owner/repo           # same, each labeled item shows up as a chat
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

## Local models (free, offline)

Runs entirely on your machine with [Ollama](https://ollama.com): no subscription, no key, no internet.
Claude Code does the file editing; your local model does the thinking.

```bash
ollama pull qwen3.5:9b
agentflow owner/repo#123 --agent ollama --model qwen3.5:9b
```

In the app, pick `ollama` and type the model name. Any Ollama model with tool calling works; bigger models make
fewer mistakes. It uses `claude --bare`, so your global Claude Code setup (CLAUDE.md, hooks, plugins) stays out of the
way, and the model only gets file tools (no shell).

## Custom agents

Add any CLI that edits files in the current directory to `~/.agentflow.toml`, or override a built-in one.
`{prompt}` is replaced with the message, `{model}` with the model. Optional: `model_args` (added when a model is
picked), `resume` (with `{session}`), `mcp` / `mcp_env` (with `{mcp}`, the path to the enabled MCP servers file).

```toml
[agents.mycli]
cmd = ["mycli", "--yes", "-p", "{prompt}"]
model_args = ["--model", "{model}"]

[agents.lmstudio]  # any Anthropic-compatible local server
cmd = ["claude", "--bare", "-p", "{prompt}", "--model", "{model}", "--permission-mode", "acceptEdits"]
env = { ANTHROPIC_BASE_URL = "http://localhost:1234", ANTHROPIC_API_KEY = "local" }
```

Then `agentflow owner/repo#123 --agent mycli`, or pick it in the app.

## Safety

The agent edits files with your permissions and agentflow pushes with your `gh` login, so treat every run like
giving someone your keyboard:

- Issue authors and PR commenters without write access are ignored unless you opt in (`--trust-author`).
- Claude Code runs with `acceptEdits`; Codex runs with `--full-auto` (its sandbox). Pick agents you trust.
- GitHub chats and automations work in agentflow's own clone, never your checkout. PRs from forks are refused.
- **Folder chats edit your files directly**, like running the agent in that folder yourself. Commit first if you want
  an easy undo; the Changes view shows everything changed since the chat began.
- On Windows, npm-installed agents are started without `cmd.exe`, so quotes, `&` or `|` in a message can't run commands.
- The app listens on 127.0.0.1 only and rejects other Host headers and non-JSON posts.

## Limits

PRs from forks aren't supported. Pending CI checks are ignored (only finished failures count). Diffs over 40 KB are
cut short for review. Only Claude, Ollama and Cursor resume a chat's session; other agents get the recent conversation
(last ~12,000 characters) replayed with each message.

## Development

```bash
pip install pytest -e .
python -m pytest
```
