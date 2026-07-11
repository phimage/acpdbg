"""In-process command bridge that exposes the live debugger to the MCP server.

The problem: a real ACP agent (Gemini, Claude Code) reaches tools by spawning an
MCP server as its *own* subprocess. That subprocess cannot touch the
``lldb.SBDebugger`` object, which lives inside the debugger's Python process.

The bridge solves this with a tiny local socket. Inside the debugger process we
run :class:`CommandBridge`, which listens on a private unix socket and runs
debugger commands via an injected ``executor`` callable. The MCP server (see
``mcp_server.py``) connects to that socket and forwards tool calls. A random
per-session token guards the socket against other local processes.

The bridge is intentionally free of any LLDB import so it can be unit tested
with a fake executor.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import tempfile
import threading
from dataclasses import dataclass
from typing import Callable

from . import log

# A callable that runs one debugger command and returns its textual output.
Executor = Callable[[str], str]

ENV_SOCKET = "ACPDBG_BRIDGE_SOCKET"
ENV_TOKEN = "ACPDBG_BRIDGE_TOKEN"


@dataclass
class BridgeInfo:
    socket_path: str
    token: str

    def env(self) -> dict[str, str]:
        return {ENV_SOCKET: self.socket_path, ENV_TOKEN: self.token}

    @classmethod
    def from_env(cls) -> "BridgeInfo | None":
        socket_path = os.environ.get(ENV_SOCKET)
        token = os.environ.get(ENV_TOKEN)
        if socket_path and token:
            return cls(socket_path=socket_path, token=token)
        return None

    def to_file(self, path: str) -> None:
        """Write connection info to a private file for external MCP clients."""
        path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as handle:
            json.dump({"socket": self.socket_path, "token": self.token}, handle)
        os.chmod(path, 0o600)


class CommandBridge:
    """Serves debugger commands over a local unix socket, in a background thread.

    ``executor`` runs a command string (LLDB command interpreter). ``control``,
    if given, handles execution-control actions (step/continue) via the SB API —
    those don't work reliably through the command interpreter in this context.
    """

    def __init__(self, executor: Executor, control: Executor | None = None) -> None:
        self._executor = executor
        self._control = control
        self._token = secrets.token_hex(16)
        self._dir = tempfile.mkdtemp(prefix="acpdbg-")
        self._socket_path = os.path.join(self._dir, "bridge.sock")
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()  # serialize debugger access
        self._stop = threading.Event()

    @property
    def info(self) -> BridgeInfo:
        return BridgeInfo(socket_path=self._socket_path, token=self._token)

    def start(self) -> BridgeInfo:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self._socket_path)
        os.chmod(self._socket_path, 0o600)
        server.listen(8)
        server.settimeout(0.5)
        self._server = server
        self._thread = threading.Thread(target=self._serve, name="acpdbg-bridge", daemon=True)
        self._thread.start()
        return self.info

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            buffer = b""
            conn.settimeout(1.0)
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if line.strip():
                        response = self._dispatch(line)
                        try:
                            conn.sendall(json.dumps(response).encode() + b"\n")
                        except OSError:
                            return

    def _dispatch(self, line: bytes) -> dict:
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            return {"ok": False, "error": f"invalid request: {exc}"}

        if request.get("token") != self._token:
            return {"ok": False, "error": "unauthorized"}

        command = str(request.get("command", "")).strip()
        if not command:
            return {"ok": False, "error": "empty command"}

        op = request.get("op", "command")
        if op == "control":
            if self._control is None:
                return {"ok": False, "error": "control not available"}
            handler = self._control
        else:
            handler = self._executor

        log.debug("bridge", f"{op}: {command!r}")
        try:
            with self._lock:
                output = handler(command)
        except Exception as exc:  # pragma: no cover - handler is backend-specific
            log.debug("bridge", f"{op} failed: {type(exc).__name__}: {exc}")
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        log.debug("bridge", f"{op} ok ({len(output or '')} chars)")
        return {"ok": True, "output": output}

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            try:
                self._server.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            os.unlink(self._socket_path)
        except OSError:
            pass
        try:
            os.rmdir(self._dir)
        except OSError:
            pass


def call_bridge(
    socket_path: str, token: str, command: str, op: str = "command", timeout: float = 120.0
) -> str:
    """Client helper used by the MCP server to run one command via the bridge."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        sock.sendall(json.dumps({"token": token, "op": op, "command": command}).encode() + b"\n")
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
    line = buffer.split(b"\n", 1)[0]
    response = json.loads(line)
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "bridge error"))
    return response.get("output", "")
