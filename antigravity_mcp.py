"""MCP server that delegates coding and research tasks to Google Antigravity sub-agents.

Exposes two tools that both run a task to completion in a caller-supplied
directory and return the sub-agent's final answer:

* ``delegate_to_antigravity`` -- the google-antigravity Python SDK, billed as a
  standalone Gemini API key / GCP project.
* ``delegate_to_antigravity_cli`` -- the ``agy`` CLI as a subprocess, using
  whatever Google account it is already logged into (so a subscription such as
  AI Pro/Ultra applies, unlike the SDK path). Requires ``agy`` to already be
  installed and logged in; this server never handles that login itself.

Transport is stdio, so nothing may ever be written to stdout except MCP frames --
all diagnostics go to stderr.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

try:
    from google.antigravity import Agent, CapabilitiesConfig, LocalAgentConfig
    from google.antigravity.hooks import policy
    from google.antigravity.types import BuiltinTools, StopReason
except ImportError as exc:  # pragma: no cover - environment problem, not logic
    raise SystemExit(
        "The google-antigravity SDK is not importable from this interpreter.\n"
        f"Interpreter: {sys.executable}\n"
        "Install it with: python -m pip install google-antigravity\n"
        "(Install from PyPI, not from a git clone -- the SDK ships a compiled "
        "runtime binary that only the PyPI wheels contain.)"
    ) from exc

logging.basicConfig(
    level=os.environ.get("ANTIGRAVITY_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [antigravity-mcp] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("antigravity_mcp")

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

def _env_int(name: str, default: int) -> int:
    """Reads an int from the environment without letting a typo kill startup."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d.", name, raw, default)
        return default


DEFAULT_TIMEOUT_SECONDS = _env_int("ANTIGRAVITY_TIMEOUT_SECONDS", 900)
MAX_TIMEOUT_SECONDS = 3600
DEFAULT_MODEL = os.environ.get("ANTIGRAVITY_MODEL") or None

# Tools the sub-agent may use in read-only mode. The SDK's own read-only set
# plus search_web, which reaches the network but mutates nothing locally.
READ_ONLY_TOOL_NAMES = tuple(t.value for t in BuiltinTools.read_only()) + (
    BuiltinTools.SEARCH_WEB.value,
)
# Denied outright in read-only mode, as a second layer behind the allowlist.
MUTATING_TOOL_NAMES = (
    BuiltinTools.CREATE_FILE.value,
    BuiltinTools.EDIT_FILE.value,
    BuiltinTools.RUN_COMMAND.value,
)

# Direct Gemini API keys. If Vertex / Gemini Enterprise is enabled instead, the
# SDK authenticates through Application Default Credentials and needs no key.
API_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
VERTEX_ENV_VARS = ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_ENTERPRISE")

# CLI backend (agy). Extra seconds of grace beyond the CLI's own --print-timeout
# before this server force-kills a hung subprocess itself.
CLI_SUBPROCESS_TIMEOUT_BUFFER = _env_int("ANTIGRAVITY_CLI_TIMEOUT_BUFFER_SECONDS", 30)
# Default install location of the Windows installer (antigravity.google/cli/install.ps1).
# Overridable, and not the only lookup path -- see _find_cli_binary.
WINDOWS_DEFAULT_CLI_PATH = Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"

DELEGATE_SYSTEM_INSTRUCTIONS = """\
You are a delegated engineering sub-agent. Another AI agent has handed you a \
self-contained task and is waiting on your result; there is no human available \
to answer follow-up questions, so never ask for clarification -- make a \
reasonable assumption, act on it, and state the assumption in your final answer.

You are scoped to a single workspace directory. Investigate it before changing \
anything, and keep your edits confined to the task you were given.

Your final message is the entire deliverable. Make it self-contained:
  * Lead with the outcome in one or two sentences.
  * Reference concrete files as relative paths, with line numbers where useful.
  * List every file you created or modified.
  * State plainly what you could not do, verify, or complete, and why.
Do not pad the answer with restatements of the task or filler pleasantries.
"""


@dataclass
class DelegationResult:
    """Everything worth reporting back about one delegated run."""

    text: str
    elapsed_seconds: float
    tool_calls: Counter = field(default_factory=Counter)
    files_touched: list[str] = field(default_factory=list)
    stop_reason: object | None = None
    total_tokens: int | None = None
    trace_error: str | None = None


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #


