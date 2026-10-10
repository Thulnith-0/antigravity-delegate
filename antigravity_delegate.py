"""MCP server that delegates coding and research tasks to Google Antigravity.

Exposes a single tool, ``delegate_to_antigravity``, which runs the Antigravity
CLI (``agy``) as a subprocess against a caller-supplied directory and returns
the sub-agent's final report.

Runs bill against whatever Google account ``agy`` is logged into, so an AI
Pro/Ultra subscription applies. The CLI must already be installed and logged in;
this server never performs that login.

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
from dataclasses import dataclass
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

logging.basicConfig(
    level=os.environ.get("ANTIGRAVITY_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [antigravity-delegate] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("antigravity_delegate")

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


def _env_bool(name: str, default: bool) -> bool:
    """Reads a boolean from the environment; anything unrecognised is a no."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_TIMEOUT_SECONDS = _env_int("ANTIGRAVITY_TIMEOUT_SECONDS", 900)
MAX_TIMEOUT_SECONDS = 3600
DEFAULT_MODEL = os.environ.get("ANTIGRAVITY_MODEL") or None

# Opt-in default for allow_shell. Ships false: allow_shell removes directory
# confinement entirely, so it must never become the default by accident for
# someone who installed this without reading the README. Operators who
# knowingly accept that (e.g. delegating whole features that reliably need a
# shell) can flip it once for their own machine.
DEFAULT_ALLOW_SHELL = _env_bool("ANTIGRAVITY_ALLOW_SHELL", False)

# Opt-in default for allow_edits. Also ships false: --mode accept-edits blocks
# the shell but does NOT confine writes to --add-dir (verified, see below), so
# it is a real grant of write access to the whole filesystem and should be a
# deliberate per-call choice rather than a silent default.
DEFAULT_ALLOW_EDITS = _env_bool("ANTIGRAVITY_ALLOW_EDITS", False)

# Extra seconds of grace beyond agy's own --print-timeout before this server
# force-kills a subprocess that ignored it.
CLI_SUBPROCESS_TIMEOUT_BUFFER = _env_int("ANTIGRAVITY_CLI_TIMEOUT_BUFFER_SECONDS", 30)

# Default install location of the Windows installer (antigravity.google/cli).
# Overridable, and not the only lookup path -- see _find_cli_binary.
WINDOWS_DEFAULT_CLI_PATH = Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"

# Accepted --effort values. Taken from the binary's own rejection message rather
# than its --help text: agy 1.1.24 documented only low|medium|high, but 1.3.3
# accepts five. Validating against the shorter list would reject values the CLI
# supports, so this is read from what the CLI actually enforces.
VALID_EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")


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


def _validate_timeout(timeout_seconds: int) -> int:
    if timeout_seconds <= 0:
        raise ToolError("`timeout_seconds` must be a positive number of seconds.")
    if timeout_seconds > MAX_TIMEOUT_SECONDS:
        raise ToolError(
            f"`timeout_seconds` may not exceed {MAX_TIMEOUT_SECONDS} "
            f"({MAX_TIMEOUT_SECONDS // 60} minutes)."
        )
    return timeout_seconds


def _validate_effort(effort: str | None) -> str | None:
    """Checks `effort` against the levels the CLI accepts."""
    if effort is None:
        return None
    normalised = effort.strip().lower()
    if normalised not in VALID_EFFORT_LEVELS:
        raise ToolError(
            f"`effort` must be one of {', '.join(VALID_EFFORT_LEVELS)}; "
            f"got {effort!r}."
        )
    return normalised


def _resolve_extra_dirs(extra_dirs: list[str] | None) -> list[Path]:
    """Validates additional workspace directories and returns absolute paths."""
    if not extra_dirs:
        return []

    resolved: list[Path] = []
    for entry in extra_dirs:
        if not entry or not entry.strip():
            raise ToolError(
                "`extra_dirs` contains an empty path. Remove it, or pass the "
                "absolute path of a directory that exists."
            )
        path = Path(entry.strip()).expanduser()
        try:
            candidate = path.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ToolError(
                f"`extra_dirs` entry {entry!r} could not be resolved: {exc}. "
                "Each entry must be an absolute path to a directory that "
                "already exists."
            ) from exc
        if not candidate.is_dir():
            raise ToolError(
                f"`extra_dirs` entry {str(candidate)!r} is a file, not a "
                "directory. Pass the containing folder instead."
            )
        if candidate not in resolved:
            resolved.append(candidate)
    return resolved


