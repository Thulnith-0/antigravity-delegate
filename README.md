# antigravity-delegate

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant hand
off self-contained coding and research tasks to
[Google Antigravity](https://antigravity.google) sub-agents running locally
against a specific directory.

It exposes two tools with the same contract -- a task and a target directory in,
a final report out -- backed by two different ways of reaching Antigravity:

| Tool | Backend | Billing |
| --- | --- | --- |
| `delegate_to_antigravity` | the [google-antigravity](https://pypi.org/project/google-antigravity/) Python SDK | a standalone Gemini API key / GCP project |
| `delegate_to_antigravity_cli` | the [`agy` CLI](https://antigravity.google/docs/cli/) as a subprocess | whatever Google account `agy` is logged into (e.g. an AI Pro/Ultra subscription) |

A Google AI Pro/Ultra subscription does **not** carry over to the SDK path --
Google bills direct API/SDK use separately from the subscribed surfaces (the
Antigravity desktop app, IDE, and CLI). If you want to spend your subscription
rather than a metered API key, use `delegate_to_antigravity_cli`.

## Requirements

Common to both tools:
- Python 3.10 or newer

For `delegate_to_antigravity` (SDK):
- A Gemini API key ([AI Studio](https://aistudio.google.com/apikey)), or a Vertex
  AI / Gemini Enterprise project

For `delegate_to_antigravity_cli` (CLI):
- The [Antigravity CLI](https://antigravity.google/docs/cli/install/) (`agy`)
  installed and logged in. Run `agy` once, interactively, yourself to complete
  the browser login -- this server never installs `agy` or handles that login.

## Install

```bash
python -m venv .venv
```

Activate it (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate`
elsewhere), then:

```bash
pip install -r requirements.txt
```

Install `google-antigravity` from PyPI rather than from a git clone — the SDK
ships a compiled runtime binary that only the published wheels contain.

## Configure your MCP client

Add the server to your client's MCP config, using the **absolute path** to the
virtualenv's interpreter so the server does not depend on an activated shell.
For Claude Code, a `.mcp.json` in the project root works:

```json
{
  "mcpServers": {
    "antigravity-delegate": {
      "command": "/absolute/path/to/repo/.venv/bin/python",
      "args": ["/absolute/path/to/repo/antigravity_mcp.py"]
    }
  }
}
```

That block needs **no secrets at all** -- `delegate_to_antigravity_cli` uses
whatever account `agy` is logged into. Add an `env` block with `GEMINI_API_KEY`
only if you also want the SDK-backed tool (see
[mcp_config.example.json](mcp_config.example.json)); without it that one tool
reports missing credentials and the CLI tool still works.

On Windows the interpreter is `.venv\Scripts\python.exe`, and backslashes must be
escaped in JSON (`C:\\path\\to\\repo\\.venv\\Scripts\\python.exe`).

MCP servers are loaded when a session starts, so **start a new client session**
after editing the config -- an already-running one will not pick it up.

> **Keep your key out of git.** The config above holds a live credential. Store it
> in your client's own config file outside this repo, or in an untracked file —
> `.gitignore` already excludes `.env` and `claude_mcp_config.json`.

To use Vertex AI / Gemini Enterprise instead of an API key, drop `GEMINI_API_KEY`
and set `GOOGLE_GENAI_USE_VERTEXAI=true`, `GOOGLE_CLOUD_PROJECT` and
`GOOGLE_CLOUD_LOCATION`, then run `gcloud auth application-default login`.

Neither tool needs its credential configured to use the *other* one -- e.g. you
can run `delegate_to_antigravity_cli` with no `GEMINI_API_KEY` set at all, as
long as `agy` is installed and logged in.

## The tools

Both take the same core arguments:

| Parameter | Default | Purpose |
| --- | --- | --- |
| `task` | *required* | The complete task description. The sub-agent cannot ask follow-up questions, so state what to do, what "done" means, and any constraints. |
| `directory` | *required* | Absolute path to an existing directory. The sub-agent's file tools are confined to it. |
| `allow_shell` | `false` | Let the sub-agent run shell commands without confirmation (needed for builds, tests, linters). |
| `read_only` | `false` | Restrict the sub-agent so it cannot modify `directory`. For research and code review. Cannot be combined with `allow_shell`. |
| `timeout_seconds` | `900` | Abort the run after this many seconds (max 3600). |
| `model` | backend default | Optional model override. |

`delegate_to_antigravity_cli` additionally takes `conversation_id`, to resume a
previous run's context. Every result reports the `conversation_id` to chain from.

Both return the sub-agent's final answer followed by a footer (elapsed time,
token usage, and backend-specific diagnostics), and both surface an early-stop
or non-success status as a warning at the *top* of the reply, so a partial
result is never mistaken for a finished one.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | Gemini API credential (SDK tool only). |
| `ANTIGRAVITY_TIMEOUT_SECONDS` | `900` | Default run timeout for both tools. |
| `ANTIGRAVITY_MODEL` | — | Default model for both tools. |
| `ANTIGRAVITY_LOG_LEVEL` | `INFO` | Server log verbosity (stderr only). |
| `ANTIGRAVITY_CLI_PATH` | — | Explicit path to the `agy` executable (CLI tool only). Set this if `agy` isn't on `PATH` and isn't at the default Windows install location. |
| `ANTIGRAVITY_CLI_TIMEOUT_BUFFER_SECONDS` | `30` | Extra grace period beyond `timeout_seconds` before force-killing a hung `agy` process (CLI tool only). |

## Permissions model

### `delegate_to_antigravity` (SDK)

1. **Directory sandbox.** `workspaces` is set to the target directory, and the
   SDK's policy evaluator confines the file tools to it.
2. **Shell access.** Off unless `allow_shell=true`. The SDK's default
   `confirm_run_command()` policy denies `run_command` outright when no human is
   available to confirm — which is always the case inside an MCP server — so shell
   calls fail fast with a clear message instead of hanging.

`read_only=true` narrows the tool set to an explicit allowlist
(`BuiltinTools.read_only()` plus `search_web`), disables sub-agent spawning, and
adds deny policies for `create_file`, `edit_file` and `run_command` as a second
layer.

### `delegate_to_antigravity_cli` (CLI)

Verified empirically against `agy` 1.1.22 (its headless-mode documentation was
inconsistent, so this is what the binary actually does, not what the docs say):

1. **Directory scoping is permission-gated, not a hard sandbox.** The target
   directory is passed as `--add-dir`. Access to paths *outside* it raises a
   permission request, and because headless mode cannot prompt, that request is
   auto-denied -- so in default mode the agent is effectively confined
   (verified: a write to an outside path was refused, and the file was
   untouched). The confinement comes from the auto-denial, **not** from
   `--add-dir` enforcing a path boundary.
2. **Shell access.** In headless mode `agy` **auto-denies shell commands** with
   a clear stderr message rather than hanging -- but it **auto-approves file
   writes inside the workspace** with no opt-in needed. This is more permissive
   than the SDK tool's default.
3. `read_only=true` passes `--mode plan`: `agy` investigates and writes an
   implementation plan instead of editing files in `directory` (verified: no
   file was created in the target directory under plan mode). Note it still
   writes the plan itself under `~/.gemini/antigravity-cli/brain/`, outside the
   workspace.

> ### ⚠️ `allow_shell=true` removes the directory boundary entirely
>
> `agy` has no shell-only approval flag, so `allow_shell=true` passes
> `--dangerously-skip-permissions`, which auto-approves **every** tool call --
> including the out-of-workspace file access that would otherwise be
> auto-denied.
>
> **This is verified, not theoretical:** with the flag set and `--add-dir`
> pointed at directory A, `agy` was asked to overwrite a file in unrelated
> directory B and did so successfully. With `allow_shell=true`, the CLI tool
> has **no directory confinement at all** -- it can read and write anywhere the
> user account can, and run arbitrary shell commands.
>
> Treat it as "give this agent my user account", not as "let it run tests in
> this folder". The SDK tool (`delegate_to_antigravity`) keeps its
> policy-enforced `workspaces` boundary for file tools even when
> `allow_shell=true`, and is the safer choice if you need shell access.

### The practical tradeoff (measured, not theoretical)

`allow_shell` is not a fine-tuning knob on this tool -- it decides whether real
work is possible at all. Measured on the same three-bug refactoring task:

| `allow_shell` | Outcome | Cost |
| --- | --- | --- |
| `false` | **Failed.** `agy` reached for a shell command during normal work, headless mode auto-denied it, and the entire run was `CANCELED` -- no edits, no output, no partial progress. | ~81K tokens, wasted |
| `true` | **Succeeded.** All three bugs fixed correctly (verified by independent behavioral tests), no stray files. | ~208K tokens |

So on the CLI tool the choice is effectively:

- `allow_shell=false` -- safe, but unable to finish most real coding tasks. Fine
  for read-only research and small single-file edits that never need a command.
- `allow_shell=true` -- capable, but with **no directory confinement at all**.

If you need both capability *and* a file sandbox, use the SDK tool
(`delegate_to_antigravity`), which enforces `workspaces` independently of shell
access -- at the cost of needing a separate API key instead of your
subscription.

### Cost

Delegated runs are not cheap, and they bill against whichever account the tool
uses. Observed: ~25K tokens for a one-line file write, ~208K for the small
refactor above, and ~220K for a single run that timed out without finishing.
Budget accordingly, and prefer `timeout_seconds` low enough to fail fast.

### Resuming conversations (`conversation_id`)

Verified working end to end: a resumed run recalls the earlier run's context,
and every result reports its `conversation_id` so you can chain follow-ups.

**Resuming is for continuity, not for saving tokens.** The cost effect is
task-dependent and can go either way:

| Follow-up task | Resumed | Fresh | Effect |
| --- | --- | --- | --- |
| Answer a question about a file already read | 36.8K | 30.2K | **+22% worse** -- carried history costs more than re-reading |
| Recall a fact with nothing on disk to re-derive it from | 20.8K | 167.6K | **-88% better** -- the fresh run burned 127s hunting for the answer |

Use it when restating context would be long or error-prone (conventions,
decisions, a running refactor). Omit it for independent tasks, where a fresh
conversation is usually cheaper.

### Stored transcripts and cross-delegation leakage

`agy` writes every conversation to `~/.gemini/antigravity-cli/` in plaintext --
full transcripts under `brain/<conversation-id>/` and a `conversations/*.db`.
These persist indefinitely and are not cleaned up by this server.

**With `allow_shell=true`, a delegated agent can and will read them.** Verified:
a fresh delegation, asked about a fact it had no legitimate access to, spent 58
steps searching the filesystem, found a *previous, unrelated* delegation's
transcript, and answered from it -- including a fact from a different test run
entirely.

The same probe with `allow_shell=false` correctly answered "UNKNOWN", because
out-of-workspace reads are auto-denied.

So with `allow_shell=true` there is no isolation between delegations: anything
sent to one delegation may surface in a later, unrelated one, including across
different projects. If you delegate anything sensitive, either keep
`allow_shell=false` or clear `~/.gemini/antigravity-cli/brain/` between runs.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
pytest
```

The suite covers validation, configuration, formatting and the MCP tool boundary
for both tools. The CLI tool's tests run against a small stand-in script
(`tests/fakes/fake_agy.py`) instead of the real `agy` binary. Nothing in the
suite makes a live call, so it runs without credentials or `agy` installed.

## License

See [LICENSE](LICENSE).