def _resolve_workspace(directory: str) -> Path:
    """Validates the target directory and returns it as an absolute path."""
    if not directory or not directory.strip():
        raise ToolError(
            "`directory` is required: pass the absolute path of the directory "
            "the sub-agent should work in."
        )

    path = Path(directory.strip()).expanduser()
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ToolError(
            f"Could not resolve `directory` {directory!r}: {exc}. "
            "Pass an absolute path to a directory that already exists."
        ) from exc

    if not resolved.is_dir():
        raise ToolError(
            f"`directory` must be a directory, but {str(resolved)!r} is a file. "
            "Pass the folder the sub-agent should work in."
        )
    return resolved


def _check_credentials() -> None:
    """Fails fast with an actionable message when no usable credential is set."""
    if any(os.environ.get(var) for var in API_KEY_ENV_VARS):
        return
    if any(os.environ.get(var) for var in VERTEX_ENV_VARS):
        # Vertex / Gemini Enterprise path: the SDK uses Application Default
        # Credentials, which cannot be meaningfully verified without a live call.
        return
    raise ToolError(
        "No Gemini credentials are visible to the MCP server process. Set "
        "GEMINI_API_KEY in the server's `env` block in your MCP config (get a "
        "key at https://aistudio.google.com/apikey), or configure Vertex by "
        "setting GOOGLE_GENAI_USE_VERTEXAI=true with GOOGLE_CLOUD_PROJECT and "
        "GOOGLE_CLOUD_LOCATION, plus `gcloud auth application-default login`."
    )


def _validate_timeout(timeout_seconds: int) -> int:
    if timeout_seconds <= 0:
        raise ToolError("`timeout_seconds` must be a positive number of seconds.")
    if timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise ToolError(
            f"`timeout_seconds` may not exceed {MAX_TIMEOUT_SECONDS} "
            f"({MAX_TIMEOUT_SECONDS // 60} minutes)."
        )
    return timeout_seconds


# --------------------------------------------------------------------------- #
# Agent construction and execution
# --------------------------------------------------------------------------- #


def _build_config(
    workspace: Path,
    *,
    allow_shell: bool,
    read_only: bool,
    model: str | None,
) -> LocalAgentConfig:
    """Builds the agent config for one delegated run.

    ``workspaces`` is the security boundary: the SDK's policy evaluator confines
    the file tools to those directories. Shell access is layered on top of that
    and stays off unless explicitly requested.
    """
    kwargs: dict[str, object] = {
        "system_instructions": DELEGATE_SYSTEM_INSTRUCTIONS,
        "workspaces": [str(workspace)],
    }

    if read_only:
        # Restricting the agent takes an explicit allowlist. Omitting
        # `capabilities` does NOT yield a read-only agent: LocalAgentConfig
        # fills in a default CapabilitiesConfig() that is byte-identical to the
        # explicit one, so a bare omission would leave writes fully enabled.
        kwargs["capabilities"] = CapabilitiesConfig(
            enabled_tools=list(READ_ONLY_TOOL_NAMES),
            # A sub-agent could otherwise be spawned without these limits.
            enable_subagents=False,
        )
        # Second layer: even if the allowlist were bypassed, the policy
        # evaluator refuses the mutating tools outright.
        kwargs["policies"] = [policy.deny(name) for name in MUTATING_TOOL_NAMES]
    else:
        kwargs["capabilities"] = CapabilitiesConfig()

    if allow_shell:
        # Replaces the default confirm_run_command() policy, which denies
        # run_command outright because no human is available to confirm it.
        # Workspace file scoping is enforced by more specific policies and
        # survives this wildcard allow.
        kwargs["policies"] = [policy.allow_all()]

    if model:
        kwargs["model"] = model

    return LocalAgentConfig(**kwargs)


def _build_prompt(task: str, workspace: Path, *, allow_shell: bool) -> str:
    shell_note = (
        "You may run shell commands in this directory."
        if allow_shell
        else "Shell execution is disabled for this run; work through the file "
        "tools only. If the task genuinely cannot be completed without running "
        "a command, say so in your final answer rather than working around it."
    )
    return (
        f"Workspace directory: {workspace}\n"
        f"{shell_note}\n\n"
        f"Task:\n{task.strip()}\n"
    )


