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
async def test_tool_is_registered_with_expected_schema() -> None:
    async with Client(srv.mcp) as client:
        tools = await client.list_tools()

    assert [t.name for t in tools] == ["delegate_to_antigravity"]
    schema = tools[0].inputSchema
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
    assert (tools[0].description or "").strip()


async def _call_expecting_error(arguments: dict) -> str:
    async with Client(srv.mcp) as client:
        result = await client.call_tool(
            "delegate_to_antigravity", arguments, raise_on_error=False
        )
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