def _resolve_json_schema(json_schema: str | None) -> str | None:
    """Accepts either an inline JSON schema or a path to a schema file.

    The CLI takes both forms in the same argument, so this only has to decide
    which one it was given and reject anything that is neither. Inline JSON is
    checked first: a filesystem path is not valid JSON, so there is no ambiguity
    in practice.
    """
    if json_schema is None:
        return None

    value = json_schema.strip()
    if not value:
        raise ToolError(
            "`json_schema` is empty. Pass an inline JSON schema, a path to a "
            "schema file, or omit the parameter."
        )

    try:
        json.loads(value)
    except (json.JSONDecodeError, ValueError):
        pass
    else:
        return value

    path = Path(value).expanduser()
    try:
        candidate = path.resolve(strict=True)
    except (OSError, RuntimeError):
        candidate = None

    if candidate is not None and candidate.is_file():
        return str(candidate)

    raise ToolError(
        f"`json_schema` is neither valid JSON nor an existing file: {value!r}. "
        "Pass an inline JSON schema string, or the path to a .json schema file."
    )


# --------------------------------------------------------------------------- #
# CLI invocation
#
# Verified empirically against agy 1.1.24 (its headless-mode docs were
# inconsistent, so this reflects what the binary actually does):
#   * Default headless mode auto-DENIES the "write_file" permission, because it
#     cannot prompt. This CHANGED from 1.1.22, where writes inside the workspace
#     were auto-approved; a plain delegation that writes anything is now CANCELED
#     with "a tool required the write_file permission". Hence allow_edits.
#   * Default headless mode likewise auto-DENIES the "command" (shell)
#     permission. Fails closed, no hang. A task that needs a shell is CANCELED
#     outright rather than degraded.
#   * --mode plan investigates and writes a plan without editing the workspace.
#   * --mode accept-edits auto-approves file writes and still DENIES the shell,
#     so it is the middle tier between the two. But it is NOT path-confined:
#     verified that under accept-edits agy writes to absolute paths entirely
#     outside --add-dir. It narrows the grant to "no arbitrary commands", not
#     to "only this directory".
#   * --add-dir is NOT a hard path sandbox. Out-of-workspace access raises a
#     permission request that headless mode auto-denies, and that denial is what
#     confines the agent. Under --dangerously-skip-permissions the denial becomes
#     approval: verified that agy then reads and writes paths entirely outside
#     --add-dir. So allow_shell=True means no file confinement at all.
#   * READING named files inside --add-dir needs NO permission grant, including
#     images: verified against agy 1.3.3 that a plain delegation transcribed
#     text out of a .png with no flags beyond the defaults. What fails is
#     ENUMERATING a directory ("every .png in here"), because the agent reaches
#     for a shell to list it and the command permission is auto-denied. Naming
#     the files in the task avoids that entirely, so vision and bulk extraction
#     work in the most confined mode. See the README security model.
# --------------------------------------------------------------------------- #


