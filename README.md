# antigravity-delegate

An [MCP](https://modelcontextprotocol.io) server that lets an AI assistant hand
off self-contained coding and research tasks to
[Google Antigravity](https://pypi.org/project/google-antigravity/) sub-agents
running locally against a specific directory.

The assistant calls one tool, `delegate_to_antigravity`, with a task and a target
directory. The server starts an Antigravity `Agent` sandboxed to that directory,
runs the task to completion, and returns the sub-agent's final report along with
a trace of what it actually did.

## Requirements

- Python 3.10 or newer
- A Gemini API key ([AI Studio](https://aistudio.google.com/apikey)), or a Vertex
  AI / Gemini Enterprise project

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

## The tool

`delegate_to_antigravity(task, directory, ...)`

| Parameter | Default | Purpose |
| --- | --- | --- |
| `task` | *required* | The complete task description. The sub-agent cannot ask follow-up questions, so state what to do, what "done" means, and any constraints. |
| `directory` | *required* | Absolute path to an existing directory. The sub-agent's file tools are confined to it. |
| `allow_shell` | `false` | Let the sub-agent run shell commands without confirmation (needed for builds, tests, linters). |
| `read_only` | `false` | Restrict the sub-agent to read-only tools. For research and code review. Cannot be combined with `allow_shell`. |
| `timeout_seconds` | `900` | Abort the run after this many seconds (max 3600). |
| `model` | SDK default | Optional Gemini model override. |

Returns the sub-agent's final answer, followed by a footer listing tool calls,
paths touched, elapsed time and token usage. If the run stopped early (token or
tool-call limits, quota exhaustion) a truncation warning is placed at the top, so
a partial result is not mistaken for a finished one.

### Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `GEMINI_API_KEY` / `GOOGLE_API_KEY` | — | Gemini API credential. |
| `ANTIGRAVITY_TIMEOUT_SECONDS` | `900` | Default run timeout. |
| `ANTIGRAVITY_MODEL` | — | Default model for every run. |
| `ANTIGRAVITY_LOG_LEVEL` | `INFO` | Server log verbosity (stderr only). |

## Permissions model

Two independent limits apply to every run:

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

> **`allow_shell=true` is genuine autonomous execution.** The sub-agent runs
> commands in the target directory without confirmation. The file sandbox still
> applies, but a shell can reach past it. Leave it off for directories that matter
> until you have watched a few runs.

## Tests

```bash
pip install -r requirements-dev.txt
```

```bash
pytest
```

The suite covers validation, configuration, formatting and the MCP tool boundary.
It makes no live API calls, so it runs without credentials.

## License

See [LICENSE](LICENSE).
