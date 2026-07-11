"""Shared debug log for every acpdbg process.

acpdbg spans several processes — the lldb plugin (embedded Python), the
`acpdbg-session` helper, the MCP tool server, and the agent itself — and GUI
frontends like Xcode show very little of what happens between them. When debug
mode is on, they all append timestamped lines to one log file so a stall can be
located from a terminal (`tail -f`) or with the `acpdbg log` lldb command.

Enable with `acpdbg config debug on` (or ACPDBG_DEBUG=1). The switch travels to
child processes through the environment. Stdlib-only: this module is imported
inside the debugger's embedded Python (3.9 on Xcode's lldb).
"""

from __future__ import annotations

import datetime
import os
import threading
import traceback

_LOCK = threading.Lock()


def log_path() -> str:
    """Where the log lives; overridable for tests via ACPDBG_LOG_FILE."""
    return os.environ.get("ACPDBG_LOG_FILE") or os.path.expanduser(
        "~/.acpdbg/acpdbg.log"
    )


def enabled() -> bool:
    return os.environ.get("ACPDBG_DEBUG", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def set_enabled(on: bool) -> None:
    """Flip debug mode for this process and any child it spawns."""
    os.environ["ACPDBG_DEBUG"] = "1" if on else "0"


def debug(component: str, message: str) -> None:
    """Append one log line, silently doing nothing when debug mode is off."""
    if not enabled():
        return
    now = datetime.datetime.now()
    stamp = now.strftime("%H:%M:%S") + f".{now.microsecond // 1000:03d}"
    line = f"{stamp} [{os.getpid():>6} {component}] {message.rstrip()}\n"
    try:
        with _LOCK:
            path = log_path()
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8", errors="replace") as handle:
                handle.write(line)
    except OSError:
        pass  # logging must never break the debugger


def exception(component: str, message: str) -> None:
    """Log ``message`` plus the current exception's traceback."""
    debug(component, f"{message}\n{traceback.format_exc().rstrip()}")


def stderr_sink(component: str):
    """A writable fd capturing a subprocess's stderr into the log, or None.

    Used for the agent process in debug mode: agent CLIs report auth and
    startup problems on stderr, which is otherwise discarded.
    """
    if not enabled():
        return None
    try:
        path = log_path()
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        handle = open(path, "a", encoding="utf-8", errors="replace")
        debug(component, "capturing the agent's stderr into this log (untagged lines)")
        return handle
    except OSError:
        return None


def tail(count: int = 40) -> str:
    """The last ``count`` lines of the log (for the `acpdbg log` command)."""
    try:
        with open(log_path(), "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    except OSError:
        return ""
    return "".join(lines[-count:])


def clear() -> None:
    try:
        os.unlink(log_path())
    except OSError:
        pass
