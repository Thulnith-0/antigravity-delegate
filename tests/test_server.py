"""Tests for the Antigravity delegation MCP server.

These exercise configuration, validation, formatting and the MCP tool boundary.
They never make a live Gemini call, so they run without credentials.
"""

from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import antigravity_mcp as srv  # noqa: E402
from google.antigravity.types import StopReason  # noqa: E402


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "example.py").write_text("print('hello')\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in srv.API_KEY_ENV_VARS + srv.VERTEX_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def with_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used-for-live-calls")


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
# Credential detection
# --------------------------------------------------------------------------- #


def test_missing_credentials_raise(no_credentials: None) -> None:
    with pytest.raises(ToolError, match="No Gemini credentials"):
        srv._check_credentials()


def test_api_key_satisfies_credentials(with_credentials: None) -> None:
    srv._check_credentials()


def test_vertex_flag_satisfies_credentials(
    no_credentials: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    srv._check_credentials()


# --------------------------------------------------------------------------- #
# Environment parsing
# --------------------------------------------------------------------------- #


def test_env_int_falls_back_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_INT", "not-a-number")
    assert srv._env_int("SOME_INT", 900) == 900


def test_env_int_reads_valid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOME_INT", "120")
    assert srv._env_int("SOME_INT", 900) == 120


# --------------------------------------------------------------------------- #
# Agent configuration
#
# Regression guard: LocalAgentConfig fills in a default CapabilitiesConfig()
# that is identical to an explicitly constructed one, so simply omitting
# `capabilities` does NOT produce a read-only agent. read_only must restrict
# the tool set explicitly.
# --------------------------------------------------------------------------- #


def test_workspace_is_the_configured_sandbox(workspace: Path) -> None:
    config = srv._build_config(
        workspace, allow_shell=False, read_only=False, model=None
    )
    assert [Path(p) for p in config.workspaces] == [workspace]


def test_read_only_excludes_mutating_tools(workspace: Path) -> None:
    config = srv._build_config(
        workspace, allow_shell=False, read_only=True, model=None
    )
    enabled = {getattr(t, "value", t) for t in config.capabilities.enabled_tools}
    assert enabled == set(srv.READ_ONLY_TOOL_NAMES)
    for tool in srv.MUTATING_TOOL_NAMES:
        assert tool not in enabled


def test_read_only_denies_mutating_tools_by_policy(workspace: Path) -> None:
    config = srv._build_config(
        workspace, allow_shell=False, read_only=True, model=None
    )
    denied = {p.tool for p in config.policies}
    for tool in srv.MUTATING_TOOL_NAMES:
        assert tool in denied


def test_read_only_disables_subagents(workspace: Path) -> None:
    config = srv._build_config(
        workspace, allow_shell=False, read_only=True, model=None
    )
    assert config.capabilities.enable_subagents is False


def test_read_only_differs_from_write_mode(workspace: Path) -> None:
    read_only = srv._build_config(
        workspace, allow_shell=False, read_only=True, model=None
    )
    writable = srv._build_config(
        workspace, allow_shell=False, read_only=False, model=None
    )
    assert read_only.capabilities != writable.capabilities


def test_allow_shell_installs_allow_all_policy(workspace: Path) -> None:
    config = srv._build_config(
        workspace, allow_shell=True, read_only=False, model=None
    )
    assert any("allow_all" in (p.name or "") for p in config.policies)


def test_shell_is_off_by_default(workspace: Path) -> None:
    config = srv._build_config(
        workspace, allow_shell=False, read_only=False, model=None
    )
    assert not any("allow_all" in (p.name or "") for p in config.policies)


def test_model_override_is_applied(workspace: Path) -> None:
    config = srv._build_config(
        workspace, allow_shell=False, read_only=False, model="gemini-3-pro-preview"
    )
    assert config.model == "gemini-3-pro-preview"


# --------------------------------------------------------------------------- #
# Prompt construction
# --------------------------------------------------------------------------- #


def test_prompt_states_shell_is_unavailable(workspace: Path) -> None:
    prompt = srv._build_prompt("Do a thing", workspace, allow_shell=False)
    assert "disabled" in prompt
    assert str(workspace) in prompt
    assert "Do a thing" in prompt


def test_prompt_grants_shell_when_enabled(workspace: Path) -> None:
    prompt = srv._build_prompt("Do a thing", workspace, allow_shell=True)
    assert "may run shell commands" in prompt


# --------------------------------------------------------------------------- #
# Result formatting
# --------------------------------------------------------------------------- #


def test_format_puts_answer_first(workspace: Path) -> None:
    result = srv.DelegationResult(
        text="Refactored the parser.",
        elapsed_seconds=12.34,
        tool_calls=Counter({"view_file": 3, "edit_file": 1}),
        files_touched=["parser.py"],
        total_tokens=4242,
        stop_reason=StopReason.UNSPECIFIED,
    )
    out = srv._format_result(result, workspace)
    assert out.startswith("Refactored the parser.")
    assert "view_file x3" in out
    assert "parser.py" in out
    assert "4,242" in out
    assert "Warning" not in out


def test_format_hoists_early_stop_warning(workspace: Path) -> None:
    result = srv.DelegationResult(
        text="Partial work.",
        elapsed_seconds=1.0,
        stop_reason=StopReason.MAX_TOOL_CALLS_EXCEEDED,
    )
    out = srv._format_result(result, workspace)
    assert out.startswith("> **Warning:**")
    assert "MAX_TOOL_CALLS_EXCEEDED" in out


def test_format_truncates_long_path_lists(workspace: Path) -> None:
    result = srv.DelegationResult(
        text="Done.",
        elapsed_seconds=1.0,
        files_touched=[f"file_{i}.py" for i in range(40)],
    )
    out = srv._format_result(result, workspace)
    assert "+25 more" in out


def test_format_reports_trace_failure_without_losing_answer(workspace: Path) -> None:
    result = srv.DelegationResult(
        text="The answer survives.", elapsed_seconds=1.0, trace_error="stream closed"
    )
    out = srv._format_result(result, workspace)
    assert out.startswith("The answer survives.")
    assert "stream closed" in out


# --------------------------------------------------------------------------- #
# MCP tool boundary
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_both_tools_registered_with_expected_schema() -> None:
    async with Client(srv.mcp) as client:
        tools = await client.list_tools()

    by_name = {t.name: t for t in tools}
    assert set(by_name) == {"delegate_to_antigravity", "delegate_to_antigravity_cli"}

    for tool in by_name.values():
        schema = tool.inputSchema
        assert set(schema["required"]) == {"task", "directory"}
        for param in (
            "task",
            "directory",
            "allow_shell",
            "read_only",
            "timeout_seconds",
            "model",
        ):
            assert param in schema["properties"]
        assert schema["properties"]["allow_shell"]["default"] is False
        assert schema["properties"]["read_only"]["default"] is False
        assert (tool.description or "").strip()


async def _call_expecting_error(
    arguments: dict, tool_name: str = "delegate_to_antigravity"
) -> str:
    async with Client(srv.mcp) as client:
        result = await client.call_tool(tool_name, arguments, raise_on_error=False)
    assert result.is_error, "expected the call to fail"
    return "".join(getattr(block, "text", "") for block in result.content)


@pytest.mark.asyncio
async def test_blank_task_is_rejected(workspace: Path, with_credentials: None) -> None:
    message = await _call_expecting_error({"task": "   ", "directory": str(workspace)})
    assert "`task` is required" in message


@pytest.mark.asyncio
async def test_read_only_and_allow_shell_conflict(
    workspace: Path, with_credentials: None
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
async def test_missing_directory_is_rejected(
    workspace: Path, with_credentials: None
) -> None:
    message = await _call_expecting_error(
        {"task": "Audit this", "directory": str(workspace / "nope")}
    )
    assert "Could not resolve" in message


@pytest.mark.asyncio
async def test_missing_credentials_reported_cleanly(
    workspace: Path, no_credentials: None
) -> None:
    message = await _call_expecting_error(
        {"task": "Audit this", "directory": str(workspace)}
    )
    assert "No Gemini credentials" in message
    assert "GEMINI_API_KEY" in message


# --------------------------------------------------------------------------- #
# CLI backend (agy)
# --------------------------------------------------------------------------- #

FAKE_AGY = Path(__file__).resolve().parent / "fakes" / "fake_agy.py"


@pytest.fixture
def fake_agy(monkeypatch: pytest.MonkeyPatch):
    """Points the CLI backend at the fake stand-in instead of a real binary."""
    monkeypatch.setenv("ANTIGRAVITY_CLI_PATH", str(FAKE_AGY))
    # CLI_SUBPROCESS_TIMEOUT_BUFFER is read from the env once at import time
    # (like DEFAULT_TIMEOUT_SECONDS), so monkeypatching the env var here would
    # have no effect on the already-computed constant. Patch it directly so
    # the timeout test doesn't sit through the real ~30s default buffer.
    monkeypatch.setattr(srv, "CLI_SUBPROCESS_TIMEOUT_BUFFER", 1)

    def _set(behavior: str) -> None:
        monkeypatch.setenv("FAKE_AGY_BEHAVIOR", behavior)

    _set("success")
    return _set


def test_find_cli_binary_honors_override(fake_agy) -> None:
    assert srv._find_cli_binary() == FAKE_AGY


def test_find_cli_binary_rejects_missing_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTIGRAVITY_CLI_PATH", str(tmp_path / "nope.exe"))
    with pytest.raises(ToolError, match="does not exist"):
        srv._find_cli_binary()


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


def test_build_cli_args_default(workspace: Path) -> None:
    args = srv._build_cli_args(
        "do the thing", workspace,
        allow_shell=False, read_only=False, timeout_seconds=120, model=None,
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
        allow_shell=False, read_only=True, timeout_seconds=60, model=None,
    )
    i = args.index("--mode")
    assert args[i + 1] == "plan"


def test_build_cli_args_allow_shell_skips_permissions(workspace: Path) -> None:
    args = srv._build_cli_args(
        "run tests", workspace,
        allow_shell=True, read_only=False, timeout_seconds=60, model=None,
    )
    assert "--dangerously-skip-permissions" in args


def test_build_cli_args_model_override(workspace: Path) -> None:
    args = srv._build_cli_args(
        "x", workspace,
        allow_shell=False, read_only=False, timeout_seconds=60,
        model="claude-sonnet-4-6",
    )
    i = args.index("--model")
    assert args[i + 1] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_cli_delegation_success(
    workspace: Path, fake_agy
) -> None:
    async with Client(srv.mcp) as client:
        result = await client.call_tool(
            "delegate_to_antigravity_cli",
            {"task": "do a thing", "directory": str(workspace)},
        )
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert "fake response for argv=" in text
    assert "Tokens: 49" in text
    assert "Warning" not in text


@pytest.mark.asyncio
async def test_cli_delegation_surfaces_shell_denied_diagnostics(
    workspace: Path, fake_agy
) -> None:
    fake_agy("shell_denied")
    async with Client(srv.mcp) as client:
        result = await client.call_tool(
            "delegate_to_antigravity_cli",
            {"task": "run a command", "directory": str(workspace)},
        )
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert not result.is_error
    assert "finished without a response" in text
    assert "auto-denied" in text


@pytest.mark.asyncio
async def test_cli_delegation_handles_malformed_json(
    workspace: Path, fake_agy
) -> None:
    fake_agy("malformed_json")
    async with Client(srv.mcp) as client:
        result = await client.call_tool(
            "delegate_to_antigravity_cli",
            {"task": "x", "directory": str(workspace)},
        )
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert not result.is_error
    assert "not json at all" in text
    assert "not valid JSON" in text


@pytest.mark.asyncio
async def test_cli_delegation_reports_nonzero_exit(
    workspace: Path, fake_agy
) -> None:
    fake_agy("nonzero_no_output")
    async with Client(srv.mcp) as client:
        result = await client.call_tool(
            "delegate_to_antigravity_cli",
            {"task": "x", "directory": str(workspace)},
            raise_on_error=False,
        )
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert result.is_error
    assert "exited with code 1" in text
    assert "simulated crash" in text


@pytest.mark.asyncio
async def test_cli_delegation_kills_hung_process_and_times_out(
    workspace: Path, fake_agy
) -> None:
    fake_agy("hang")
    async with Client(srv.mcp) as client:
        result = await client.call_tool(
            "delegate_to_antigravity_cli",
            {"task": "x", "directory": str(workspace), "timeout_seconds": 1},
            raise_on_error=False,
        )
    text = "".join(getattr(b, "text", "") for b in result.content)
    assert result.is_error
    assert "did not finish within 1s" in text


@pytest.mark.asyncio
async def test_cli_delegation_rejects_conflicting_flags(
    workspace: Path, fake_agy
) -> None:
    message = await _call_expecting_error(
        {
            "task": "x",
            "directory": str(workspace),
            "read_only": True,
            "allow_shell": True,
        },
        tool_name="delegate_to_antigravity_cli",
    )
    assert "contradictory" in message


@pytest.mark.asyncio
async def test_cli_delegation_rejects_blank_task(workspace: Path, fake_agy) -> None:
    message = await _call_expecting_error(
        {"task": "   ", "directory": str(workspace)},
        tool_name="delegate_to_antigravity_cli",
    )
    assert "`task` is required" in message


@pytest.mark.asyncio
async def test_cli_delegation_rejects_missing_directory(
    workspace: Path, fake_agy
) -> None:
    message = await _call_expecting_error(
        {"task": "x", "directory": str(workspace / "nope")},
        tool_name="delegate_to_antigravity_cli",
    )
    assert "Could not resolve" in message
