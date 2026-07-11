"""Configuration for acpdbg: which agent to talk to and how much it may do.

Everything is overridable from the environment so it works both from the
``acpdbg`` CLI and from inside an interactive ``lldb`` session (where you can
also use the ``acpdbg-config`` command).
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from dataclasses import dataclass

# Friendly agent aliases -> how to launch them as an ACP agent over stdio.
# The value is (executable, default_args). The executable is resolved on PATH.
_AGENT_ALIASES: dict[str, tuple[str, list[str]]] = {
    # GitHub Copilot CLI has a built-in ACP server mode.
    "copilot": ("copilot", ["--acp", "--disable-builtin-mcps"]),
    # Google's Gemini CLI speaks ACP behind an experimental flag.
    "gemini": ("gemini", ["--experimental-acp"]),
    # Zed's Claude Code ACP adapter (`npm i -g @zed-industries/claude-code-acp`).
    "claude-code": ("claude-code-acp", []),
    "claude": ("claude-code-acp", []),
}


def _truthy(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


class ConfigError(Exception):
    """Raised when the configured agent cannot be launched."""


# --- plugin ↔ helper wire protocol for persistent sessions ------------------
# These live here (not in session.py) because the lldb plugin — running in the
# debugger's possibly-ancient embedded Python — may import only stdlib-level
# acpdbg modules, and session.py pulls in the ACP SDK.
#
# The plugin spawns `acpdbg-session` with SESSION_PERSISTENT_ENV=1 and writes
# one command per line to its stdin:
#     PROMPT <path>   run one turn with the prompt text read from <path>
#     RESET           start a fresh conversation on the same agent process
# stdin EOF ends the helper (so it dies with the debugger). The helper streams
# agent output on stdout and marks each command's completion with a line
# starting with SESSION_TURN_END followed by a status word: "ready" (once,
# after startup), then "ok" or "error". The \x1e record separator makes a
# collision with genuine agent output effectively impossible.
SESSION_PERSISTENT_ENV = "ACPDBG_PERSISTENT"
SESSION_TURN_END = "\x1e##acpdbg-turn-end##"


@dataclass
class Config:
    # Agent alias ("mock", "gemini", "claude-code") or a raw command line.
    agent: str = "mock"
    # "auto" approves tool-use permission requests; "prompt" asks on the console.
    permission_mode: str = "auto"
    # Allow the agent to write files (e.g. apply a fix). Off by default.
    allow_writes: bool = False
    # Expose the live debugger to the agent via the MCP tool bridge.
    use_mcp: bool = True
    # Skip the debugger-command safety filter in the MCP bridge. Off by default.
    unsafe: bool = False
    # Let the agent drive execution (step/continue/breakpoints), like a human
    # debugger. Off by default: in crash triage you rarely want to resume.
    allow_control: bool = False
    # Automatically expose stopped sessions to external MCP clients (see
    # `acpdbg serve`). Implies control unless ACPDBG_CONTROL says otherwise.
    autoserve: bool = False
    # Keep one agent conversation alive across asks (the agent remembers earlier
    # answers and startup cost is paid once). Off = a fresh one-shot per ask.
    session: bool = True
    # Forward the agent subprocess's stderr (useful when debugging the agent).
    agent_stderr: bool = False
    # Seconds to wait for the agent to finish a turn (0 disables the timeout).
    prompt_timeout: float = 300.0
    # Debug mode: every acpdbg process logs to ~/.acpdbg/acpdbg.log (see log.py).
    debug: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        # Autoserve defaults control to on (a read-only external bridge is rarely
        # what you want); an explicit ACPDBG_CONTROL still wins either way.
        autoserve = _truthy(os.environ.get("ACPDBG_AUTOSERVE"), False)
        return cls(
            agent=os.environ.get("ACPDBG_AGENT", "mock"),
            permission_mode=os.environ.get("ACPDBG_PERMISSION", "auto"),
            allow_writes=_truthy(os.environ.get("ACPDBG_ALLOW_WRITES"), False),
            use_mcp=_truthy(os.environ.get("ACPDBG_MCP"), True),
            unsafe=_truthy(os.environ.get("ACPDBG_UNSAFE"), False),
            allow_control=_truthy(os.environ.get("ACPDBG_CONTROL"), autoserve),
            autoserve=autoserve,
            session=_truthy(os.environ.get("ACPDBG_SESSION"), True),
            agent_stderr=_truthy(os.environ.get("ACPDBG_AGENT_STDERR"), False),
            prompt_timeout=float(os.environ.get("ACPDBG_TIMEOUT", "300") or 300),
            debug=_truthy(os.environ.get("ACPDBG_DEBUG"), False),
        )

    def agent_argv(self) -> list[str]:
        """Resolve ``self.agent`` to a runnable argv, or raise ConfigError."""
        name = self.agent.strip()

        # The bundled mock agent: prefer the installed console script (works even
        # when running inside an embedded interpreter where sys.executable is not
        # a Python), and fall back to `python -m acpdbg.mock_agent`.
        if name == "mock":
            script = shutil.which("acpdbg-mock-agent")
            if script:
                return [script]
            return [python_executable(), "-m", "acpdbg.mock_agent"]

        alias = _AGENT_ALIASES.get(name)
        if alias is not None:
            executable, args = alias
            resolved = _resolve_executable(executable)
            if resolved is None:
                raise ConfigError(
                    f"Agent '{self.agent}' needs the '{executable}' command, which was "
                    f"not found on PATH.\n{_install_hint(self.agent)}"
                )
            return [resolved, *args]

        # Treat the value as a raw command line, e.g. "my-agent --acp".
        parts = shlex.split(self.agent)
        if not parts:
            raise ConfigError("No agent configured (ACPDBG_AGENT is empty).")
        resolved = _resolve_executable(parts[0])
        if resolved is None:
            raise ConfigError(f"Agent command '{parts[0]}' was not found on PATH.")
        return [resolved, *parts[1:]]

    def describe_agent(self) -> str:
        try:
            return " ".join(self.agent_argv())
        except ConfigError as exc:
            return f"{self.agent} (unavailable: {exc})"

    def to_env(self) -> dict[str, str]:
        """Serialize the config so a helper subprocess can reconstruct it."""
        return {
            "ACPDBG_AGENT": self.agent,
            "ACPDBG_PERMISSION": self.permission_mode,
            "ACPDBG_ALLOW_WRITES": "1" if self.allow_writes else "0",
            "ACPDBG_MCP": "1" if self.use_mcp else "0",
            "ACPDBG_UNSAFE": "1" if self.unsafe else "0",
            "ACPDBG_CONTROL": "1" if self.allow_control else "0",
            "ACPDBG_AUTOSERVE": "1" if self.autoserve else "0",
            "ACPDBG_SESSION": "1" if self.session else "0",
            "ACPDBG_AGENT_STDERR": "1" if self.agent_stderr else "0",
            "ACPDBG_TIMEOUT": str(self.prompt_timeout),
            "ACPDBG_DEBUG": "1" if self.debug else "0",
        }


def resolve_session_command() -> list[str]:
    """Return the argv for the out-of-process ACP helper (`acpdbg-session`).

    The debugger's embedded Python may be too old for the ACP SDK (Xcode's lldb
    ships Python 3.9, for example), so the agent work runs in a separate helper
    process on a modern Python. The path to that helper is normally baked into
    ``~/.lldbinit`` by ``acpdbg --install-lldbinit`` via ``ACPDBG_SESSION_CMD``.
    """
    explicit = os.environ.get("ACPDBG_SESSION_CMD")
    if explicit:
        return shlex.split(explicit)
    found = shutil.which("acpdbg-session")
    if found:
        return [found]
    if sys.version_info >= (3, 10):
        return [python_executable(), "-m", "acpdbg.session"]
    raise ConfigError(
        "acpdbg needs a Python 3.10+ helper to talk to the agent, but this "
        f"debugger runs Python {sys.version_info[0]}.{sys.version_info[1]} and no "
        "helper was found. Run `acpdbg --install-lldbinit` from your acpdbg "
        "installation (it bakes the helper path into ~/.lldbinit), or set "
        "ACPDBG_SESSION_CMD to the path of the `acpdbg-session` command."
    )


def python_executable() -> str:
    """A real Python interpreter to launch helper processes with.

    Inside an embedded interpreter (e.g. lldb) ``sys.executable`` may not point
    to a Python binary, so fall back to one on PATH.
    """
    exe = sys.executable or ""
    if "python" in os.path.basename(exe).lower():
        return exe
    return shutil.which("python3") or shutil.which("python") or exe


def _resolve_executable(name: str) -> str | None:
    if name == sys.executable or os.path.isabs(name):
        return name if os.path.exists(name) else shutil.which(name)
    return shutil.which(name)


def _install_hint(alias: str) -> str:
    hints = {
        "copilot": "Install GitHub Copilot CLI: npm install -g @github/copilot",
        "gemini": "Install the Gemini CLI: https://github.com/google-gemini/gemini-cli",
        "claude-code": "Install the adapter: npm install -g @zed-industries/claude-code-acp",
        "claude": "Install the adapter: npm install -g @zed-industries/claude-code-acp",
    }
    return hints.get(alias, "")
