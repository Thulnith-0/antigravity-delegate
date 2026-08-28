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

```json
{
  "mcpServers": {
    "antigravity-delegate": {
      "command": "/absolute/path/to/repo/.venv/bin/python",
      "args": ["/absolute/path/to/repo/antigravity_mcp.py"],
      "env": {
        "GEMINI_API_KEY": "your-key-here"
      }
    }
  }
}
```

On Windows the interpreter is `.venv\Scripts\python.exe`, and backslashes must be
escaped in JSON (`C:\\path\\to\\repo\\.venv\\Scripts\\python.exe`).

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
