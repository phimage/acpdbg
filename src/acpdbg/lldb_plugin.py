"""LLDB plugin: `ask` / `why` / `acpdbg` commands backed by a coding agent.

Load it inside an LLDB session:

    (lldb) command script import acpdbg.lldb_plugin

Then, once your program has stopped:

    (lldb) ask why did this crash?
    (lldb) acpdbg config agent gemini
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time as _time

import lldb

from . import __version__
from . import log as _log
from .bridge import CommandBridge
from .config import Config, ConfigError, resolve_session_command
from .lldb_inspector import LLDBInspector
from .prompts import build_initial_prompt

# This module runs inside the debugger's embedded Python, which may be older than
# the ACP SDK supports (Xcode's lldb ships Python 3.9). It therefore imports only
# the standard library plus acpdbg's own stdlib-level modules, and runs the agent
# in a separate `acpdbg-session` helper process on a modern Python.

# One config per debugger session, seeded from the environment and mutable via
# the `acpdbg config` command.
_CONFIG = Config.from_env()

# Guards against re-entrancy: when the agent uses execution-control tools, the
# process may stop again and re-trigger the CLI stop-hook. This flag makes those
# nested invocations no-ops so we never start a session inside a session.
_IN_SESSION = False

_HELP = """\
acpdbg — debug with your coding agent over ACP

  ask <question>            investigate the stopped program and answer
  why                       shorthand for `ask why did this stop?`
  acpdbg <question>         same as `ask`
  acpdbg config             show current configuration
  acpdbg config <k> <v>     set a config value
  acpdbg serve              expose this session to an external MCP client
  acpdbg serve stop         stop exposing it
  acpdbg log [n]            show the last n lines of the debug log (default 40)
  acpdbg log clear          delete the debug log
  acpdbg last               re-print the previous session's full output

Installed agents also get their own command (one-off, does not change the
configured agent):

  copilot <question>        ask GitHub Copilot CLI
  claude <question>         ask Claude Code (via the ACP adapter)
  gemini <question>         ask Gemini CLI

config keys: agent, permission (auto|prompt), mcp (on|off),
             control (on|off), unsafe (on|off), writes (on|off),
             autoserve (on|off), timeout <seconds>, debug (on|off)

`debug on` makes every acpdbg process log to ~/.acpdbg/acpdbg.log — follow it
from a terminal with `tail -f` to see exactly where a session stalls.

`control on` lets the agent drive execution — step, continue, set breakpoints —
to debug interactively like a human.

`autoserve on` runs `acpdbg serve` automatically whenever the program stops on
a crash or breakpoint, and turns control on with it.
"""


def _out(debugger, text: str) -> None:
    """Write text to the debugger's output stream (terminal lldb).

    Only works where that stream is actually rendered — i.e. terminal lldb.
    Xcode talks to lldb through lldb-rpc-server, so both this stream and the
    embedded Python's ``sys.stdout`` end up on file descriptors its console
    never reads; the only text Xcode displays for a command is the
    ``SBCommandReturnObject``, flushed once when the command returns. Callers
    with a ``result`` in hand must buffer for it when the console is not a tty
    (see ``_debug``). The SBFile API comes first because recent lldb's
    ``GetOutputFileHandle()`` returns a read-mode wrapper.
    """
    try:
        sbfile = debugger.GetOutputFile()
        if sbfile and sbfile.IsValid():
            sbfile.Write(text.encode("utf-8", "replace"))
            sbfile.Flush()
            return
    except Exception:
        pass
    try:
        handle = debugger.GetOutputFileHandle()
        if handle and handle.writable():
            handle.write(text)
            handle.flush()
            return
    except Exception:
        pass
    print(text, end="", flush=True)


def _console_isatty(debugger) -> bool:
    """Whether the debugger's console renders like a terminal (ANSI styling)."""
    try:
        # The handle may be read-mode, but isatty() still reflects the real fd.
        handle = debugger.GetOutputFileHandle()
        if handle is not None:
            return handle.isatty()
    except Exception:
        pass
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def __lldb_init_module(debugger: lldb.SBDebugger, internal_dict: dict) -> None:
    # Already loaded in this debugger (e.g. via ~/.lldbinit *and* the CLI's
    # explicit import): re-adding would print "command already exists" errors.
    try:
        if debugger.GetCommandInterpreter().UserCommandExists("acpdbg"):
            return
    except Exception:
        pass
    debugger.HandleCommand("command script add -f acpdbg.lldb_plugin._cmd_ask ask")
    debugger.HandleCommand("command script add -f acpdbg.lldb_plugin._cmd_why why")
    debugger.HandleCommand("command script add -f acpdbg.lldb_plugin._cmd_acpdbg acpdbg")
    # Used by the `acpdbg` CLI as a stop-hook one-liner; reads the question from
    # ACPDBG_QUESTION so no fragile nested quoting is needed on the command line.
    debugger.HandleCommand("command script add -f acpdbg.lldb_plugin._cmd_auto acpdbg-auto")
    # Used by the autoserve stop-hook (see `acpdbg config autoserve on`).
    debugger.HandleCommand("command script add -f acpdbg.lldb_plugin._cmd_autoserve acpdbg-autoserve")
    if _CONFIG.autoserve:
        # Baked into ~/.lldbinit (or set in the environment): arm the hook now.
        # With no target yet it lands on the dummy target, which lldb copies to
        # every target created afterwards.
        _ensure_autoserve_hook(debugger)
    agent_commands = _register_agent_commands(debugger)
    commands = ", ".join(["ask", "why", "acpdbg", *agent_commands])
    py = f"{sys.version_info[0]}.{sys.version_info[1]}"
    _out(
        debugger,
        f"acpdbg {__version__} loaded — commands: {commands}  "
        f"(agent: {_CONFIG.agent}{'; autoserve on' if _CONFIG.autoserve else ''}; debugger Python {py}).\n"
        "  Stop your program (crash or breakpoint), then: ask why did this stop?\n",
    )


