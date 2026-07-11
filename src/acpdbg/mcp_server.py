"""A tiny stdio MCP server exposing the live debugger as agent tools.

A real ACP agent connects to this process (declared via ``mcpServers`` when the
session is created) and gains three tools that reach into the stopped program
through the :mod:`acpdbg.bridge` socket:

- ``debugger_command`` — run one LLDB command and return its output
- ``get_backtrace``    — shorthand for ``bt``
- ``get_locals``       — shorthand for ``frame variable``

This is a hand-rolled, dependency-free implementation of just enough of the
Model Context Protocol (JSON-RPC 2.0 over newline-delimited stdio) to be driven
by Gemini CLI, Claude Code, and other MCP clients.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from . import log
from .bridge import ENV_SOCKET, ENV_TOKEN, call_bridge
from .safety import command_is_safe

SERVER_NAME = "acpdbg-debugger"
SERVER_VERSION = "0.1.0"
DEFAULT_MCP_PROTOCOL = "2025-06-18"

_TOOLS = [
    {
        "name": "debugger_command",
        "description": (
            "Run a single LLDB command against the live stopped process and "
            "return its output. Examples: 'bt', 'frame variable', 'p argc', "
            "'x/16xb ptr', 'image lookup -a 0x...'. Read-only inspection is "
            "allowed; commands that resume or mutate the process are rejected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The LLDB command to run, with any arguments.",
                }
            },
            "required": ["command"],
        },
    },
    {
        "name": "get_backtrace",
        "description": "Return the backtrace of the stopped thread (LLDB 'bt').",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_locals",
        "description": "Return local variables of the selected frame (LLDB 'frame variable').",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

# Execution-control tools. Only offered when ACPDBG_CONTROL is enabled, because
# they resume or mutate the process — letting the agent drive the debugger like a
# human (step, continue, breakpoints) rather than only observe.
_CONTROL_TOOLS = [
    {
        "name": "step_over",
        "description": "Execute the current source line and stop on the next one, "
        "without descending into calls (LLDB 'thread step-over' / 'next').",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "step_into",
        "description": "Step to the next line, descending into any function call "
        "(LLDB 'thread step-in' / 'step').",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "step_out",
        "description": "Run until the current function returns to its caller "
        "(LLDB 'thread step-out' / 'finish').",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "continue_execution",
        "description": "Resume the program until it next stops (breakpoint, signal, "
        "or exit) (LLDB 'continue').",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_breakpoint",
        "description": "Set a breakpoint by function name, or by file and line.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "function": {"type": "string", "description": "Function name to break on."},
                "file": {"type": "string", "description": "Source file name."},
                "line": {"type": "integer", "description": "1-based line number (with 'file')."},
            },
        },
    },
    {
        "name": "run_to_line",
        "description": "Set a one-shot breakpoint at file:line and continue to it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file": {"type": "string", "description": "Source file name."},
                "line": {"type": "integer", "description": "1-based line number."},
            },
            "required": ["file", "line"],
        },
    },
]


def _control_enabled() -> bool:
    return os.environ.get("ACPDBG_CONTROL", "").lower() in ("1", "true", "yes", "on")


def _bridge_conn() -> tuple[str | None, str | None]:
    """Locate the debugger bridge: from env vars, or a shared bridge file.

    The env vars are set when the in-session agent spawns us. External MCP
    clients (Claude Code, Copilot, …) instead point ACPDBG_BRIDGE_FILE at the
    file written by `acpdbg serve`, so their config stays stable across sessions.
    """
    socket_path = os.environ.get(ENV_SOCKET)
    token = os.environ.get(ENV_TOKEN)
    if socket_path and token:
        return socket_path, token

    path = os.environ.get("ACPDBG_BRIDGE_FILE")
    if path:
        try:
            with open(os.path.expanduser(path)) as handle:
                data = json.load(handle)
            return data.get("socket"), data.get("token")
        except (OSError, json.JSONDecodeError):
            return None, None
    return None, None


def _run_command(command: str) -> tuple[str, bool]:
    """Return (text, is_error) for a debugger command via the bridge."""
    socket_path, token = _bridge_conn()
    if not socket_path or not token:
        return ("acpdbg debugger bridge is not available (no live session).", True)

    unsafe = os.environ.get("ACPDBG_UNSAFE", "").lower() in ("1", "true", "yes", "on")
    if not command_is_safe(command, unsafe=unsafe):
        return (
            f"Command '{command}' was blocked by acpdbg's safety filter "
            f"(it resumes or mutates the process). Try a read-only command.",
            True,
        )
    try:
        return (call_bridge(socket_path, token, command), False)
    except Exception as exc:
        return (f"debugger error: {exc}", True)


def _bridge(command: str, op: str = "command") -> tuple[str, bool]:
    """Send a command (or control action) to the bridge, bypassing the filter.

    Only reached from the explicit, opt-in control tools.
    """
    socket_path, token = _bridge_conn()
    if not socket_path or not token:
        return ("acpdbg debugger bridge is not available (no live session).", True)
    try:
        output = call_bridge(socket_path, token, command, op=op)
    except Exception as exc:
        return (f"debugger error: {exc}", True)
    return (output or "(no output)", False)


def _handle_control(name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
    # Step/continue go through the SB-API "control" path; breakpoints are plain
    # commands.
    if name == "step_over":
        return _bridge("step_over", op="control")
    if name == "step_into":
        return _bridge("step_into", op="control")
    if name == "step_out":
        return _bridge("step_out", op="control")
    if name == "continue_execution":
        return _bridge("continue", op="control")
    if name == "set_breakpoint":
        function = arguments.get("function")
        file = arguments.get("file")
        line = arguments.get("line")
        if function:
            return _bridge(f"breakpoint set --name {function}")
        if file and line is not None:
            return _bridge(f'breakpoint set --file "{file}" --line {int(line)}')
        return ("set_breakpoint needs 'function', or 'file' and 'line'.", True)
    if name == "run_to_line":
        file = arguments.get("file")
        line = arguments.get("line")
        if not file or line is None:
            return ("run_to_line needs 'file' and 'line'.", True)
        setup, is_error = _bridge(
            f'breakpoint set --one-shot true --file "{file}" --line {int(line)}'
        )
        if is_error:
            return (setup, True)
        cont, is_error = _bridge("continue", op="control")
        return (f"{setup}\n{cont}", is_error)
    return (f"unknown control tool: {name}", True)


def _handle_tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    log.debug("mcp", f"tools/call {name} {arguments!r}")
    if name == "debugger_command":
        command = str(arguments.get("command", "")).strip()
        if not command:
            text, is_error = "missing 'command' argument", True
        else:
            text, is_error = _run_command(command)
    elif name == "get_backtrace":
        text, is_error = _run_command("bt")
    elif name == "get_locals":
        text, is_error = _run_command("frame variable")
    elif name in _CONTROL_NAMES:
        if not _control_enabled():
            text, is_error = (
                "Execution control is disabled. Enable it with `acpdbg config "
                "control on` (or --control) to step/continue/set breakpoints.",
                True,
            )
        else:
            text, is_error = _handle_control(name, arguments)
    else:
        return {"content": [{"type": "text", "text": f"unknown tool: {name}"}], "isError": True}

    log.debug(
        "mcp",
        f"tools/call {name} → {'error' if is_error else 'ok'} ({len(text or '')} chars)",
    )
    return {"content": [{"type": "text", "text": text or "(no output)"}], "isError": is_error}


_CONTROL_NAMES = {tool["name"] for tool in _CONTROL_TOOLS}


def _handle(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    params = request.get("params") or {}

    if method == "initialize":
        protocol = params.get("protocolVersion") or DEFAULT_MCP_PROTOCOL
        return {
            "protocolVersion": protocol,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "tools/list":
        tools = list(_TOOLS)
        if _control_enabled():
            tools += _CONTROL_TOOLS
        return {"tools": tools}
    if method == "tools/call":
        return _handle_tool_call(params.get("name", ""), params.get("arguments") or {})
    if method == "ping":
        return {}
    raise _MethodNotFound(method)


class _MethodNotFound(Exception):
    def __init__(self, method: Any) -> None:
        self.method = method


def main() -> int:
    stdin = sys.stdin
    stdout = sys.stdout
    log.debug("mcp", f"MCP tool server started (control={'on' if _control_enabled() else 'off'})")
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue

        req_id = request.get("id")
        is_notification = req_id is None and "id" not in request

        try:
            result = _handle(request)
        except _MethodNotFound as exc:
            if not is_notification:
                _write(stdout, {"jsonrpc": "2.0", "id": req_id,
                                "error": {"code": -32601, "message": f"method not found: {exc.method}"}})
            continue
        except Exception as exc:  # pragma: no cover - defensive
            if not is_notification:
                _write(stdout, {"jsonrpc": "2.0", "id": req_id,
                                "error": {"code": -32603, "message": str(exc)}})
            continue

        # Notifications (no id) never get a response.
        if is_notification:
            continue
        _write(stdout, {"jsonrpc": "2.0", "id": req_id, "result": result})
    return 0


def _write(stream, message: dict[str, Any]) -> None:
    stream.write(json.dumps(message) + "\n")
    stream.flush()


if __name__ == "__main__":
    raise SystemExit(main())