async def _collect_trace(response, result: DelegationResult) -> None:
    """Best-effort summary of the sub-agent's tool use.

    Never allowed to fail the delegation: the agent's answer is the deliverable,
    and a broken trace is not worth discarding it for.
    """
    try:
        seen_paths: list[str] = []
        async for call in response.tool_calls:
            result.tool_calls[call.name] += 1
            path = getattr(call, "canonical_path", None)
            if path and path not in seen_paths:
                seen_paths.append(path)
        result.files_touched = seen_paths

        result.stop_reason = response.stop_reason
        usage = response.usage_metadata
        if usage is not None:
            result.total_tokens = getattr(usage, "total_token_count", None)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not mask the result
        logger.warning("Could not collect run trace: %s", exc)
        result.trace_error = str(exc)


async def _run_delegation(
    task: str,
    workspace: Path,
    *,
    allow_shell: bool,
    read_only: bool,
    model: str | None,
) -> DelegationResult:
    config = _build_config(
        workspace, allow_shell=allow_shell, read_only=read_only, model=model
    )
    prompt = _build_prompt(task, workspace, allow_shell=allow_shell)

    started = time.monotonic()
    async with Agent(config) as agent:
        response = await agent.chat(prompt)
        text = await response.text()
        result = DelegationResult(
            text=(text or "").strip(),
            elapsed_seconds=time.monotonic() - started,
        )
        await _collect_trace(response, result)

    if not result.text:
        result.text = (
            "(The sub-agent finished without producing any final text. The trace "
            "below shows what it did.)"
        )
    return result


def _format_result(result: DelegationResult, workspace: Path) -> str:
    """Renders the result for the calling agent: answer first, evidence after."""
    lines = [
        result.text,
        "",
        "---",
        f"_Antigravity sub-agent - `{workspace}` - {result.elapsed_seconds:.1f}s_",
    ]

    if result.tool_calls:
        summary = ", ".join(
            f"{name} x{count}" for name, count in result.tool_calls.most_common()
        )
        lines.append(f"_Tool calls: {summary}_")

    if result.files_touched:
        shown = result.files_touched[:15]
        suffix = (
            f" (+{len(result.files_touched) - len(shown)} more)"
            if len(result.files_touched) > len(shown)
            else ""
        )
        lines.append(f"_Paths touched: {', '.join(shown)}{suffix}_")

    if result.total_tokens:
        lines.append(f"_Tokens: {result.total_tokens:,}_")

    if result.trace_error:
        lines.append(f"_Trace unavailable: {result.trace_error}_")

    # A non-standard stop reason means the answer above is probably truncated,
    # so surface it at the top rather than burying it in the footer.
    stop = result.stop_reason
    if stop is not None and stop != StopReason.UNSPECIFIED:
        label = getattr(stop, "value", stop)
        lines.insert(
            0,
            f"> **Warning:** the sub-agent stopped early (`{label}`). The result "
            "below may be incomplete.\n",
        )

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI backend (agy)
#
# Verified empirically against agy 1.1.22 (docs for headless flags were
# inconsistent, so behavior below was confirmed by actually running the CLI):
#   * Default headless mode DOES auto-approve file writes inside the workspace
#     -- more permissive than the SDK path, where writes need an explicit
#     CapabilitiesConfig.
#   * Default headless mode auto-DENIES the "command" (shell) permission with
#     a clear stderr message, because headless mode cannot prompt for it.
#     Fails closed, does not hang.
#   * --mode plan produces a plan without touching files in the workspace --
#     used here for read_only.
#   * --add-dir is NOT a hard path sandbox. Out-of-workspace access raises a
#     permission request that headless mode auto-denies, which is what confines
#     the agent in default mode. Under --dangerously-skip-permissions that
#     auto-denial becomes auto-approval: verified that agy then writes to paths
#     entirely outside --add-dir. So allow_shell=True means no file confinement.
# --------------------------------------------------------------------------- #


