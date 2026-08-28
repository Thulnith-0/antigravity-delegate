"""MCP server that delegates coding and research tasks to Google Antigravity sub-agents.

Exposes a single tool, ``delegate_to_antigravity``, which spins up an Antigravity
``Agent`` scoped to a caller-supplied directory, runs a task to completion, and
returns the agent's final answer plus a short trace of what it actually did.

Transport is stdio, so nothing may ever be written to stdout except MCP frames --
all diagnostics go to stderr.
"""

from __future__ import annotations

import asyncio
import logging
import os
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


if __name__ == "__main__":
    # stdio transport: the banner would only clutter the host's stderr log.
    mcp.run(show_banner=False)