# One lldb command per agent alias, registered only when the agent's executable
# is actually installed: `copilot <question>`, `claude <question>`, …  Each is a
# one-off — it asks that agent without changing the configured default.
_AGENT_COMMANDS = {
    "copilot": "_cmd_agent_copilot",
    "claude": "_cmd_agent_claude",
    "gemini": "_cmd_agent_gemini",
}


def _register_agent_commands(debugger) -> list:
    interpreter = debugger.GetCommandInterpreter()
    registered = []
    for alias, function in _AGENT_COMMANDS.items():
        try:
            Config(agent=alias).agent_argv()  # raises if not installed
        except ConfigError:
            continue
        try:
            if interpreter.UserCommandExists(alias):
                continue
        except Exception:
            pass
        debugger.HandleCommand(
            f"command script add -f acpdbg.lldb_plugin.{function} {alias}"
        )
        registered.append(alias)
    return registered


def _cmd_agent_copilot(debugger, command, result, internal_dict) -> None:
    _debug_with_agent(debugger, "copilot", command, result)


def _cmd_agent_claude(debugger, command, result, internal_dict) -> None:
    _debug_with_agent(debugger, "claude", command, result)


def _cmd_agent_gemini(debugger, command, result, internal_dict) -> None:
    _debug_with_agent(debugger, "gemini", command, result)


def _debug_with_agent(debugger, agent: str, command, result) -> None:
    question = (command or "").strip() or "Why did this program stop, and how do I fix it?"
    previous = _CONFIG.agent
    _CONFIG.agent = agent
    try:
        _debug(debugger, question, result)
    finally:
        _CONFIG.agent = previous


def _cmd_ask(debugger, command, result, internal_dict) -> None:
    _debug(debugger, command, result)


def _cmd_why(debugger, command, result, internal_dict) -> None:
    _debug(debugger, command or "Why did this program stop, and how do I fix it?", result)


def _cmd_auto(debugger, command, result, internal_dict) -> None:
    # Fired from the CLI's stop-hook when the program stops. Only act on genuine
    # faults, not the benign launch stops the hook also sees, and never while a
    # session is already running (the agent may have resumed execution itself).
    if _IN_SESSION:
        return
    if not LLDBInspector(debugger).is_reportable_stop():
        return
    question = os.environ.get("ACPDBG_QUESTION") or "Why did this program stop, and how do I fix it?"
    _debug(debugger, question, result)


def _cmd_autoserve(debugger, command, result, internal_dict) -> None:
    # Fired from the autoserve stop-hook on every stop. Start the external
    # bridge on the first genuine stop (crash or breakpoint, not the benign
    # launch stops), and stay quiet once one is running — the stops an agent or
    # external client causes while driving execution land here too.
    if not _CONFIG.autoserve or _IN_SESSION or _SERVED_BRIDGE is not None:
        return
    if not LLDBInspector(debugger).is_reportable_stop():
        return
    _serve(debugger, "", result)