def _find_cli_binary() -> Path:
    """Locates the agy executable.

    Checked in order: an explicit override (also how tests point this at a
    stand-in), PATH, then the default Windows install location -- agy is not
    reliably on PATH right after the installer runs in the *current* shell.
    """
    override = os.environ.get("ANTIGRAVITY_CLI_PATH")
    if override:
        path = Path(override)
        if not path.exists():
            raise ToolError(f"ANTIGRAVITY_CLI_PATH={override!r} does not exist.")
        return path

    found = shutil.which("agy")
    if found:
        return Path(found)

    if WINDOWS_DEFAULT_CLI_PATH.exists():
        return WINDOWS_DEFAULT_CLI_PATH

    raise ToolError(
        "Could not find the Antigravity CLI ('agy'). Install it "
        "(https://antigravity.google/docs/cli/install/) and log in once "
        "interactively, or set ANTIGRAVITY_CLI_PATH to its executable path."
    )


def _cli_command_prefix(binary: Path) -> list[str]:
    """Builds the subprocess argv prefix for `binary`.

    A `.py` path means a test stand-in (see tests/fakes/fake_agy.py); it has to
    run through the current interpreter rather than being executed directly.
    """
    if binary.suffix == ".py":
        return [sys.executable, str(binary)]
    return [str(binary)]


def _build_cli_args(
    task: str,
    workspace: Path,
    *,
    allow_shell: bool,
    read_only: bool,
    timeout_seconds: int,
    model: str | None,
    conversation_id: str | None = None,
) -> list[str]:
    args = [
        "-p",
        task,
        "--add-dir",
        str(workspace),
        "--output-format",
        "json",
        "--print-timeout",
        f"{timeout_seconds}s",
        # The task text originates from another AI agent, not a human at a
        # keyboard; it should never be reinterpreted as a slash command.
        "--disable-slash-commands",
    ]
    if conversation_id:
        # Explicit id rather than agy's --continue ("most recent"), which would
        # be racy when delegations run concurrently.
        args += ["--conversation", conversation_id]
    if read_only:
        args += ["--mode", "plan"]
    if allow_shell:
        # Broader than the SDK tool's allow_shell: this auto-approves every
        # tool call agy makes, not just run_command -- agy has no CLI flag for
        # shell-only approval (that requires a settings.json allow-rule).
        args += ["--dangerously-skip-permissions"]
    if model:
        args += ["--model", model]
    return args


@dataclass
class CliDelegationResult:
    """Everything worth reporting back about one agy CLI run."""

    text: str
    elapsed_seconds: float
    status: str | None = None
    usage: dict | None = None
    raw_stderr: str = ""
    parse_error: str | None = None
    conversation_id: str | None = None


async def _run_cli_delegation(
    task: str,
    workspace: Path,
    *,
    allow_shell: bool,
    read_only: bool,
    timeout_seconds: int,
    model: str | None,
    conversation_id: str | None = None,
) -> CliDelegationResult:
    binary = _find_cli_binary()
    command = _cli_command_prefix(binary) + _build_cli_args(
        task,
        workspace,
        allow_shell=allow_shell,
        read_only=read_only,
        timeout_seconds=timeout_seconds,
        model=model,
        conversation_id=conversation_id,
    )

    started = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(workspace),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_seconds + CLI_SUBPROCESS_TIMEOUT_BUFFER,
        )
    except asyncio.TimeoutError:
        # --print-timeout should make agy exit on its own; this is the backstop
        # if it doesn't, so the subprocess is never left running unattended.
        proc.kill()
        await proc.wait()
        raise ToolError(
            f"agy did not finish within {timeout_seconds}s and was killed. Any "
            "files it already wrote are still on disk. Re-run with a larger "
            "`timeout_seconds`, or split the task into smaller pieces."
        ) from None

    elapsed = time.monotonic() - started
    stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
    stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0 and not stdout:
        raise ToolError(
            f"agy exited with code {proc.returncode} and produced no output. "
            f"stderr: {stderr or '(empty)'}"
        )

    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        return CliDelegationResult(
            text=stdout or "(agy produced no parseable output)",
            elapsed_seconds=elapsed,
            raw_stderr=stderr,
            parse_error=str(exc),
        )

    text = (payload.get("response") or "").strip()
    if not text:
        text = "(agy finished without a response; see status/diagnostics below)"

    return CliDelegationResult(
        text=text,
        elapsed_seconds=elapsed,
        status=payload.get("status"),
        usage=payload.get("usage"),
        raw_stderr=stderr,
        conversation_id=payload.get("conversation_id"),
    )


