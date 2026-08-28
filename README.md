# antigravity-delegate

[![tests](https://github.com/Thulnith-0/antigravity-delegate/actions/workflows/tests.yml/badge.svg)](https://github.com/Thulnith-0/antigravity-delegate/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An [MCP](https://modelcontextprotocol.io) server that lets Claude Code hand off
self-contained coding and research tasks to
[Google Antigravity](https://antigravity.google) sub-agents running locally
against a specific directory.

Claude stays on architecture, logic, and review. Antigravity does the legwork.
Because it drives the Antigravity CLI (`agy`) rather than the Gemini API, the
work bills against **the Google account you're already logged into** — an AI
Pro/Ultra subscription applies, with no separate API key.

```
Claude Code  ──MCP──▶  antigravity-delegate  ──subprocess──▶  agy  ──▶  your Antigravity account
```

---

## Table of contents

- [Requirements](#requirements)
- [Setup](#setup)
- [Verify it works](#verify-it-works)
- [Usage](#usage)
- [Tool reference](#tool-reference)
- [Permissions and safety](#permissions-and-safety)
- [Reducing Claude Code token burn](#reducing-claude-code-token-burn)
- [Configuration](#configuration)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [License](#license)

---

## Requirements

| | |
| --- | --- |
| **Python** | 3.10 or newer |
| **Antigravity CLI** (`agy`) | Installed and logged in — [install docs](https://antigravity.google/docs/cli/install/) |
| **Claude Code** | Or any other MCP client |

No Gemini API key is needed.

---

## Setup

### 1. Install the Antigravity CLI

**Windows (PowerShell):**

```powershell
irm https://antigravity.google/cli/install.ps1 | iex
```

**macOS / Linux:**

```bash
curl -fsSL https://antigravity.google/cli/install.sh | sh
```

### 2. Log in — once, interactively

```bash
agy
```

This opens your browser for Google sign-in and caches the credentials in your OS
keyring. **You must do this yourself before the server can work**; it runs `agy`
headlessly and never handles login. Confirm it worked:

```bash
agy models
```

A list of models means you're authenticated.

### 3. Clone and install

```bash
git clone https://github.com/Thulnith-0/antigravity-delegate.git
```

```bash
cd antigravity-delegate && python -m venv .venv
```

Activate the virtualenv — `.venv\Scripts\activate` on Windows,
`source .venv/bin/activate` elsewhere — then:

```bash
pip install -r requirements.txt
```

### 4. Register the server with Claude Code

Create a `.mcp.json` in the directory you want to use it from (or add the
`mcpServers` entry to your existing one). Use **absolute paths** to the
virtualenv's interpreter, so the server doesn't depend on an activated shell:

```json
{
  "mcpServers": {
    "antigravity-delegate": {
      "command": "/absolute/path/to/antigravity-delegate/.venv/bin/python",
      "args": ["/absolute/path/to/antigravity-delegate/antigravity_delegate.py"]
    }
  }
}
```

On Windows the interpreter is `.venv\Scripts\python.exe`, and backslashes must
be escaped in JSON:

```json
{
  "mcpServers": {
    "antigravity-delegate": {
      "command": "C:\\path\\to\\antigravity-delegate\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\antigravity-delegate\\antigravity_delegate.py"]
    }
  }
}
```

No `env` block and no secrets are required.

### 5. Restart Claude Code

MCP servers load at session start. **An already-running session will not pick up
the new config** — quit and reopen Claude Code.

---

## Verify it works

Ask Claude Code:

> Use delegate_to_antigravity to list what's in ./src and summarise it. Use read_only.

If you get a summary back, you're set.

To check the server independently of Claude — Windows:

```powershell
.venv\Scripts\python.exe -c "import antigravity_delegate; print('ok')"
```

macOS / Linux:

```bash
.venv/bin/python -c "import antigravity_delegate; print('ok')"
```

Verified from a clean `git clone`: clone → venv → `pip install -r
requirements.txt` → import succeeds → `pytest` passes 54 tests.

---

## Usage

Talk to Claude Code normally; it calls the tool when delegation makes sense. You
can also be explicit:

> Delegate this to Antigravity: add type hints and docstrings to every function in src/utils.py

**Good candidates** — well-scoped, mechanical, verifiable:

- Adding type hints, docstrings, or tests across a module
- Building a UI component to a written spec
- Auditing a package for a pattern (`read_only=true`)
- Repetitive refactors across many files

**Poor candidates:**

- Anything needing back-and-forth — the sub-agent cannot ask questions
- Cross-cutting architectural decisions
- Work depending on context that exists only in your Claude conversation

Each delegation starts fresh, so **task descriptions must be self-contained**.
Pass `conversation_id` from a previous result to continue where it left off.

> **Review what comes back.** Treat delegated output like a pull request from a
> capable contractor: usually good, occasionally confidently wrong.

---

## Tool reference

### `delegate_to_antigravity`

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `task` | string | *required* | Complete, self-contained task description. |
| `directory` | string | *required* | Absolute path to an existing directory to work in. |
| `allow_shell` | bool | `false` | Let the sub-agent run shell commands. **Removes directory confinement** — see below. |
| `read_only` | bool | `false` | Plan mode: investigates and writes a plan without editing files. Cannot combine with `allow_shell`. |
| `timeout_seconds` | int | `900` | Abort after this many seconds (max 3600). |
| `model` | string | account default | Model override. Run `agy models` for valid IDs. |
| `conversation_id` | string | — | Resume a previous run's context. |

**Returns** the sub-agent's final answer, followed by elapsed time, token usage,
the `conversation_id` for chaining, and any diagnostics. If the run ends in a
non-success state, a warning is placed at the **top** so a partial result isn't
mistaken for a finished one.

---

## Permissions and safety

Behaviour below was verified by running `agy` 1.1.22 directly, because its
headless-mode documentation was inconsistent with what the binary does.

### Default mode (`allow_shell=false`)

- ✅ Reads and writes files **inside** `directory`
- ✅ Out-of-workspace access is **auto-denied** (headless mode can't prompt), which
  is what effectively confines the agent
- ❌ Shell commands are auto-denied — and a task that needs one is **`CANCELED`
  outright**, producing no partial work

### `allow_shell=true` — understand this before using it

`agy` has no shell-only approval flag, so this passes
`--dangerously-skip-permissions`, which auto-approves **every** tool call.

> **⚠️ This removes directory confinement entirely.** Verified: with `--add-dir`
> pointed at directory A, `agy` was asked to overwrite a file in unrelated
> directory B and did so successfully. It can read and write anywhere your user
> account can.

**It also breaks isolation between delegations.** `agy` stores every conversation
in plaintext under `~/.gemini/antigravity-cli/` (full transcripts in
`brain/<conversation-id>/`). With `allow_shell=true`, a delegated agent will read
them: in testing, a fresh delegation asked about a fact it had no access to spent
58 steps searching the filesystem, found an unrelated earlier delegation's
transcript, and answered from it. The same probe with `allow_shell=false`
correctly answered "UNKNOWN".

So anything sent to one delegation may surface in a later, unrelated one —
including across different projects.

**Recommendations:**

1. Default to `allow_shell=false`; many file-editing tasks complete fine.
2. Use `read_only=true` for research and review.
3. Enable `allow_shell` only for directories you'd be comfortable granting full
   account access to. Not your home directory, not repos holding credentials.
4. Clear `~/.gemini/antigravity-cli/brain/` between sensitive runs.

### When is `allow_shell` actually needed?

Not always — it correlates with task size more than task type. Measured:

| Task | `allow_shell=false` |
| --- | --- |
| Create a single file | ✅ Succeeded |
| Add type hints + docstrings across 2 functions | ✅ Succeeded |
| Read-only research question | ✅ Succeeded |
| Fix 3 bugs across a module | ❌ `CANCELED` |
| Build a whole UI feature in one call | ❌ `CANCELED` |

Larger, multi-step tasks tend to reach for a command (to verify their own work),
and that single denial cancels the entire run. **Decomposing a big task into
per-file calls often keeps it inside the safe mode** as well as making it faster.

### Changing the default

If your workflow genuinely needs shell most of the time, set it once for your
own machine rather than passing the flag on every call:

```json
{
  "mcpServers": {
    "antigravity-delegate": {
      "command": "…/.venv/bin/python",
      "args": ["…/antigravity_delegate.py"],
      "env": { "ANTIGRAVITY_ALLOW_SHELL": "true" }
    }
  }
}
```

The server logs a warning at startup when this is on. Individual calls still
override it — `allow_shell=false` opts back out, and `read_only=true` continues
to work normally rather than conflicting with the default.

The shipped default stays `false` deliberately: this flag grants
filesystem-wide access, and nobody should acquire that by installing a tool
without reading this section.

---

## Reducing Claude Code token burn

This is the main reason to use this server. Claude Code's context is a scarce,
metered resource; your Antigravity subscription is a flat-rate one you have
already paid for. Delegation **moves work from the scarce budget to the
flat-rate one**.

The saving on Claude's side is real and large, because delegating replaces the
two things that consume context fastest:

| Doing it in Claude Code | Delegating |
| --- | --- |
| Read every relevant file into context | Write one task description |
| Emit every line of code as output tokens | Read one summary back |
| Re-read files and iterate on errors | (happens inside Antigravity) |

Claude keeps the work that actually needs its context — architecture, the
existing codebase's logic, reviewing what comes back. Antigravity absorbs the
token-heavy legwork.

### Measured: a real UI feature

Building a responsive pricing section (3 tier cards, CSS Grid, dark mode,
accessible monthly/yearly toggle with live price updates) into a bare static
site — 18.9 KB of HTML/CSS/JS across three files, verified working in a real
browser:

| | Claude Code tokens | Antigravity tokens |
| --- | --- | --- |
| **Delegated** (4 calls: prompts + results) | **~2,800** | 412,842 |
| **Written by Claude directly** (estimated) | ~15,000–25,000 | 0 |
| **Effect** | **~5–9× less Claude context** | paid by your subscription |

The Claude-side figure is exact: 5,979 characters of task descriptions sent and
5,275 characters of results read. The "written directly" figure is an estimate —
4,729 output tokens for the code itself, plus reading the files, plus the
reasoning and iteration a UI task normally takes.

### The honest catch

Those 412K Antigravity tokens bought 18.9 KB of code, and **only ~54% of that
spend produced anything**:

| Run | Tokens | Outcome |
| --- | --- | --- |
| Default mode (`allow_shell=false`) | 39,887 | ❌ `CANCELED` — needed a shell, auto-denied, zero output |
| `allow_shell=true`, whole task at once | 97,837 | ❌ Timed out at 500s — wrote `index.html` only, left the site broken |
| Resumed via `conversation_id`, `styles.css` only | 148,443 | ✅ Succeeded in 54s |
| Fresh conversation, `app.js` only | 126,675 | ✅ Succeeded in 72s |

Two lessons, both learned the expensive way:

1. **Decompose UI tasks by file.** The whole feature at once timed out; one file
   per call succeeded in about a minute each.
2. **Resuming carries the failure's cost.** Run 3 resumed the timed-out
   conversation and inherited its history — 148K tokens for one CSS file. The
   fresh run in step 4 did comparable work for less. Start fresh unless you
   genuinely need the prior context.

### Per-task reference

| Task | Tokens |
| --- | --- |
| Write a one-line file | ~25K |
| Add type hints + docstrings to 2 small functions | ~49K |
| Fix 3 bugs in a 20-line module | ~208K |
| One file of a UI feature (scoped well) | ~127K |
| A run that timed out without finishing | ~220K |

Keep `timeout_seconds` tight so failures fail fast and cheap.

### About `conversation_id`

Resuming is for **continuity, not savings**. Measured both directions:

| Follow-up | Resumed | Fresh | Effect |
| --- | --- | --- | --- |
| Question about an already-read file | 36.8K | 30.2K | **+22% worse** |
| Recall a fact not derivable from disk | 20.8K | 167.6K | **−88% better** |

Use it when restating context would be long or error-prone. Omit it for
independent tasks.

---

## Configuration

Optional environment variables, set in the server's `env` block:

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTIGRAVITY_ALLOW_SHELL` | `false` | Default for `allow_shell`. **Read [Permissions and safety](#permissions-and-safety) before enabling.** |
| `ANTIGRAVITY_TIMEOUT_SECONDS` | `900` | Default run timeout. |
| `ANTIGRAVITY_MODEL` | — | Default model for every run. |
| `ANTIGRAVITY_LOG_LEVEL` | `INFO` | Server log verbosity (stderr only). |
| `ANTIGRAVITY_CLI_PATH` | — | Explicit path to `agy` if it isn't on `PATH`. |
| `ANTIGRAVITY_CLI_TIMEOUT_BUFFER_SECONDS` | `30` | Grace period before force-killing a hung `agy`. |

---

## Troubleshooting

**The tool doesn't appear in Claude Code.**
Restart Claude Code — servers load at session start. Verify your `.mcp.json` is
valid JSON with correctly escaped backslashes, and that both absolute paths exist.

**`Could not find the Antigravity CLI ('agy')`.**
`agy` isn't on `PATH`. Set `ANTIGRAVITY_CLI_PATH` to its full path — on Windows,
usually `C:\Users\<you>\AppData\Local\agy\bin\agy.exe`.

**`authentication required`, or empty responses.**
Your `agy` login expired. Run `agy` interactively to sign in again, then confirm
with `agy models`.

**The run returns `CANCELED` with a "permission" diagnostic.**
The task needed a shell command, which is auto-denied in default mode. Either
rewrite the task to avoid shell, or pass `allow_shell=true` after reading
[Permissions and safety](#permissions-and-safety).

**Runs time out.**
Raise `timeout_seconds` (max 3600), or split the task. Anything already written
to disk stays there.

---

## Development

```bash
pip install -r requirements-dev.txt
```

```bash
pytest
```

38 tests cover validation, argv construction, subprocess handling, result
formatting, and the MCP tool boundary. The `agy` binary is replaced by a stand-in
(`tests/fakes/fake_agy.py`), so the suite makes no live calls and needs no
credentials or Antigravity install. CI runs it on Python 3.10–3.12.

```
antigravity_delegate.py   # the server (single module)
tests/
  test_server.py
  fakes/fake_agy.py       # stand-in binary for tests
```

---

## License

[MIT](LICENSE)
