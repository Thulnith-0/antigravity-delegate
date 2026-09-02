# antigravity-delegate

[![tests](https://github.com/Thulnith-0/antigravity-delegate/actions/workflows/tests.yml/badge.svg)](https://github.com/Thulnith-0/antigravity-delegate/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

A [Model Context Protocol](https://modelcontextprotocol.io) server that allows
Claude Code to delegate coding and research tasks to
[Google Antigravity](https://antigravity.google) sub-agents running locally
against a specified directory.

The server drives the Antigravity CLI (`agy`) as a subprocess rather than calling
the Gemini API. Work therefore bills against the Google account that `agy` is
already signed in to, so an AI Pro or Ultra subscription applies and no separate
API key is required.

```
Claude Code  --MCP-->  antigravity-delegate  --subprocess-->  agy  -->  Antigravity account
```

## Contents

- [Motivation](#motivation)
- [Requirements](#requirements)
- [Installation](#installation)
- [Verification](#verification)
- [Usage](#usage)
- [Tool reference](#tool-reference)
- [Configuration](#configuration)
- [Security model](#security-model)
- [Performance and cost](#performance-and-cost)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Compatibility](#compatibility)
- [License](#license)

## Motivation

Claude Code's context window is a metered resource. An Antigravity subscription
is a flat-rate one. Delegation moves token-heavy work from the former to the
latter.

Delegating replaces the two operations that consume context fastest: reading
source files into context, and emitting generated code as output tokens. Claude
retains the work that genuinely requires its context, such as architectural
decisions, reasoning about existing code, and reviewing returned output.
Antigravity absorbs the mechanical implementation work.

Measured results are given in [Performance and cost](#performance-and-cost).

## Requirements

| Component | Requirement |
| --- | --- |
| Python | 3.10 or newer |
| Antigravity CLI (`agy`) | Installed and authenticated. See [installation docs](https://antigravity.google/docs/cli/install/). |
| MCP client | Claude Code, or any other MCP-compatible client |

No Gemini API key is required.

## Installation

### 1. Install the Antigravity CLI

Windows (PowerShell):

```powershell
irm https://antigravity.google/cli/install.ps1 | iex
```

macOS and Linux:

```bash
curl -fsSL https://antigravity.google/cli/install.sh | sh
```

### 2. Authenticate the CLI

Run `agy` once interactively. This opens a browser for Google sign-in and caches
credentials in the operating system keyring.

```bash
agy
```

This step must be completed manually before the server can function. The server
invokes `agy` in headless mode and does not perform authentication itself.

Confirm authentication succeeded:

```bash
agy models
```

A list of available models indicates the CLI is authenticated.

### 3. Install the server

```bash
git clone https://github.com/Thulnith-0/antigravity-delegate.git
```

```bash
cd antigravity-delegate && python -m venv .venv
```

Activate the virtual environment (`.venv\Scripts\activate` on Windows,
`source .venv/bin/activate` on macOS and Linux), then install dependencies:

```bash
pip install -r requirements.txt
```

### 4. Register the server with your MCP client

Create a `.mcp.json` file in the project directory where the server should be
available, or add the `mcpServers` entry to an existing configuration. Use
absolute paths to the virtual environment's interpreter so that the server does
not depend on an activated shell.

macOS and Linux:

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

Windows (note that backslashes must be escaped in JSON):

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

No environment block and no credentials are required for a default installation.

### 5. Restart the MCP client

MCP servers are loaded when a client session starts. An already-running session
will not detect the new configuration. Quit and reopen Claude Code.

## Verification

Ask Claude Code to invoke the tool:

> Use delegate_to_antigravity to list the contents of ./src and summarise it, with read_only enabled.

A returned summary indicates the server is operating correctly.

To verify the server independently of any MCP client:

Windows:

```powershell
.venv\Scripts\python.exe -c "import antigravity_delegate; print('ok')"
```

macOS and Linux:

```bash
.venv/bin/python -c "import antigravity_delegate; print('ok')"
```

The installation procedure above has been verified from a clean `git clone`:
clone, virtual environment creation, dependency installation, module import, and
the full test suite all complete successfully.

## Usage

Claude Code invokes the tool automatically when delegation is appropriate. It can
also be requested explicitly:

> Delegate this to Antigravity with allow_edits enabled: add type hints and docstrings to every function in src/utils.py

Tasks that modify files require `allow_edits`. Without it the sub-agent can read
and report, but every write is denied and the run is cancelled.

### Suitable tasks

Well-scoped, mechanical work with verifiable output:

- Adding type hints, docstrings, or tests across a module
- Implementing a UI component from a written specification
- Auditing a package for a particular pattern (use `read_only`)
- Repetitive refactoring across multiple files

### Unsuitable tasks

- Work requiring clarification, since the sub-agent cannot ask questions
- Cross-cutting architectural decisions
- Work that depends on context existing only in the Claude Code conversation

### Operational guidance

Each delegation begins a new conversation, so task descriptions must be
self-contained. Supply `conversation_id` from a previous result to continue an
earlier session.

Decompose large tasks by file. A complete multi-file feature requested in a
single call is significantly more likely to exceed its timeout than the same work
issued as one call per file. See
[Performance and cost](#performance-and-cost) for measurements.

Review returned output before accepting it. Treat a delegated result as a pull
request from an external contributor: generally sound, occasionally incorrect
with high confidence.

## Tool reference

### `delegate_to_antigravity`

| Parameter | Type | Default | Description |
| --- | --- | --- | --- |
| `task` | string | required | Complete, self-contained task description. |
| `directory` | string | required | Absolute path to an existing directory in which the sub-agent operates. |
| `allow_edits` | boolean | `false` | Permits file writes (`--mode accept-edits`). **Required for any task that modifies files.** The shell remains denied, but writes are not confined to `directory`. See [Security model](#security-model). |
| `allow_shell` | boolean | `false` | Permits shell command execution. Removes directory confinement. Implies `allow_edits`. See [Security model](#security-model). |
| `read_only` | boolean | `false` | Plan mode. The sub-agent investigates and produces an implementation plan without modifying files. Mutually exclusive with `allow_edits` and `allow_shell`. |
| `timeout_seconds` | integer | `900` | Abort the run after the specified duration. Maximum 3600. |
| `model` | string | account default | Model override. Run `agy models` for valid identifiers. |
| `conversation_id` | string | none | Resume the context of a previous run. |

The tool returns the sub-agent's final response, followed by elapsed time, token
usage, the `conversation_id` for subsequent chaining, and any diagnostic output.
If a run terminates in a non-success state, a warning is placed at the beginning
of the response so that an incomplete result is not mistaken for a finished one.

## Configuration

The following environment variables may be set in the server's `env` block.

| Variable | Default | Purpose |
| --- | --- | --- |
| `ANTIGRAVITY_ALLOW_EDITS` | `false` | Default value for `allow_edits`. Set this if most delegations are expected to modify files. Review the [Security model](#security-model) before enabling. |
| `ANTIGRAVITY_ALLOW_SHELL` | `false` | Default value for `allow_shell`. Review the [Security model](#security-model) before enabling. |
| `ANTIGRAVITY_TIMEOUT_SECONDS` | `900` | Default run timeout in seconds. |
| `ANTIGRAVITY_MODEL` | none | Default model for all runs. |
| `ANTIGRAVITY_LOG_LEVEL` | `INFO` | Server log verbosity. Output is written to stderr only. |
| `ANTIGRAVITY_CLI_PATH` | none | Explicit path to the `agy` executable if it is not resolvable on `PATH`. |
| `ANTIGRAVITY_CLI_TIMEOUT_BUFFER_SECONDS` | `30` | Grace period before a non-responsive `agy` process is terminated. |

## Security model

The behaviour described in this section was established by direct testing against
`agy` version 1.1.24, because the published headless-mode documentation did not
match observed behaviour.

### Summary of the three modes

| Capability | Default | `allow_edits` | `allow_shell` |
| --- | --- | --- | --- |
| Read files inside `directory` | Permitted | Permitted | Permitted |
| Write files inside `directory` | **Denied** | Permitted | Permitted |
| Write files outside `directory` | Denied | **Permitted** | Permitted |
| Execute shell commands | Denied | Denied | Permitted |

### Default mode

Headless mode cannot present an interactive permission prompt, so any request
requiring one is denied. That automatic denial is the mechanism that confines the
sub-agent to the target directory.

This includes file writes. In version 1.1.22 writes inside the workspace were
approved automatically; as of 1.1.24 they are not. A delegation that attempts to
modify a file without `allow_edits` is cancelled with a diagnostic naming the
`write_file` permission, having changed nothing. The default mode is therefore
useful for reading, research and reporting, but cannot complete editing work.

A task that requires a denied permission is cancelled in its entirety rather than
completing partially. No output is produced, and tokens consumed up to that point
are not recovered.

### Edit mode (`allow_edits` enabled)

This parameter passes `--mode accept-edits`, which approves file writes while
continuing to deny shell commands. It is the appropriate setting for the majority
of delegations, which need to modify files but not run commands.

**This grant is not confined to `directory`.** With `--add-dir` set to one
directory, a sub-agent instructed to write to an absolute path in an unrelated
directory did so successfully. The restriction that remains in this mode is the
absence of arbitrary command execution, which is a meaningful reduction in
exposure relative to `allow_shell`, but it is not a path sandbox.

### Shell mode (`allow_shell` enabled)

The Antigravity CLI provides no option to approve shell commands selectively.
Enabling this parameter passes `--dangerously-skip-permissions`, which
automatically approves every tool call the sub-agent makes.

**This removes directory confinement entirely.** With `--add-dir` set to one
directory, the sub-agent was instructed to overwrite a file in an unrelated
directory and did so successfully. In this mode the sub-agent can read and write
any location accessible to the current user account.

**This also removes isolation between delegations.** The Antigravity CLI stores
every conversation in plain text under `~/.gemini/antigravity-cli/`, with full
transcripts in `brain/<conversation-id>/`. In testing, a new delegation was asked
for information it had no legitimate access to. It performed 58 steps searching
the filesystem, located the transcript of an unrelated earlier delegation, and
answered from its contents. The same query in default mode correctly returned
"UNKNOWN".

Consequently, information supplied to one delegation may surface in a later,
unrelated delegation, including across different projects.

### Recommendations

1. Use the least permission the task requires: `read_only` for research,
   auditing and code review; `allow_edits` for work that modifies files;
   `allow_shell` only when commands must actually run.
2. Prefer `allow_edits` over `allow_shell` for editing work. Most file-editing
   tasks complete successfully without shell access, and the difference between
   the two is arbitrary command execution.
3. Enable `allow_shell` only for directories where granting full user-account
   access is acceptable. Do not enable it for home directories or repositories
   containing credentials.
4. Treat neither `allow_edits` nor `allow_shell` as a path restriction. If a
   delegation must not touch a particular tree, the enforcement has to come from
   outside this server.
5. Clear `~/.gemini/antigravity-cli/brain/` between sensitive delegations.

### Choosing a permission level

Any task that writes a file requires at least `allow_edits`. Beyond that, the
determining factor for `allow_shell` is task size rather than task type, because
larger tasks tend to invoke commands to verify their own work.

| Task | Minimum required |
| --- | --- |
| Read-only research query | Default |
| Auditing or planning without changes | `read_only` |
| Create a single file | `allow_edits` |
| Add type hints and docstrings across two functions | `allow_edits` |
| Implement a complete single-file UI from a specification | `allow_edits` |
| Fix three defects across a module | `allow_shell` |
| Any task that must install packages, run tests, or use git | `allow_shell` |

Decomposing a large task into per-file calls frequently keeps the work within
`allow_edits`, in addition to reducing latency.

The measurements in the preceding rows were taken under version 1.1.24. Earlier
releases approved in-workspace writes without `allow_edits`; if a delegation that
previously succeeded now returns `CANCELED`, this change is the likely cause.

### Changing the default

If a workflow consistently requires shell access, set the default once in the
client configuration rather than passing the parameter on every call:

```json
{
  "mcpServers": {
    "antigravity-delegate": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/antigravity_delegate.py"],
      "env": { "ANTIGRAVITY_ALLOW_SHELL": "true" }
    }
  }
}
```

The server emits a warning at startup when this is enabled. Individual calls
still take precedence: passing `allow_shell` as `false` disables it for that
call, and `read_only` continues to function normally rather than conflicting with
the configured default.

The distributed default remains `false` by design. This parameter grants
filesystem-wide access, which should not be acquired by installing the server
without reading this section.

## Performance and cost

### Context reduction

The following measurements are from implementing a responsive pricing section
(three tier cards, CSS Grid layout, dark mode, and an accessible monthly/yearly
toggle with live price updates) into an empty static site. The result was 18.9 KB
of HTML, CSS, and JavaScript across three files, verified functional in a browser.

| Approach | Claude Code tokens | Antigravity tokens |
| --- | --- | --- |
| Delegated (4 calls) | approximately 2,800 | 412,842 |
| Implemented directly by Claude (estimated) | 15,000 to 25,000 | 0 |
| Net effect | 5x to 9x context reduction | billed to subscription |

The Claude Code figure is exact: 5,979 characters of task descriptions issued and
5,275 characters of results returned. The direct-implementation figure is an
estimate comprising 4,729 output tokens for the generated code, the tokens
required to read the existing files, and the reasoning and iteration typical of
UI work.

### Delegation efficiency

Of the 412,842 Antigravity tokens consumed above, approximately 54 percent
produced usable output.

| Run | Tokens | Outcome |
| --- | --- | --- |
| Default mode, complete task | 39,887 | Cancelled. Required shell access. |
| Shell mode, complete task | 97,837 | Timed out at 500s. Only `index.html` was written. |
| Resumed via `conversation_id`, `styles.css` only | 148,443 | Succeeded in 54s. |
| New conversation, `app.js` only | 126,675 | Succeeded in 72s. |

Two conclusions follow:

1. **Decompose UI tasks by file.** The complete feature requested in one call
   exceeded its timeout. Individual files completed in approximately one minute
   each.
2. **Resuming a conversation inherits its accumulated cost.** The third run
   resumed the timed-out conversation and carried its history, consuming 148,443
   tokens for a single stylesheet. The fourth run performed comparable work in a
   new conversation for less.

### Conversation resumption

The `conversation_id` parameter provides continuity, not cost reduction. Its
effect on token consumption depends on the task:

| Follow-up task | Resumed | New conversation | Difference |
| --- | --- | --- | --- |
| Question about a previously read file | 36,800 | 30,200 | 22 percent higher |
| Recall of a fact not derivable from disk | 20,800 | 167,600 | 88 percent lower |

Use resumption when restating context would be lengthy or error-prone. Omit it
for independent tasks, where a new conversation is generally less expensive.

### Reference measurements

| Task | Approximate tokens |
| --- | --- |
| Write a single-line file | 25,000 |
| Add type hints and docstrings to two functions | 49,000 |
| Fix three defects in a 20-line module | 208,000 |
| One file of a UI feature, appropriately scoped | 127,000 |
| A run that timed out without completing | 220,000 |

Set `timeout_seconds` conservatively so that failures terminate quickly.

## Troubleshooting

**The tool does not appear in the MCP client.**
Restart the client, as servers are loaded at session start. Confirm that
`.mcp.json` contains valid JSON with correctly escaped backslashes, and that both
absolute paths exist.

**`Could not find the Antigravity CLI ('agy')`.**
The executable is not resolvable on `PATH`. Set `ANTIGRAVITY_CLI_PATH` to its
full path. On Windows this is typically
`C:\Users\<username>\AppData\Local\agy\bin\agy.exe`.

**Authentication errors or empty responses.**
The CLI session has expired. Run `agy` interactively to sign in again, then
confirm with `agy models`.

**A run returns `CANCELED` with a permission diagnostic.**
The task required a permission that headless mode cannot prompt for. Read the
diagnostic to determine which one. A `write_file` denial means the task modifies
files and requires `allow_edits`. A `command` denial means it requires a shell,
and therefore `allow_shell`. Enable the narrower of the two after reviewing the
[Security model](#security-model), or restructure the task to avoid the
permission entirely.

**A delegation reports success but no files changed.**
The run had no edit permission and reported only what it intended to do. Pass
`allow_edits` as `true`.

**Runs exceed their timeout.**
Increase `timeout_seconds` up to the maximum of 3600, or decompose the task.
Files already written to disk are retained.

## Development

Install development dependencies:

```bash
pip install -r requirements-dev.txt
```

Run the test suite:

```bash
pytest
```

The suite contains 54 tests covering input validation, argument construction,
subprocess handling, result formatting, and the MCP tool boundary. The `agy`
binary is replaced by a stand-in implementation (`tests/fakes/fake_agy.py`), so
no test performs a live API call. The suite requires neither credentials nor an
Antigravity installation. Continuous integration runs it on Python 3.10, 3.11,
and 3.12.

### Project layout

```
antigravity_delegate.py     Server implementation (single module)
tests/
  test_server.py            Test suite
  fakes/fake_agy.py         Stand-in for the agy binary
```

## Compatibility

Behaviour documented here was verified on Windows against `agy` version 1.1.24.
Continuous integration confirms the test suite passes on Linux, but live
delegations have not been exercised on macOS or Linux.

The Antigravity CLI updates itself automatically, so its behaviour may diverge
from what is documented here. If observed behaviour contradicts this document,
please open an issue reporting the `agy` version in use.

## License

Released under the [MIT License](LICENSE).