def _format_cli_result(result: CliDelegationResult, workspace: Path) -> str:
    """Renders the result for the calling agent: answer first, evidence after."""
    lines = [
        result.text,
        "",
        "---",
        f"_Antigravity CLI sub-agent - `{workspace}` - {result.elapsed_seconds:.1f}s_",
    ]

    if result.usage:
        total = result.usage.get("total_tokens")
        if total:
            lines.append(f"_Tokens: {total:,}_")

    if result.conversation_id:
        # Surfaced so a follow-up call can pass conversation_id and keep this
        # session's context. Note resuming costs MORE tokens, not fewer.
        lines.append(f"_conversation_id: `{result.conversation_id}`_")

    if result.parse_error:
        lines.append(
            f"_agy's output was not valid JSON ({result.parse_error}); showing "
            "raw output above instead of a parsed response._"
        )

    if result.raw_stderr:
        # agy's stderr carries diagnostics (denied permissions, progress) that
        # matter for a caller trying to understand a short/empty response --
        # capped so a noisy run doesn't dominate the reply.
        snippet = result.raw_stderr[:400]
        suffix = "..." if len(result.raw_stderr) > 400 else ""
        lines.append(f"_agy diagnostics: {snippet}{suffix}_")

    if result.status and result.status != "SUCCESS":
        lines.insert(
            0,
            f"> **Warning:** agy reported status `{result.status}`. The result "
            "below may be incomplete.\n",
        )

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #

mcp = FastMCP(
    name="antigravity-delegate",
    instructions=(
        "Delegates self-contained coding and research tasks to Google "
        "Antigravity sub-agents that run locally against a specific directory."
    ),
)


