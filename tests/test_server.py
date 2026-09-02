"""Tests for the antigravity-delegate MCP server.

These exercise validation, argv construction, subprocess handling, result
formatting and the MCP tool boundary. The agy binary is replaced by a stand-in
(tests/fakes/fake_agy.py), so nothing here makes a live call: the suite needs no
credentials and no Antigravity install.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import antigravity_delegate as srv  # noqa: E402

TOOL = "delegate_to_antigravity"
FAKE_AGY = Path(__file__).resolve().parent / "fakes" / "fake_agy.py"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "example.py").write_text("print('hello')\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def fake_agy(monkeypatch: pytest.MonkeyPatch):
    """Points the server at the fake agy stand-in instead of a real binary."""
    monkeypatch.setenv("ANTIGRAVITY_CLI_PATH", str(FAKE_AGY))
    # CLI_SUBPROCESS_TIMEOUT_BUFFER is read from the env once at import time, so
    # monkeypatching the env var would not affect the already-computed constant.
    # Patch it directly so the timeout test doesn't sit through the real buffer.
    monkeypatch.setattr(srv, "CLI_SUBPROCESS_TIMEOUT_BUFFER", 1)

    def _set(behavior: str) -> None:
        monkeypatch.setenv("FAKE_AGY_BEHAVIOR", behavior)

    _set("success")
    return _set


# --------------------------------------------------------------------------- #
# Workspace validation
# --------------------------------------------------------------------------- #


def test_resolve_workspace_accepts_existing_dir(workspace: Path) -> None:
    assert srv._resolve_workspace(str(workspace)) == workspace.resolve()


@pytest.mark.parametrize("value", ["", "   "])
def test_resolve_workspace_rejects_blank(value: str) -> None:
    with pytest.raises(ToolError, match="required"):
        srv._resolve_workspace(value)


def test_resolve_workspace_rejects_missing(workspace: Path) -> None:
    with pytest.raises(ToolError, match="Could not resolve"):
        srv._resolve_workspace(str(workspace / "does_not_exist"))


def test_resolve_workspace_rejects_file(workspace: Path) -> None:
    with pytest.raises(ToolError, match="must be a directory"):
        srv._resolve_workspace(str(workspace / "example.py"))


# --------------------------------------------------------------------------- #
# Timeout validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [0, -1])
def test_timeout_rejects_non_positive(value: int) -> None:
    with pytest.raises(ToolError, match="positive"):
        srv._validate_timeout(value)


def test_timeout_rejects_over_maximum() -> None:
    with pytest.raises(ToolError, match="exceed"):
        srv._validate_timeout(srv.MAX_TIMEOUT_SECONDS + 1)


def test_timeout_accepts_boundary() -> None:
    assert srv._validate_timeout(srv.MAX_TIMEOUT_SECONDS) == srv.MAX_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# Environment parsing
# --------------------------------------------------------------------------- #


def test_env_int_falls_back_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_INT", "not-a-number")
    assert srv._env_int("SOME_INT", 900) == 900


def test_env_int_reads_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_INT", "120")
    assert srv._env_int("SOME_INT", 900) == 120


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_env_bool_accepts_truthy(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("SOME_BOOL", raw)
    assert srv._env_bool("SOME_BOOL", False) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "maybe", ""])
def test_env_bool_rejects_everything_else(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("SOME_BOOL", raw)
    assert srv._env_bool("SOME_BOOL", False) is False


def test_env_bool_unset_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOME_BOOL", raising=False)
    assert srv._env_bool("SOME_BOOL", True) is True


def test_allow_shell_ships_disabled() -> None:
    # Guards the shipped security posture: allow_shell removes directory
    # confinement, so it must never default on without an operator opting in.
    assert srv.DEFAULT_ALLOW_SHELL is False


def test_allow_edits_ships_disabled() -> None:
    # accept-edits blocks the shell but is NOT path-confined -- it writes
    # outside --add-dir -- so it is an opt-in grant like allow_shell.
    assert srv.DEFAULT_ALLOW_EDITS is False


# --------------------------------------------------------------------------- #
# Binary discovery
# --------------------------------------------------------------------------- #


def test_find_cli_binary_honors_override(fake_agy) -> None:
    assert srv._find_cli_binary() == FAKE_AGY


def test_find_cli_binary_rejects_missing_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTIGRAVITY_CLI_PATH", str(tmp_path / "nope.exe"))
    with pytest.raises(ToolError, match="does not exist"):
        srv._find_cli_binary()


def test_find_cli_binary_falls_back_to_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTIGRAVITY_CLI_PATH", raising=False)
    monkeypatch.setattr(srv.shutil, "which", lambda name: str(tmp_path / "agy"))
    assert srv._find_cli_binary() == tmp_path / "agy"


def test_find_cli_binary_raises_when_nothing_found(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("ANTIGRAVITY_CLI_PATH", raising=False)
    monkeypatch.setattr(srv.shutil, "which", lambda name: None)
    monkeypatch.setattr(srv, "WINDOWS_DEFAULT_CLI_PATH", tmp_path / "missing.exe")
    with pytest.raises(ToolError, match="Could not find the Antigravity CLI"):
        srv._find_cli_binary()


def test_cli_command_prefix_routes_py_through_interpreter() -> None:
    assert srv._cli_command_prefix(FAKE_AGY) == [sys.executable, str(FAKE_AGY)]


def test_cli_command_prefix_runs_binary_directly(tmp_path: Path) -> None:
    fake_exe = tmp_path / "agy.exe"
    assert srv._cli_command_prefix(fake_exe) == [str(fake_exe)]


# --------------------------------------------------------------------------- #
# argv construction
# --------------------------------------------------------------------------- #


def test_build_cli_args_default(workspace: Path) -> None:
    args = srv._build_cli_args(
        "do the thing", workspace,
        allow_shell=False, allow_edits=False, read_only=False, timeout_seconds=120, model=None,
    )
    assert args[:2] == ["-p", "do the thing"]
    assert "--add-dir" in args and str(workspace) in args
    assert "--output-format" in args and "json" in args
    assert "--print-timeout" in args and "120s" in args
    assert "--disable-slash-commands" in args
    assert "--mode" not in args
    assert "--dangerously-skip-permissions" not in args


def test_build_cli_args_read_only_uses_plan_mode(workspace: Path) -> None:
    args = srv._build_cli_args(
        "audit this", workspace,
        allow_shell=False, allow_edits=False, read_only=True, timeout_seconds=60, model=None,
    )
    assert args[args.index("--mode") + 1] == "plan"


def test_build_cli_args_allow_shell_skips_permissions(workspace: Path) -> None:
    args = srv._build_cli_args(
        "run tests", workspace,
        allow_shell=True, allow_edits=True, read_only=False, timeout_seconds=60, model=None,
    )
    assert "--dangerously-skip-permissions" in args


def test_build_cli_args_allow_edits_uses_accept_edits_mode(workspace: Path) -> None:
    # Without this, headless agy auto-denies write_file and the run is CANCELED
    # having changed nothing.
    args = srv._build_cli_args(
        "write a file", workspace,
        allow_shell=False, allow_edits=True, read_only=False, timeout_seconds=60, model=None,
    )
    assert args[args.index("--mode") + 1] == "accept-edits"
    # The narrower grant must not quietly escalate to full permission bypass.
    assert "--dangerously-skip-permissions" not in args


def test_build_cli_args_allow_shell_omits_redundant_accept_edits(
    workspace: Path,
) -> None:
    # --dangerously-skip-permissions already approves writes; passing both would
    # be redundant argv noise.
    args = srv._build_cli_args(
        "run tests", workspace,
        allow_shell=True, allow_edits=True, read_only=False, timeout_seconds=60, model=None,
    )
    assert "--mode" not in args


def test_build_cli_args_read_only_beats_allow_edits(workspace: Path) -> None:
    # Only one --mode can win, and plan mode is the one that keeps the promise
    # of not touching the workspace.
    args = srv._build_cli_args(
        "audit", workspace,
        allow_shell=False, allow_edits=True, read_only=True, timeout_seconds=60, model=None,
    )
    assert args[args.index("--mode") + 1] == "plan"
    assert "accept-edits" not in args


def test_build_cli_args_omits_conversation_by_default(workspace: Path) -> None:
    args = srv._build_cli_args(
        "x", workspace,
        allow_shell=False, allow_edits=False, read_only=False, timeout_seconds=60, model=None,
    )
    assert "--conversation" not in args


def test_build_cli_args_passes_conversation_id(workspace: Path) -> None:
    args = srv._build_cli_args(
        "x", workspace,
        allow_shell=False, allow_edits=False, read_only=False, timeout_seconds=60, model=None,
        conversation_id="abc-123",
    )
    assert args[args.index("--conversation") + 1] == "abc-123"
    # agy's --continue ("most recent") is deliberately not used: it would be
    # racy when delegations run concurrently.
    assert "--continue" not in args


def test_build_cli_args_model_override(workspace: Path) -> None:
    args = srv._build_cli_args(
        "x", workspace,
        allow_shell=False, allow_edits=False, read_only=False, timeout_seconds=60,
        model="claude-sonnet-4-6",
    )
    assert args[args.index("--model") + 1] == "claude-sonnet-4-6"


# --------------------------------------------------------------------------- #
# Result formatting
# --------------------------------------------------------------------------- #


def test_format_puts_answer_first(workspace: Path) -> None:
    result = srv.DelegationResult(
        text="Refactored the parser.",
        elapsed_seconds=12.34,
        status="SUCCESS",
        usage={"total_tokens": 4242},
        conversation_id="conv-1",
    )
    out = srv._format_result(result, workspace)
    assert out.startswith("Refactored the parser.")
    assert "4,242" in out
    assert "conv-1" in out
    assert "Warning" not in out


def test_format_hoists_non_success_status(workspace: Path) -> None:
    result = srv.DelegationResult(
        text="Partial work.", elapsed_seconds=1.0, status="CANCELED"
    )
    out = srv._format_result(result, workspace)
    assert out.startswith("> **Warning:**")
    assert "CANCELED" in out


def test_format_reports_parse_failure_without_losing_output(workspace: Path) -> None:
    result = srv.DelegationResult(
        text="raw output", elapsed_seconds=1.0, parse_error="bad json"
    )
    out = srv._format_result(result, workspace)
    assert out.startswith("raw output")
    assert "not valid JSON" in out


def test_format_truncates_long_stderr(workspace: Path) -> None:
    result = srv.DelegationResult(
        text="ok", elapsed_seconds=1.0, raw_stderr="x" * 900
    )
    out = srv._format_result(result, workspace)
    assert "..." in out
    assert len(out) < 900


# --------------------------------------------------------------------------- #
# MCP tool boundary
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_tool_is_registered_with_expected_schema() -> None:
    async with Client(srv.mcp) as client:
        tools = await client.list_tools()

    assert [t.name for t in tools] == [TOOL]
    schema = tools[0].inputSchema
    assert set(schema["required"]) == {"task", "directory"}
    for param in (
        "task",
        "directory",
        "allow_shell",
        "allow_edits",
        "read_only",
        "timeout_seconds",
        "model",
        "conversation_id",
    ):
        assert param in schema["properties"]
    # allow_shell/allow_edits are nullable so an omitted value can fall back to
    # the operator's ANTIGRAVITY_ALLOW_* default; read_only stays a plain bool.
    assert schema["properties"]["allow_shell"]["default"] is None
    assert schema["properties"]["allow_edits"]["default"] is None
    assert schema["properties"]["read_only"]["default"] is False
    assert (tools[0].description or "").strip()


async def _call(arguments: dict):
    async with Client(srv.mcp) as client:
        return await client.call_tool(TOOL, arguments, raise_on_error=False)


async def _text(arguments: dict) -> str:
    result = await _call(arguments)
    return "".join(getattr(block, "text", "") for block in result.content)


async def _call_expecting_error(arguments: dict) -> str:
    result = await _call(arguments)
    assert result.is_error, "expected the call to fail"
    return "".join(getattr(block, "text", "") for block in result.content)


@pytest.mark.asyncio
async def test_blank_task_is_rejected(workspace: Path, fake_agy) -> None:
    message = await _call_expecting_error(
        {"task": "   ", "directory": str(workspace)}
    )
    assert "`task` is required" in message


@pytest.mark.asyncio
async def test_read_only_and_explicit_allow_shell_conflict(
    workspace: Path, fake_agy
) -> None:
    message = await _call_expecting_error(
        {
            "task": "Audit this",
            "directory": str(workspace),
            "read_only": True,
            "allow_shell": True,
        }
    )
    assert "contradictory" in message


@pytest.mark.asyncio
async def test_read_only_and_explicit_allow_edits_conflict(
    workspace: Path, fake_agy
) -> None:
    message = await _call_expecting_error(
        {
            "task": "Audit this",
            "directory": str(workspace),
            "read_only": True,
            "allow_edits": True,
        }
    )
    assert "contradictory" in message


@pytest.mark.asyncio
async def test_allow_edits_reaches_the_subprocess(workspace: Path, fake_agy) -> None:
    text = await _text(
        {"task": "write a file", "directory": str(workspace), "allow_edits": True}
    )
    assert "accept-edits" in text
    assert "--dangerously-skip-permissions" not in text


@pytest.mark.asyncio
async def test_edits_are_denied_by_default(workspace: Path, fake_agy) -> None:
    # The shipped default writes nothing; callers must opt in per call.
    text = await _text({"task": "x", "directory": str(workspace)})
    assert "accept-edits" not in text


@pytest.mark.asyncio
async def test_env_default_enables_edits(
    workspace: Path, fake_agy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "DEFAULT_ALLOW_EDITS", True)
    text = await _text({"task": "x", "directory": str(workspace)})
    assert "accept-edits" in text


@pytest.mark.asyncio
async def test_read_only_wins_over_edits_env_default(
    workspace: Path, fake_agy, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Same regression guard as the shell default: an operator opting into edits
    # must not turn every read_only call into an error.
    monkeypatch.setattr(srv, "DEFAULT_ALLOW_EDITS", True)
    text = await _text(
        {"task": "audit", "directory": str(workspace), "read_only": True}
    )
    assert "plan" in text and "accept-edits" not in text


@pytest.mark.asyncio
async def test_env_default_enables_shell(
    workspace: Path, fake_agy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "DEFAULT_ALLOW_SHELL", True)
    # The fake echoes argv, so this proves the default reached the subprocess.
    text = await _text({"task": "x", "directory": str(workspace)})
    assert "--dangerously-skip-permissions" in text


@pytest.mark.asyncio
async def test_explicit_false_overrides_env_default(
    workspace: Path, fake_agy, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(srv, "DEFAULT_ALLOW_SHELL", True)
    text = await _text(
        {"task": "x", "directory": str(workspace), "allow_shell": False}
    )
    assert "--dangerously-skip-permissions" not in text


@pytest.mark.asyncio
async def test_read_only_wins_over_env_default_instead_of_erroring(
    workspace: Path, fake_agy, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression guard: with the operator default on, read_only calls must still
    # work rather than tripping the contradiction check.
    monkeypatch.setattr(srv, "DEFAULT_ALLOW_SHELL", True)
    text = await _text(
        {"task": "audit", "directory": str(workspace), "read_only": True}
    )
    assert "--dangerously-skip-permissions" not in text
    assert "--mode" in text and "plan" in text


@pytest.mark.asyncio
async def test_missing_directory_is_rejected(workspace: Path, fake_agy) -> None:
    message = await _call_expecting_error(
        {"task": "Audit this", "directory": str(workspace / "nope")}
    )
    assert "Could not resolve" in message


@pytest.mark.asyncio
async def test_delegation_success(workspace: Path, fake_agy) -> None:
    text = await _text({"task": "do a thing", "directory": str(workspace)})
    assert "fake response for argv=" in text
    assert "Tokens: 49" in text


@pytest.mark.asyncio
async def test_delegation_reports_conversation_id_for_chaining(
    workspace: Path, fake_agy
) -> None:
    text = await _text({"task": "do a thing", "directory": str(workspace)})
    assert "conversation_id: `fake-conv-id`" in text


@pytest.mark.asyncio
async def test_delegation_forwards_conversation_id_to_agy(
    workspace: Path, fake_agy
) -> None:
    # The fake echoes its argv into the response, so this proves the flag
    # actually reaches the subprocess rather than only being built.
    text = await _text(
        {
            "task": "follow up",
            "directory": str(workspace),
            "conversation_id": "resume-me-42",
        }
    )
    assert "--conversation" in text
    assert "resume-me-42" in text


@pytest.mark.asyncio
async def test_delegation_surfaces_shell_denied_diagnostics(
    workspace: Path, fake_agy
) -> None:
    fake_agy("shell_denied")
    text = await _text({"task": "run a command", "directory": str(workspace)})
    assert "finished without a response" in text
    assert "auto-denied" in text


@pytest.mark.asyncio
async def test_delegation_handles_malformed_json(workspace: Path, fake_agy) -> None:
    fake_agy("malformed_json")
    text = await _text({"task": "x", "directory": str(workspace)})
    assert "not json at all" in text
    assert "not valid JSON" in text


@pytest.mark.asyncio
async def test_delegation_reports_nonzero_exit(workspace: Path, fake_agy) -> None:
    fake_agy("nonzero_no_output")
    message = await _call_expecting_error({"task": "x", "directory": str(workspace)})
    assert "exited with code 1" in message
    assert "simulated crash" in message


@pytest.mark.asyncio
async def test_delegation_kills_hung_process_and_times_out(
    workspace: Path, fake_agy
) -> None:
    fake_agy("hang")
    message = await _call_expecting_error(
        {"task": "x", "directory": str(workspace), "timeout_seconds": 1}
    )
    assert "did not finish within 1s" in message