def _find_cli_binary() -> Path:
    """Locates the agy executable.

    Checked in order: an explicit override (also how tests point this at a
    stand-in), PATH, then the default Windows install location -- agy is not
    reliably on PATH in the shell that a freshly-installed session inherits.
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
    allow_edits: bool,
    read_only: bool,
    timeout_seconds: int,
    model: str | None,
    conversation_id: str | None = None,
    effort: str | None = None,
    extra_dirs: list[Path] | None = None,
    json_schema: str | None = None,
) -> list[str]:
    args = [
        # The prompt is attached to the flag rather than passed as the next
        # argv element. With `--print <task>` the CLI takes whatever follows as
        # the prompt, so a flag appearing there would be swallowed instead:
        # `--print --effort high 'prompt'` sets the prompt to "--effort" and
        # silently drops the real one. agy 1.3.3 detects that case and errors,
        # but attaching the value makes it structurally impossible regardless of
        # how the remaining flags are ordered.
        f"--print={task}",
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
    for extra in extra_dirs or []:
        # --add-dir is repeatable; each extra directory joins the workspace
        # without widening `directory` itself.
        args += ["--add-dir", str(extra)]
    if effort:
        args += ["--effort", effort]
    if json_schema:
        args += ["--json-schema", json_schema]
    if conversation_id:
        # Explicit id rather than agy's --continue ("most recent"), which would
        # be racy when delegations run concurrently.
        args += ["--conversation", conversation_id]
    if read_only:
        args += ["--mode", "plan"]
    elif allow_edits and not allow_shell:
        # Redundant under --dangerously-skip-permissions, which already approves
        # writes, so only the narrower grant is spelled out here.
        args += ["--mode", "accept-edits"]
    if allow_shell:
        args += ["--dangerously-skip-permissions"]
    if model:
        args += ["--model", model]
    return args


@dataclass
class DelegationResult:
    """Everything worth reporting back about one agy run."""

    text: str
    elapsed_seconds: float
    status: str | None = None
    usage: dict | None = None
    raw_stderr: str = ""
    parse_error: str | None = None
    conversation_id: str | None = None
    # Present when json_schema was supplied: the CLI returns the schema-conformant
    # object already parsed, which is more reliable than re-parsing the prose.
    structured_output: object | None = None
    # The CLI reports auto-denied permissions as structured data as well as in
    # stderr prose. Preferred for diagnosing a CANCELED run.
    denied_actions: list[dict] | None = None


async def _run_delegation(
    task: str,
    workspace: Path,
    *,
    allow_shell: bool,
    allow_edits: bool,
    read_only: bool,
    timeout_seconds: int,
    model: str | None,
    conversation_id: str | None = None,
    effort: str | None = None,
    extra_dirs: list[Path] | None = None,
    json_schema: str | None = None,
) -> DelegationResult:
    binary = _find_cli_binary()
    command = _cli_command_prefix(binary) + _build_cli_args(
        task,
        workspace,
        allow_shell=allow_shell,
        allow_edits=allow_edits,
        read_only=read_only,
        timeout_seconds=timeout_seconds,
        model=model,
        conversation_id=conversation_id,
        effort=effort,
        extra_dirs=extra_dirs,
        json_schema=json_schema,
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
        # so a subprocess is never left running unattended.
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
        return DelegationResult(
            text=stdout or "(agy produced no parseable output)",
            elapsed_seconds=elapsed,
            raw_stderr=stderr,
            parse_error=str(exc),
        )

    text = (payload.get("response") or "").strip()
    if not text:
        text = "(agy finished without a response; see status/diagnostics below)"

    denied = payload.get("denied_actions")
    return DelegationResult(
        text=text,
        elapsed_seconds=elapsed,
        status=payload.get("status"),
        usage=payload.get("usage"),
        raw_stderr=stderr,
        conversation_id=payload.get("conversation_id"),
        structured_output=payload.get("structured_output"),
        denied_actions=denied if isinstance(denied, list) and denied else None,
    )


def _format_result(result: DelegationResult, workspace: Path) -> str:
    """Renders the result for the calling agent: answer first, evidence after."""
    lines = [result.text]

    if result.structured_output is not None:
        # The caller supplied a json_schema, so the validated object is the
        # actual deliverable. Emit it verbatim rather than making the caller
        # re-parse it out of the prose above.
        try:
            rendered = json.dumps(result.structured_output, indent=2)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            rendered = repr(result.structured_output)
        lines += ["", "Structured output:", "```json", rendered, "```"]

    lines += [
        "",
        "---",
        f"_Antigravity sub-agent - `{workspace}` - {result.elapsed_seconds:.1f}s_",
    ]

    if result.usage:
        total = result.usage.get("total_tokens")
        if total:
            lines.append(f"_Tokens: {total:,}_")

    if result.conversation_id:
        # Surfaced so a follow-up call can pass conversation_id and keep this
        # session's context. Note resuming often costs MORE tokens, not fewer.
        lines.append(f"_conversation_id: `{result.conversation_id}`_")

    if result.parse_error:
        lines.append(
            f"_agy's output was not valid JSON ({result.parse_error}); showing "
            "raw output above instead of a parsed response._"
        )

    if result.raw_stderr:
        # agy's stderr carries diagnostics (denied permissions, progress) that
        # matter for understanding a short or empty response -- capped so a
        # noisy run doesn't dominate the reply.
        snippet = result.raw_stderr[:400]
        suffix = "..." if len(result.raw_stderr) > 400 else ""
        lines.append(f"_agy diagnostics: {snippet}{suffix}_")

    # An auto-denied permission names the tool but not the flag that would have
    # allowed it, which makes an empty CANCELED run look inexplicable. Name the
    # fix instead of leaving the caller to guess. Prefer the structured
    # denied_actions field; fall back to the stderr prose, which is all older
    # CLI versions provide.
    denied_names = {
        str(entry.get("action", "")).lower()
        for entry in (result.denied_actions or [])
        if isinstance(entry, dict)
    }
    stderr_text = result.raw_stderr or ""
    if "write_file" in denied_names or (
        "write_file" in stderr_text and "permission" in stderr_text
    ):
        lines.append(
            "_Nothing was written: this run had no edit permission. Retry "
            "with `allow_edits=true` to let the sub-agent write files._"
        )
    elif "command" in denied_names or '"command" permission' in stderr_text:
        lines.append(
            "_This task needed a shell command, which is denied by default. "
            "Most often that is directory enumeration: naming the files "
            "explicitly in the task avoids it entirely and needs no extra "
            "permission. Otherwise retry with `allow_shell=true`, but only if "
            "running commands is genuinely required._"
        )

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

if DEFAULT_ALLOW_SHELL:
    logger.warning(
        "ANTIGRAVITY_ALLOW_SHELL is set: delegations default to shell access, "
        "which removes directory confinement and lets sub-agents read other "
        "delegations' stored transcripts. Pass allow_shell=false per call to "
        "opt back out."
    )

if DEFAULT_ALLOW_EDITS:
    logger.warning(
        "ANTIGRAVITY_ALLOW_EDITS is set: delegations default to write access, "
        "which is not confined to the target directory. Pass allow_edits=false "
        "per call to opt back out."
    )


@mcp.tool
async def delegate_to_antigravity(
    task: str,
    directory: str,
    allow_edits: bool | None = None,
    allow_shell: bool | None = None,
    read_only: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    model: str | None = DEFAULT_MODEL,
    conversation_id: str | None = None,
    effort: str | None = None,
    extra_dirs: list[str] | None = None,
    json_schema: str | None = None,
) -> str:
    """Delegate a self-contained coding or research task to a Google Antigravity sub-agent.

    Runs the Antigravity CLI locally against `directory` and works autonomously
    until it produces a final answer, billing against the Google account `agy`
    is logged into. It cannot ask follow-up questions, so state the task
    completely: what to do, what "done" looks like, and any constraints.

    Good for well-scoped work you would otherwise do file-by-file yourself:
    implementing a component, adding type hints or tests across a module,
    auditing a package for a pattern. Review what comes back -- treat it as a
    pull request from a capable contractor, not as finished work.

    Args:
        task: The complete task description. Self-contained and specific.
        directory: Absolute path to the existing directory the sub-agent works
            in, passed to agy as `--add-dir`. With allow_edits and allow_shell
            both off this effectively confines the agent, because every
            out-of-workspace access needs a permission that headless mode
            auto-denies. It is NOT a hard path sandbox -- see allow_edits.
        allow_edits: REQUIRED FOR ANY TASK THAT WRITES FILES. Passes
            `--mode accept-edits`. Without it, headless agy auto-denies the
            write_file permission and the run is CANCELED having changed
            nothing, so a plain delegation can read and report but never edit.
            The grant is narrower than allow_shell -- the shell stays denied,
            so no arbitrary commands -- but it is NOT path-confined: verified
            that under accept-edits agy writes to absolute paths outside
            `directory`. Prefer it over allow_shell for anything that only
            needs to edit files. Defaults to off (or to the
            ANTIGRAVITY_ALLOW_EDITS environment variable, if the operator set
            it).
        allow_shell: DANGEROUS. Passes `--dangerously-skip-permissions`, which
            auto-approves every tool call agy makes -- agy has no shell-only
            approval flag. This also removes directory confinement: verified
            that with this flag agy reads and writes files anywhere on the
            filesystem, and will read other delegations' stored transcripts
            under ~/.gemini/antigravity-cli/. Treat it as granting full
            user-account access. Implies allow_edits. Defaults to off (or to
            the ANTIGRAVITY_ALLOW_SHELL environment variable, if the operator
            set it). Reach for it only when the task genuinely needs to run
            commands -- installing packages, running tests, git operations. A
            task that merely writes files wants allow_edits instead.
        read_only: Runs agy in plan mode (`--mode plan`): it investigates and
            writes an implementation plan, but does not modify files in
            `directory`. Cannot be combined with allow_edits or allow_shell.
        timeout_seconds: Abort the run after this many seconds (max 3600).
            Passed through to agy's own `--print-timeout`.
        model: Optional model override (see `agy models` for valid IDs;
            includes non-Gemini models depending on your account).
        conversation_id: Resume a previous run's conversation, keeping its
            context (prior instructions, conventions, decisions) so you need
            not restate them. Pass the `conversation_id` from an earlier result.
            Use this for CONTINUITY, not to save tokens -- measured, resuming
            can cost either more or less than a fresh run depending on the task.
            Omit it for independent tasks.
        effort: Reasoning effort for this run: "low", "medium", "high",
            "xhigh" or "max". Omitted leaves the account default untouched.
            "low" is markedly cheaper and is usually enough for mechanical work
            such as transcription, renaming, or applying a stated pattern.
        extra_dirs: Additional absolute directory paths to add to the
            workspace, one `--add-dir` each. Use this when inputs and outputs
            live in different trees, instead of widening `directory` to a
            common ancestor. Each must already exist. Subject to the same
            permission rules as `directory`.
        json_schema: Either an inline JSON schema string or a path to a .json
            schema file, constraining the sub-agent's final answer. When set,
            the validated object is returned verbatim under "Structured
            output", which is more reliable than re-parsing prose. Useful for
            bulk extraction work such as transcription, where you want rows
            rather than a narrative.

    Returns:
        agy's final answer, the validated object when `json_schema` was used,
        then token usage, the conversation_id for chaining, and any diagnostics.
    """
    if not task or not task.strip():
        raise ToolError(
            "`task` is required: describe the work the sub-agent should carry out."
        )

    # An explicit allow_shell/allow_edits=True alongside read_only is a genuine
    # contradiction and should be reported. But when either is merely inheriting
    # an operator's ANTIGRAVITY_ALLOW_* default, an explicit read_only is the
    # more specific intent and quietly wins -- otherwise setting those variables
    # would break every read_only call.
    if read_only:
        if allow_shell:
            raise ToolError(
                "`read_only` and `allow_shell` are contradictory: shell access "
                "would let the sub-agent modify the workspace. Enable at most one."
            )
        if allow_edits:
            raise ToolError(
                "`read_only` and `allow_edits` are contradictory: plan mode "
                "exists precisely to leave the workspace unmodified. Enable at "
                "most one."
            )
        allow_shell = False
        allow_edits = False
    else:
        if allow_shell is None:
            allow_shell = DEFAULT_ALLOW_SHELL
        if allow_edits is None:
            allow_edits = DEFAULT_ALLOW_EDITS
        # --dangerously-skip-permissions already approves writes; keeping the
        # flags consistent means the logged state matches what agy can do.
        if allow_shell:
            allow_edits = True

    workspace = _resolve_workspace(directory)
    timeout = _validate_timeout(timeout_seconds)
    chosen_effort = _validate_effort(effort)
    resolved_extra_dirs = _resolve_extra_dirs(extra_dirs)
    resolved_schema = _resolve_json_schema(json_schema)

    logger.info(
        "Delegating to agy in %s (shell=%s, edits=%s, read_only=%s, timeout=%ss, "
        "effort=%s, extra_dirs=%d, json_schema=%s)",
        workspace,
        allow_shell,
        allow_edits,
        read_only,
        timeout,
        chosen_effort or "default",
        len(resolved_extra_dirs),
        bool(resolved_schema),
    )

    try:
        result = await _run_delegation(
            task,
            workspace,
            allow_shell=allow_shell,
            allow_edits=allow_edits,
            read_only=read_only,
            timeout_seconds=timeout,
            model=model,
            conversation_id=conversation_id,
            effort=chosen_effort,
            extra_dirs=resolved_extra_dirs,
            json_schema=resolved_schema,
        )
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a usable message to caller
        logger.exception("Delegation failed in %s", workspace)
        raise ToolError(
            f"The Antigravity sub-agent failed: {type(exc).__name__}: {exc}"
        ) from exc

    logger.info(
        "Delegation finished in %.1fs (status=%s)",
        result.elapsed_seconds,
        result.status,
    )
    return _format_result(result, workspace)


if __name__ == "__main__":
    # stdio transport: the banner would only clutter the host's stderr log.
    mcp.run(show_banner=False)