def _cmd_acpdbg(debugger, command, result, internal_dict) -> None:
    args = (command or "").strip()
    if not args or args == "help":
        result.AppendMessage(_HELP)
        return
    verb = args.split()[0]
    if verb == "config":
        _config(debugger, args[len("config"):].strip(), result)
        return
    if verb == "serve":
        _serve(debugger, args[len("serve"):].strip(), result)
        return
    if verb == "log":
        _show_log(args[len("log"):].strip(), result)
        return
    if verb == "last":
        result.AppendMessage(_LAST_TRANSCRIPT or "acpdbg: no session has run yet.")
        return
    _debug(debugger, args, result)


def _show_log(args: str, result) -> None:
    if args == "clear":
        _log.clear()
        result.AppendMessage("acpdbg: debug log cleared.")
        return
    count = int(args) if args.isdigit() else 40
    status = "on" if _log.enabled() else "off — enable with `acpdbg config debug on`"
    header = f"acpdbg debug log: {_log.log_path()} (debug {status})\n"
    text = _log.tail(count)
    result.AppendMessage(header + (text or "(log is empty)"))


def _debug(debugger, question, result) -> None:
    inspector = LLDBInspector(debugger)

    if not inspector.has_target():
        result.SetError("acpdbg: load and run a program first (e.g. `file ./a.out` then `run`).")
        return
    if inspector.stopped_thread() is None:
        result.SetError("acpdbg: the program is not stopped. Run it until it crashes or hits a breakpoint.")
        return

    # GUI frontends (Xcode) render a command's output only once, from the
    # SBCommandReturnObject, after the command returns — nothing written to the
    # debugger's output stream mid-command ever reaches their console. So on a
    # tty we stream lines as they arrive; otherwise we buffer the whole session
    # and hand it to `result` as a single block at the end.
    streaming = _console_isatty(debugger)
    notes = []

    def emit(text: str) -> None:
        if streaming:
            _out(debugger, text)
        else:
            notes.append(text)

    if not inspector.is_debug_build():
        emit("acpdbg: warning — no debug info found; compile with -g for best results.\n")

    context = inspector.crash_context()
    prompt_text = build_initial_prompt(context, question)

    try:
        session_cmd = resolve_session_command()
    except ConfigError as exc:
        result.SetError(str(exc))
        return

    _log.debug("plugin", f"ask: agent={_CONFIG.agent} question={question!r}")
    _log.debug("plugin", f"agent argv: {_CONFIG.describe_agent()}")
    _log.debug("plugin", f"session helper: {' '.join(session_cmd)}")

    emit(f"\nacpdbg → {_CONFIG.agent} (investigating…)\n\n")

    # Start the debugger bridge in-process (stdlib only) so the agent — running
    # in the helper — can reach the live process through the MCP tools.
    bridge = None
    bridge_info = None
    if _CONFIG.use_mcp:
        try:
            control = inspector.control if _CONFIG.allow_control else None
            bridge = CommandBridge(inspector.run_command, control=control)
            bridge_info = bridge.start()
            _log.debug("plugin", f"bridge listening on {bridge_info.socket_path}")
        except Exception as exc:
            emit(f"acpdbg: live debugger tools unavailable ({exc}); continuing.\n")
            _log.exception("plugin", "bridge failed to start")
            bridge = None

    global _IN_SESSION
    _IN_SESSION = True
    started = _time.monotonic()
    try:
        code = _run_session_process(debugger, session_cmd, prompt_text, bridge_info, streaming)
        _log.debug(
            "plugin",
            f"session exited: code={code} after {_time.monotonic() - started:.1f}s",
        )
        if code != 0:
            result.SetError(
                "acpdbg: the agent session exited with an error (see output above)."
                + _log_hint()
            )
    except Exception as exc:  # surface any transport failure cleanly
        _log.exception("plugin", "session raised")
        result.SetError(f"acpdbg: {type(exc).__name__}: {exc}{_log_hint()}")
    finally:
        _IN_SESSION = False
        if bridge is not None:
            bridge.stop()
            _log.debug("plugin", "bridge stopped")
        if not streaming:
            block = "".join(notes) + _LAST_TRANSCRIPT
            if block.strip():
                result.AppendMessage(block)


