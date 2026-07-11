"""Drive a coding agent over ACP to debug a stopped program.

Given some prompt text and (optionally) a live debugger executor, it:

1. launches the configured coding agent as an ACP subprocess,
2. negotiates the protocol and advertises file-read (and optional write) access,
3. exposes the live debugger to the agent as an MCP tool server,
4. sends the prompt and streams the agent's reasoning back to the console.

The public entry point is the synchronous :func:`ask`, so it can be called
directly from an LLDB command handler.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from typing import Any, Callable, Optional

from acp import (
    PROTOCOL_VERSION,
    Client,
    RequestError,
    connect_to_agent,
    text_block,
)
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    ClientCapabilities,
    DeclineElicitationResponse,
    DeniedOutcome,
    EnvVariable,
    FileSystemCapabilities,
    Implementation,
    McpServerStdio,
    PermissionOption,
    ReadTextFileResponse,
    ResourceContentBlock,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    WriteTextFileResponse,
)

from . import __version__
from . import log
from .bridge import ENV_TOKEN, ENV_SOCKET, BridgeInfo, CommandBridge
from .config import Config, python_executable


class ConsolePrinter:
    """Renders streamed agent output to a terminal. Overridable for tests."""

    def __init__(self, stream=None) -> None:
        self._stream = stream or sys.stdout
        self._at_line_start = True
        # ANSI styling only where it will render. The lldb plugin always reads
        # us through a pipe, so it tells us via ACPDBG_COLOR whether the final
        # console is a real terminal (Xcode's console, for one, is not).
        color_env = os.environ.get("ACPDBG_COLOR")
        if color_env is not None:
            self._color = color_env.strip().lower() not in ("0", "false", "no", "off")
        else:
            self._color = bool(getattr(self._stream, "isatty", lambda: False)())

    def _styled(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self._color else text

    def _emit(self, text: str) -> None:
        if not text:
            return
        self._stream.write(text)
        self._stream.flush()
        self._at_line_start = text.endswith("\n")

    def _ensure_newline(self) -> None:
        if not self._at_line_start:
            self._emit("\n")

    def agent_text(self, text: str) -> None:
        self._emit(text)

    def agent_thought(self, text: str) -> None:
        self._ensure_newline()
        self._emit(self._styled(text, "2"))

    def tool_call(self, title: str, status: str) -> None:
        self._ensure_newline()
        self._emit(f"{self._styled(f'• {title}', '36')} ({status})\n")

    def note(self, text: str) -> None:
        self._ensure_newline()
        self._emit(text + "\n")

    def finish(self) -> None:
        self._ensure_newline()


def _content_text(content: Any) -> str:
    if isinstance(content, TextContentBlock):
        return content.text
    if isinstance(content, ResourceContentBlock):
        return content.uri or "<resource>"
    if isinstance(content, dict):
        return str(content.get("text", ""))
    return getattr(content, "text", "")


class DebuggerClient(Client):
    """The ACP client half: acpdbg acting as an editor/IDE for the agent."""

    def __init__(self, config: Config, printer: ConsolePrinter, cwd: str) -> None:
        self._config = config
        self._printer = printer
        self._cwd = cwd

    # --- streaming --------------------------------------------------------
    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if isinstance(update, AgentMessageChunk):
            self._log_first("message", "first agent message chunk")
            self._printer.agent_text(_content_text(update.content))
        elif isinstance(update, AgentThoughtChunk):
            self._log_first("thought", "first agent thought chunk")
            self._printer.agent_thought(_content_text(update.content))
        elif isinstance(update, ToolCallStart):
            log.debug("acp", f"tool call: {update.title or '?'} ({update.status or 'pending'})")
            self._printer.tool_call(update.title or "tool call", update.status or "pending")
        elif isinstance(update, ToolCallProgress):
            if update.status in ("completed", "failed"):
                log.debug("acp", f"tool call {update.status}: {update.tool_call_id or '?'}")
                self._printer.tool_call(update.tool_call_id or "tool call", update.status)
        else:
            log.debug("acp", f"session update: {type(update).__name__}")

    def _log_first(self, kind: str, message: str) -> None:
        seen = getattr(self, "_seen_kinds", set())
        if kind not in seen:
            seen.add(kind)
            self._seen_kinds = seen
            log.debug("acp", message)

    # --- permissions ------------------------------------------------------
    async def request_permission(
        self, session_id: str, tool_call: Any, options: list[PermissionOption], **kwargs: Any
    ):
        from acp.schema import RequestPermissionResponse

        if not options:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

        if self._config.permission_mode == "auto":
            option = _preferred_option(options)
            return RequestPermissionResponse(
                outcome=AllowedOutcome(option_id=option.option_id, outcome="selected")
            )

        # Interactive approval on the console.
        self._printer.note(f"🔐 Agent requests: {getattr(tool_call, 'title', None) or 'tool call'}")
        for idx, opt in enumerate(options, start=1):
            self._printer.note(f"   {idx}. {opt.name} ({opt.kind})")
        loop = asyncio.get_running_loop()
        while True:
            choice = (await loop.run_in_executor(None, lambda: input("   select> ").strip()))
            if choice.isdigit() and 1 <= int(choice) <= len(options):
                opt = options[int(choice) - 1]
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(option_id=opt.option_id, outcome="selected")
                )
            self._printer.note("   invalid selection")

    # --- filesystem -------------------------------------------------------
    async def read_text_file(
        self, session_id: str, path: str, line: int | None = None, limit: int | None = None, **kwargs: Any
    ) -> ReadTextFileResponse:
        abspath = path if os.path.isabs(path) else os.path.join(self._cwd, path)
        try:
            with open(abspath, "r", errors="replace") as handle:
                text = handle.read()
        except OSError as exc:
            raise RequestError.invalid_params({"path": path, "reason": str(exc)})
        if line is not None or limit is not None:
            text = _slice(text, line, limit)
        return ReadTextFileResponse(content=text)

    async def write_text_file(
        self, session_id: str, path: str, content: str, **kwargs: Any
    ) -> WriteTextFileResponse | None:
        if not self._config.allow_writes:
            raise RequestError.method_not_found("fs/write_text_file")
        abspath = path if os.path.isabs(path) else os.path.join(self._cwd, path)
        os.makedirs(os.path.dirname(abspath) or ".", exist_ok=True)
        with open(abspath, "w") as handle:
            handle.write(content)
        self._printer.note(f"✍️  agent wrote {abspath} ({len(content)} bytes)")
        return WriteTextFileResponse()

    # --- capabilities we don't offer -------------------------------------
    async def create_terminal(self, *args: Any, **kwargs: Any):
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, *args: Any, **kwargs: Any):
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(self, *args: Any, **kwargs: Any):
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(self, *args: Any, **kwargs: Any):
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, *args: Any, **kwargs: Any):
        raise RequestError.method_not_found("terminal/kill")

    async def create_elicitation(self, message: str, mode: Any, **kwargs: Any):
        return DeclineElicitationResponse(action="decline")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None:
        return None

    async def ext_method(self, method: str, params: dict, **kwargs: Any) -> dict:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict, **kwargs: Any) -> None:
        return None


def _preferred_option(options: list[PermissionOption]) -> PermissionOption:
    for option in options:
        if option.kind in ("allow_once", "allow_always"):
            return option
    return options[0]


def _slice(content: str, line: int | None, limit: int | None) -> str:
    lines = content.splitlines()
    start = max((line or 1) - 1, 0)
    end = start + limit if limit else len(lines)
    return "\n".join(lines[start:end])


def _mcp_command() -> tuple[str, list[str]]:
    """Prefer the installed `acpdbg-mcp` console script; else `python -m`."""
    import shutil

    script = shutil.which("acpdbg-mcp")
    if script:
        return script, []
    return python_executable(), ["-m", "acpdbg.mcp_server"]


def _mcp_server(config: Config, bridge: BridgeInfo) -> McpServerStdio:
    package_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    pythonpath = os.pathsep.join(
        p for p in [package_parent, os.environ.get("PYTHONPATH", "")] if p
    )
    env = {
        ENV_SOCKET: bridge.socket_path,
        ENV_TOKEN: bridge.token,
        "ACPDBG_UNSAFE": "1" if config.unsafe else "0",
        "ACPDBG_CONTROL": "1" if config.allow_control else "0",
        "ACPDBG_DEBUG": "1" if (config.debug or log.enabled()) else "0",
        "ACPDBG_LOG_FILE": log.log_path(),
        "PYTHONPATH": pythonpath,
    }
    command, args = _mcp_command()
    return McpServerStdio(
        name="acpdbg-debugger",
        command=command,
        args=args,
        env=[EnvVariable(name=k, value=v) for k, v in env.items()],
    )


async def _run_session(
    config: Config,
    prompt_text: str,
    *,
    bridge: Optional[BridgeInfo],
    printer: ConsolePrinter,
    cwd: str,
) -> None:
    argv = config.agent_argv()
    # In debug mode capture the agent's stderr into the log — agent CLIs report
    # auth and startup failures there, which would otherwise vanish.
    stderr_sink = None if config.agent_stderr else log.stderr_sink("session")
    if config.agent_stderr:
        stderr = None
    elif stderr_sink is not None:
        stderr = stderr_sink
    else:
        stderr = asyncio.subprocess.DEVNULL
    log.debug("session", f"spawning agent: {' '.join(argv)}")
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
    )
    log.debug("session", f"agent pid={proc.pid}")
    if proc.stdin is None or proc.stdout is None:
        raise RuntimeError("agent process did not expose stdio pipes")

    client = DebuggerClient(config, printer, cwd)
    conn = connect_to_agent(client, proc.stdin, proc.stdout)
    try:
        log.debug("acp", "initialize →")
        await _with_heartbeat(
            conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(
                    fs=FileSystemCapabilities(
                        read_text_file=True, write_text_file=config.allow_writes
                    ),
                    terminal=False,
                ),
                client_info=Implementation(name="acpdbg", title="acpdbg", version=__version__),
            ),
            printer,
            f"{config.agent} is initializing",
        )
        log.debug("acp", "initialize ← ok")
        mcp_servers = [_mcp_server(config, bridge)] if bridge is not None else []
        log.debug("acp", f"session/new → (mcp servers: {len(mcp_servers)})")
        # Some agents (Copilot CLI, for one) take minutes here; without feedback
        # this silence is indistinguishable from a hang.
        session = await _with_heartbeat(
            conn.new_session(cwd=cwd, mcp_servers=mcp_servers),
            printer,
            f"{config.agent} is starting the session",
        )
        log.debug("acp", f"session/new ← id={session.session_id}")

        log.debug("acp", f"session/prompt → ({len(prompt_text)} chars, timeout={config.prompt_timeout:g}s)")
        request = conn.prompt(session_id=session.session_id, prompt=[text_block(prompt_text)])
        timeout = config.prompt_timeout
        if timeout and timeout > 0:
            try:
                await asyncio.wait_for(request, timeout=timeout)
                log.debug("acp", "session/prompt ← turn complete")
            except asyncio.TimeoutError:
                log.debug("acp", f"session/prompt TIMED OUT after {timeout:.0f}s")
                printer.note(f"(agent timed out after {timeout:.0f}s)")
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(conn.cancel(session_id=session.session_id), timeout=2)
        else:
            await request
            log.debug("acp", "session/prompt ← turn complete")
    finally:
        printer.finish()
        await _shutdown(proc, conn)
        log.debug("session", f"agent shut down (returncode={proc.returncode})")
        if stderr_sink is not None:
            with contextlib.suppress(OSError):
                stderr_sink.close()


async def _with_heartbeat(awaitable, printer: ConsolePrinter, label: str, interval: float = 20.0):
    """Await ``awaitable``, printing a progress note every ``interval`` seconds."""
    task = asyncio.ensure_future(awaitable)
    waited = 0.0
    while True:
        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=interval)
        except asyncio.TimeoutError:
            waited += interval
            printer.note(f"({label}… {waited:.0f}s)")
            log.debug("acp", f"still waiting: {label} ({waited:.0f}s)")


async def _shutdown(proc, conn) -> None:
    with contextlib.suppress(Exception):
        await asyncio.wait_for(conn.close(), timeout=2)
    if proc.stdin is not None:
        with contextlib.suppress(Exception):
            proc.stdin.close()
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)


def run_prompt(
    config: Config,
    prompt_text: str,
    *,
    bridge_info: Optional[BridgeInfo] = None,
    cwd: Optional[str] = None,
    printer: Optional[ConsolePrinter] = None,
) -> None:
    """Run one ACP turn against ``config``'s agent, streaming the reply.

    ``bridge_info`` points at an already-running debugger bridge (created by the
    lldb plugin, possibly in a different process); when given, the agent gets the
    live ``debugger_command`` / ``get_backtrace`` / ``get_locals`` tools.
    """
    cwd = cwd or os.getcwd()
    printer = printer or ConsolePrinter()
    asyncio.run(
        _run_session(config, prompt_text, bridge=bridge_info, printer=printer, cwd=cwd)
    )


def ask(
    config: Config,
    prompt_text: str,
    *,
    executor: Optional[Callable[[str], str]] = None,
    cwd: Optional[str] = None,
    printer: Optional[ConsolePrinter] = None,
) -> None:
    """Same-process convenience: start a debugger bridge from ``executor`` (if
    given and MCP is enabled), run one ACP turn, then tear the bridge down.
    """
    bridge_server: Optional[CommandBridge] = None
    bridge_info: Optional[BridgeInfo] = None
    if config.use_mcp and executor is not None:
        bridge_server = CommandBridge(executor)
        bridge_info = bridge_server.start()

    try:
        run_prompt(config, prompt_text, bridge_info=bridge_info, cwd=cwd, printer=printer)
    finally:
        if bridge_server is not None:
            bridge_server.stop()