@mcp.tool
async def delegate_to_antigravity(
    task: str,
    directory: str,
    allow_shell: bool = False,
    read_only: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    model: str | None = DEFAULT_MODEL,
) -> str:
    """Delegate a self-contained coding or research task to a Google Antigravity sub-agent.

    The sub-agent runs locally, sandboxed to `directory`, and works autonomously
    until it produces a final answer. It cannot ask follow-up questions, so state
    the task completely: what to do, what "done" looks like, and any constraints.
    Prefer this for well-scoped work you would otherwise do file-by-file yourself
    (implement a feature in a module, audit a package for a pattern, write tests
    for a file). Returns the sub-agent's final report and a trace of its tool use.

    Args:
        task: The complete task description. Self-contained and specific.
        directory: Absolute path to the existing directory the sub-agent works
            in. Its file tools are confined to this directory.
        allow_shell: Allow the sub-agent to run shell commands in `directory`
            without confirmation (needed to run builds, tests, or linters). Off
            by default; when off, shell calls are refused and file tools still
            work.
        read_only: Restrict the sub-agent to read-only tools so it cannot modify
            anything. Use for research and code review. Cannot be combined with
            allow_shell.
        timeout_seconds: Abort the run after this many seconds (max 3600).
        model: Optional Gemini model override; defaults to the SDK's choice.

    Returns:
        The sub-agent's final answer, followed by a short execution trace.
    """
    if not task or not task.strip():
        raise ToolError(
            "`task` is required: describe the work the sub-agent should carry out."
        )
    if read_only and allow_shell:
        raise ToolError(
            "`read_only` and `allow_shell` are contradictory: shell access would "
            "let the sub-agent modify the workspace. Enable at most one."
        )

    workspace = _resolve_workspace(directory)
    timeout = _validate_timeout(timeout_seconds)
    _check_credentials()

    logger.info(
        "Delegating to Antigravity in %s (shell=%s, read_only=%s, timeout=%ss)",
        workspace,
        allow_shell,
        read_only,
        timeout,
    )

    try:
        result = await asyncio.wait_for(
            _run_delegation(
                task,
                workspace,
                allow_shell=allow_shell,
                read_only=read_only,
                model=model,
            ),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("Delegation timed out after %ss in %s", timeout, workspace)
        raise ToolError(
            f"The sub-agent did not finish within {timeout}s and was cancelled. "
            "Any files it already wrote are still on disk. Re-run with a larger "
            "`timeout_seconds`, or split the task into smaller pieces."
        ) from None
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a usable message to caller
        logger.exception("Delegation failed in %s", workspace)
        raise ToolError(
            f"The Antigravity sub-agent failed: {type(exc).__name__}: {exc}"
        ) from exc

    logger.info(
        "Delegation finished in %.1fs (%d tool calls)",
        result.elapsed_seconds,
        sum(result.tool_calls.values()),
    )
    return _format_result(result, workspace)


@mcp.tool
async def delegate_to_antigravity_cli(
    task: str,
    directory: str,
    allow_shell: bool = False,
    read_only: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    model: str | None = DEFAULT_MODEL,
    conversation_id: str | None = None,
) -> str:
    """Delegate a task to Antigravity via the `agy` CLI, using your logged-in account instead of a separate API key.

    Same contract as delegate_to_antigravity (self-contained task, sandboxed
    directory, no follow-up questions), but runs the Antigravity CLI (agy) as a
    subprocess instead of the Python SDK. Billing and rate limits come from
    whatever plan the Google account `agy` is logged into has (e.g. an AI
    Pro/Ultra subscription) -- not a separate metered Gemini API key.

    Requires `agy` to already be installed and logged in: run it once
    interactively yourself first so it can complete the browser login. This
    tool never installs `agy` or handles that login; it only runs it headlessly
    once credentials are already cached.

    Args:
        task: The complete task description. Self-contained and specific.
        directory: Absolute path to the existing directory the sub-agent works
            in, passed to agy as `--add-dir`. With allow_shell=False this
            effectively confines the agent, because out-of-workspace access
            needs a permission that headless mode auto-denies. It is NOT a
            hard path sandbox -- see allow_shell.
        allow_shell: DANGEROUS. Passes `--dangerously-skip-permissions`, which
            auto-approves every tool call agy makes -- agy has no shell-only
            approval flag. This also removes the directory confinement:
            verified that with this flag agy will read and write files
            anywhere on the filesystem, not just `directory`. Treat it as
            granting full user-account access, not as scoped shell access.
            Prefer delegate_to_antigravity (the SDK tool), which keeps its
            file-tool sandbox even with shell enabled. Off by default; even
            off, agy still edits files inside the workspace, but shell
            commands are auto-denied with a clear message rather than hanging.
        read_only: Runs agy in plan mode (`--mode plan`): it investigates and
            writes an implementation plan, but does not modify files in
            `directory`. Cannot be combined with allow_shell.
        timeout_seconds: Abort the run after this many seconds (max 3600).
            Passed through to agy's own `--print-timeout`.
        model: Optional model override (see `agy models` for valid IDs;
            includes non-Gemini models depending on your account).
        conversation_id: Resume a previous run's conversation, keeping its
            context (prior instructions, conventions, decisions) so you need
            not restate them. Pass the `conversation_id` reported in an earlier
            result. Use this for CONTINUITY, not to save tokens -- measured, a
            resumed follow-up cost ~22% MORE than the same task in a fresh
            conversation, because the carried history outweighs what re-reading
            the files would have cost. Omit it for independent tasks.

    Returns:
        agy's final answer, then token usage, the conversation_id for chaining,
        and any diagnostics.
    """
    if not task or not task.strip():
        raise ToolError(
            "`task` is required: describe the work the sub-agent should carry out."
        )
    if read_only and allow_shell:
        raise ToolError(
            "`read_only` and `allow_shell` are contradictory: shell access would "
            "let the sub-agent modify the workspace. Enable at most one."
        )

    workspace = _resolve_workspace(directory)
    timeout = _validate_timeout(timeout_seconds)

    logger.info(
        "Delegating to agy CLI in %s (shell=%s, read_only=%s, timeout=%ss)",
        workspace,
        allow_shell,
        read_only,
        timeout,
    )

    try:
        result = await _run_cli_delegation(
            task,
            workspace,
            allow_shell=allow_shell,
            read_only=read_only,
            timeout_seconds=timeout,
            model=model,
            conversation_id=conversation_id,
        )
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a usable message to caller
        logger.exception("CLI delegation failed in %s", workspace)
        raise ToolError(
            f"The Antigravity CLI sub-agent failed: {type(exc).__name__}: {exc}"
        ) from exc

    logger.info(
        "CLI delegation finished in %.1fs (status=%s)",
        result.elapsed_seconds,
        result.status,
    )
    return _format_cli_result(result, workspace)


if __name__ == "__main__":
    # stdio transport: the banner would only clutter the host's stderr log.
    mcp.run(show_banner=False)
