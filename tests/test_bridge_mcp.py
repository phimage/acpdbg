"""Tests for the live-debugger tool path: the socket bridge and the MCP server.

A fake executor stands in for the real LLDB command interpreter, so the whole
tool round-trip can be exercised without a debugger.
"""

import pytest

from acpdbg import mcp_server
from acpdbg.bridge import CommandBridge, call_bridge


@pytest.fixture()
def bridge():
    calls = []
    controls = []

    def executor(command: str) -> str:
        calls.append(command)
        return f"OUTPUT<{command}>"

    def control(action: str) -> str:
        controls.append(action)
        return f"CONTROL<{action}>"

    server = CommandBridge(executor, control=control)
    info = server.start()
    server.calls = calls  # type: ignore[attr-defined]
    server.controls = controls  # type: ignore[attr-defined]
    server.info_obj = info  # type: ignore[attr-defined]
    try:
        yield server
    finally:
        server.stop()


def test_bridge_runs_command(bridge):
    info = bridge.info_obj
    assert call_bridge(info.socket_path, info.token, "bt") == "OUTPUT<bt>"
    assert bridge.calls == ["bt"]


def test_bridge_rejects_bad_token(bridge):
    info = bridge.info_obj
    with pytest.raises(RuntimeError, match="unauthorized"):
        call_bridge(info.socket_path, "wrong-token", "bt")


def test_mcp_initialize_and_list():
    init = mcp_server._handle({"method": "initialize", "params": {"protocolVersion": "2025-06-18"}, "id": 1})
    assert init["serverInfo"]["name"] == "acpdbg-debugger"
    assert init["protocolVersion"] == "2025-06-18"

    tools = mcp_server._handle({"method": "tools/list", "params": {}, "id": 2})
    names = {t["name"] for t in tools["tools"]}
    assert names == {"debugger_command", "get_backtrace", "get_locals"}


def test_mcp_tool_call_uses_bridge(bridge, monkeypatch):
    info = bridge.info_obj
    monkeypatch.setenv("ACPDBG_BRIDGE_SOCKET", info.socket_path)
    monkeypatch.setenv("ACPDBG_BRIDGE_TOKEN", info.token)

    result = mcp_server._handle(
        {"method": "tools/call", "params": {"name": "debugger_command", "arguments": {"command": "bt"}}, "id": 3}
    )
    assert result["isError"] is False
    assert result["content"][0]["text"] == "OUTPUT<bt>"

    # Convenience tool maps to `frame variable`.
    locals_result = mcp_server._handle(
        {"method": "tools/call", "params": {"name": "get_locals", "arguments": {}}, "id": 4}
    )
    assert locals_result["content"][0]["text"] == "OUTPUT<frame variable>"
    assert "frame variable" in bridge.calls


def test_mcp_tool_call_blocks_unsafe(bridge, monkeypatch):
    info = bridge.info_obj
    monkeypatch.setenv("ACPDBG_BRIDGE_SOCKET", info.socket_path)
    monkeypatch.setenv("ACPDBG_BRIDGE_TOKEN", info.token)

    result = mcp_server._handle(
        {"method": "tools/call", "params": {"name": "debugger_command", "arguments": {"command": "continue"}}, "id": 5}
    )
    assert result["isError"] is True
    assert "safety filter" in result["content"][0]["text"]
    assert "continue" not in bridge.calls


def test_mcp_unknown_method_raises():
    with pytest.raises(mcp_server._MethodNotFound):
        mcp_server._handle({"method": "does/not/exist", "id": 9})


def test_mcp_resolves_bridge_from_file(bridge, tmp_path, monkeypatch):
    # An external MCP client (Claude Code, etc.) finds the bridge via a file
    # written by `acpdbg serve`, not the per-session env vars.
    monkeypatch.delenv("ACPDBG_BRIDGE_SOCKET", raising=False)
    monkeypatch.delenv("ACPDBG_BRIDGE_TOKEN", raising=False)
    bridge_file = tmp_path / "bridge.json"
    bridge.info_obj.to_file(str(bridge_file))
    monkeypatch.setenv("ACPDBG_BRIDGE_FILE", str(bridge_file))

    result = mcp_server._handle(
        {"method": "tools/call", "params": {"name": "get_backtrace", "arguments": {}}, "id": 1}
    )
    assert result["isError"] is False
    assert result["content"][0]["text"] == "OUTPUT<bt>"


def test_bridge_info_from_env(bridge, monkeypatch):
    from acpdbg.bridge import BridgeInfo

    info = bridge.info_obj
    for key, value in info.env().items():
        monkeypatch.setenv(key, value)
    restored = BridgeInfo.from_env()
    assert restored is not None
    assert restored.socket_path == info.socket_path
    assert restored.token == info.token


def test_bridge_info_from_env_absent(monkeypatch):
    from acpdbg.bridge import BridgeInfo, ENV_SOCKET, ENV_TOKEN

    monkeypatch.delenv(ENV_SOCKET, raising=False)
    monkeypatch.delenv(ENV_TOKEN, raising=False)
    assert BridgeInfo.from_env() is None


def _bind(bridge, monkeypatch):
    info = bridge.info_obj
    monkeypatch.setenv("ACPDBG_BRIDGE_SOCKET", info.socket_path)
    monkeypatch.setenv("ACPDBG_BRIDGE_TOKEN", info.token)


def test_control_tools_hidden_unless_enabled(monkeypatch):
    monkeypatch.delenv("ACPDBG_CONTROL", raising=False)
    names = {t["name"] for t in mcp_server._handle({"method": "tools/list", "id": 1})["tools"]}
    assert "step_over" not in names

    monkeypatch.setenv("ACPDBG_CONTROL", "1")
    names = {t["name"] for t in mcp_server._handle({"method": "tools/list", "id": 2})["tools"]}
    assert {"step_over", "step_into", "step_out", "continue_execution",
            "set_breakpoint", "run_to_line"} <= names


def test_control_tool_blocked_when_disabled(bridge, monkeypatch):
    _bind(bridge, monkeypatch)
    monkeypatch.delenv("ACPDBG_CONTROL", raising=False)
    result = mcp_server._handle(
        {"method": "tools/call", "params": {"name": "step_over", "arguments": {}}, "id": 3}
    )
    assert result["isError"] is True
    assert "disabled" in result["content"][0]["text"]
    assert "thread step-over" not in bridge.calls


def test_control_tools_drive_execution_when_enabled(bridge, monkeypatch):
    _bind(bridge, monkeypatch)
    monkeypatch.setenv("ACPDBG_CONTROL", "1")

    def call(name, arguments=None):
        return mcp_server._handle(
            {"method": "tools/call", "params": {"name": name, "arguments": arguments or {}}, "id": 9}
        )

    # Stepping/continue go through the SB-API control path.
    assert call("step_over")["content"][0]["text"] == "CONTROL<step_over>"
    assert call("step_into")["content"][0]["text"] == "CONTROL<step_into>"
    assert call("step_out")["content"][0]["text"] == "CONTROL<step_out>"
    assert call("continue_execution")["content"][0]["text"] == "CONTROL<continue>"
    assert bridge.controls == ["step_over", "step_into", "step_out", "continue"]
    # Breakpoints are plain commands.
    call("set_breakpoint", {"function": "main"})
    assert "breakpoint set --name main" in bridge.calls
    call("run_to_line", {"file": "crash.c", "line": 15})
    assert 'breakpoint set --one-shot true --file "crash.c" --line 15' in bridge.calls
    assert bridge.controls[-1] == "continue"