def _log_hint() -> str:
    if _log.enabled():
        return f"\nDetails: `acpdbg log` (or tail -f {_log.log_path()})."
    return "\nFor details, `acpdbg config debug on` and retry."


# Everything the last session printed, so users can re-read it with
# `acpdbg last` even if the console scrolled it away. In non-streaming mode
# (Xcode) this is also the block `_debug` hands to the result object.
_LAST_TRANSCRIPT = ""


def _run_session_process(debugger, session_cmd, prompt_text, bridge_info, streaming) -> int:
    """Run the `acpdbg-session` helper, streaming its output when on a tty."""
    global _LAST_TRANSCRIPT
    _LAST_TRANSCRIPT = ""
    env = dict(os.environ)
    env.update(_CONFIG.to_env())
    # The helper's stdout is always a pipe, so it cannot detect the console
    # itself; tell it whether ANSI styling will actually render (a terminal
    # yes, Xcode's console no).
    env["ACPDBG_COLOR"] = "1" if streaming else "0"
    if bridge_info is not None:
        env.update(bridge_info.env())

    # Pass the prompt via a temp file so the helper's stdin stays attached to the
    # console (needed for `permission prompt` mode).
    handle = tempfile.NamedTemporaryFile(
        "w", prefix="acpdbg-prompt-", suffix=".md", delete=False
    )
    transcript = []
    try:
        handle.write(prompt_text)
        handle.close()
        env["ACPDBG_PROMPT_FILE"] = handle.name

        proc = subprocess.Popen(
            session_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        _log.debug("plugin", f"helper started: pid={proc.pid}, waiting for output…")
        first = True
        for line in proc.stdout:
            if first:
                _log.debug("plugin", "first output line received")
                first = False
            if streaming:
                _out(debugger, line)
            transcript.append(line)
            _log.debug("plugin", f"» {line.rstrip()}")
        return proc.wait()
    finally:
        # Set even when the read loop raises, so a partial transcript still
        # reaches the result block and `acpdbg last`.
        _LAST_TRANSCRIPT = "".join(transcript)
        try:
            os.unlink(handle.name)
        except OSError:
            pass


# A long-lived bridge exposed to *external* MCP clients via `acpdbg serve`.
_SERVED_BRIDGE = None
_BRIDGE_FILE = os.path.expanduser("~/.acpdbg/bridge.json")


def _serve(debugger, args, result) -> None:
    global _SERVED_BRIDGE

    if args.split()[:1] == ["stop"]:
        if _SERVED_BRIDGE is not None:
            _SERVED_BRIDGE.stop()
            _SERVED_BRIDGE = None
            result.AppendMessage("acpdbg: external bridge stopped.")
        else:
            result.AppendMessage("acpdbg: no external bridge is running.")
        return

    if _SERVED_BRIDGE is not None:
        result.AppendMessage("acpdbg: an external bridge is already running (use `acpdbg serve stop`).")
        return

    inspector = LLDBInspector(debugger)
    if not inspector.has_target() or inspector.stopped_thread() is None:
        result.SetError("acpdbg: run the program and stop it (crash/breakpoint) before serving.")
        return

    control = inspector.control if _CONFIG.allow_control else None
    bridge = CommandBridge(inspector.run_command, control=control)
    info = bridge.start()
    try:
        info.to_file(_BRIDGE_FILE)
    except OSError as exc:
        bridge.stop()
        result.SetError(f"acpdbg: could not write bridge file: {exc}")
        return
    _SERVED_BRIDGE = bridge
    result.AppendMessage(_serve_message(control is not None))


def _serve_message(control_on: bool) -> str:
    import shutil

    mcp = shutil.which("acpdbg-mcp") or "acpdbg-mcp"
    env = {"ACPDBG_BRIDGE_FILE": _BRIDGE_FILE}
    if control_on:
        env["ACPDBG_CONTROL"] = "1"
    env_json = ", ".join(f'"{k}": "{v}"' for k, v in env.items())
    env_cli = " ".join(f"--env {k}={v}" for k, v in env.items())
    control_note = (
        "step/continue/breakpoint tools INCLUDED"
        if control_on
        else "read-only (run `acpdbg config control on` before serving to include step/continue)"
    )
    return (
        f"\nacpdbg: debugger exposed to external MCP clients — {control_note}.\n"
        f"  bridge file: {_BRIDGE_FILE}\n\n"
        "Add this MCP server to Claude Code / Copilot / Cursor:\n\n"
        f'  {{ "mcpServers": {{ "acpdbg": {{ "command": "{mcp}", "env": {{ {env_json} }} }} }} }}\n\n'
        f"or:  claude mcp add acpdbg {env_cli} -- {mcp}\n\n"
        "Keep this lldb session stopped while the other app drives it. Don't type\n"
        "lldb commands here at the same time. `acpdbg serve stop` to end.\n"
    )


# Whether this debugger already has the autoserve stop-hook (one config — and
# one plugin instance — per debugger session, so a module flag is enough).
_AUTOSERVE_HOOK_ADDED = False


def _ensure_autoserve_hook(debugger) -> None:
    """Arm the stop-hook that serves the session whenever the program stops.

    Added at most once. With no target yet, lldb puts the hook on the dummy
    target and copies it to every target created later, so this also works from
    ~/.lldbinit. The hook itself is a no-op while autoserve is off.
    """
    global _AUTOSERVE_HOOK_ADDED
    if _AUTOSERVE_HOOK_ADDED:
        return
    debugger.HandleCommand("target stop-hook add --one-liner acpdbg-autoserve")
    _AUTOSERVE_HOOK_ADDED = True


def _config(debugger, args: str, result) -> None:
    if not args:
        result.AppendMessage(_render_config())
        return

    parts = args.replace("=", " ").split()
    if len(parts) < 2:
        result.SetError("usage: acpdbg config <key> <value>")
        return
    key, value = parts[0].lower(), " ".join(parts[1:])

    try:
        note = _apply_config(key, value)
    except ValueError as exc:
        result.SetError(str(exc))
        return
    if note:
        result.AppendMessage(f"acpdbg: {note}")
    result.AppendMessage(_render_config())

    if key == "autoserve" and _CONFIG.autoserve:
        _ensure_autoserve_hook(debugger)
        # If the program is already stopped, don't wait for the next stop.
        if _SERVED_BRIDGE is None and LLDBInspector(debugger).stopped_thread() is not None:
            _serve(debugger, "", result)


def _apply_config(key: str, value: str) -> "str | None":
    """Set one config key; returns an optional note for the console."""
    on = value.lower() in ("on", "1", "true", "yes")
    if key == "agent":
        _CONFIG.agent = value
    elif key in ("permission", "permissions"):
        if value not in ("auto", "prompt"):
            raise ValueError("permission must be 'auto' or 'prompt'")
        _CONFIG.permission_mode = value
    elif key == "mcp":
        _CONFIG.use_mcp = on
    elif key == "control":
        _CONFIG.allow_control = on
    elif key == "unsafe":
        _CONFIG.unsafe = on
    elif key in ("writes", "allow_writes"):
        _CONFIG.allow_writes = on
    elif key == "autoserve":
        _CONFIG.autoserve = on
        if on and not _CONFIG.allow_control:
            # A read-only auto-served bridge is rarely useful; turn control on
            # too. `acpdbg config control off` afterwards undoes it.
            _CONFIG.allow_control = True
            return "autoserve implies control — turned control on as well."
    elif key == "timeout":
        _CONFIG.prompt_timeout = float(value)
    elif key == "debug":
        _CONFIG.debug = on
        _log.set_enabled(on)
        if on:
            _log.debug("plugin", f"debug logging enabled (acpdbg {__version__})")
    else:
        raise ValueError(f"unknown config key: {key}")
    return None


def _render_config() -> str:
    return (
        "acpdbg config:\n"
        f"  agent      = {_CONFIG.agent}  ({_CONFIG.describe_agent()})\n"
        f"  permission = {_CONFIG.permission_mode}\n"
        f"  mcp        = {'on' if _CONFIG.use_mcp else 'off'}\n"
        f"  control    = {'on' if _CONFIG.allow_control else 'off'}\n"
        f"  autoserve  = {'on' if _CONFIG.autoserve else 'off'}\n"
        f"  unsafe     = {'on' if _CONFIG.unsafe else 'off'}\n"
        f"  writes     = {'on' if _CONFIG.allow_writes else 'off'}\n"
        f"  timeout    = {_CONFIG.prompt_timeout:g}s\n"
        f"  debug      = {'on  (log: ' + _log.log_path() + ')' if _CONFIG.debug else 'off'}"
    )
