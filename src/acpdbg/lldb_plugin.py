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
from .config import (
    SESSION_PERSISTENT_ENV,
    SESSION_TURN_END,
    Config,
    ConfigError,
    resolve_session_command,
)
from .lldb_inspector import LLDBInspector
from .prompts import build_followup_prompt, build_initial_prompt, build_restop_prompt

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
  acpdbg session            show the persistent agent conversation's status
  acpdbg session start      open the agent conversation now (asks reuse it)
  acpdbg session stop       end it (drops the agent's memory of this debug)
  acpdbg session reset      fresh conversation without restarting the agent
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
             session (on|off), autoserve (on|off),
             timeout <seconds>, debug (on|off)

By default (`session on`) the first ask opens one persistent agent conversation
and later asks continue it: the agent remembers earlier turns and its startup
cost is paid once. `session off` gives every ask a fresh one-shot agent.

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
    if verb == "session":
        _session_cmd(debugger, args[len("session"):].strip(), result)
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

    try:
        if _route_persistent(emit):
            _debug_persistent(debugger, inspector, question, streaming, emit, result)
        else:
            _debug_oneshot(debugger, inspector, question, streaming, emit, result)
    finally:
        if not streaming:
            block = "".join(notes) + _LAST_TRANSCRIPT
            if block.strip():
                result.AppendMessage(block)


def _route_persistent(emit) -> bool:
    """Whether this ask goes to the persistent conversation or a one-shot."""
    if _session_running():
        if _CONFIG.agent != _SESSION.agent:
            emit(
                f"acpdbg: asking '{_CONFIG.agent}' one-shot — the persistent session "
                f"is with '{_SESSION.agent}' (`acpdbg session stop` to switch over).\n"
            )
            return False
        return True
    if not _CONFIG.session:
        return False
    if _CONFIG.permission_mode == "prompt":
        # Interactive permission approval reads the console through the helper's
        # stdin; a persistent helper's stdin is the command pipe instead.
        emit(
            "acpdbg: `permission prompt` needs the console, so this ask runs "
            "one-shot (`acpdbg config permission auto` enables the persistent session).\n"
        )
        return False
    return True


def _debug_oneshot(debugger, inspector, question, streaming, emit, result) -> None:
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


def _debug_persistent(debugger, inspector, question, streaming, emit, result) -> None:
    global _IN_SESSION, _LAST_TRANSCRIPT
    transcript = []
    try:
        sess = _SESSION
        just_spawned = False
        if sess is None:
            emit(
                f"\nacpdbg → {_CONFIG.agent} (starting the persistent session — "
                "later asks reuse it)\n\n"
            )
            sess, error = _session_spawn(debugger, streaming, transcript)
            if sess is None:
                result.SetError(f"acpdbg: {error}{_log_hint()}")
                return
            just_spawned = True

        # First turn gets the full captured context; follow-ups only the
        # question — unless the program stopped anew since the last ask, in
        # which case the agent needs fresh context.
        generation = inspector.stop_generation()
        if sess.turns == 0:
            prompt_text = build_initial_prompt(inspector.crash_context(), question)
        elif generation != sess.stop_generation:
            prompt_text = build_restop_prompt(inspector.crash_context(), question)
        else:
            prompt_text = build_followup_prompt(question)
        sess.stop_generation = generation

        if not just_spawned:
            emit(f"\nacpdbg → {sess.agent} (session turn {sess.turns + 1})\n\n")

        handle = tempfile.NamedTemporaryFile(
            "w", prefix="acpdbg-prompt-", suffix=".md", delete=False
        )
        _IN_SESSION = True
        try:
            handle.write(prompt_text)
            handle.close()
            _log.debug(
                "plugin",
                f"session turn {sess.turns + 1}: {len(prompt_text)} chars, question={question!r}",
            )
            status = _session_send(debugger, sess, f"PROMPT {handle.name}", streaming, transcript)
        finally:
            _IN_SESSION = False
            try:
                os.unlink(handle.name)
            except OSError:
                pass

        if status is None:
            _session_teardown()
            result.SetError(
                "acpdbg: the agent session ended unexpectedly (see output above)."
                + _log_hint()
            )
            return
        sess.turns += 1
        if status != "ok":
            result.SetError("acpdbg: the agent turn failed (see output above)." + _log_hint())
    finally:
        # Even a partial transcript should reach `acpdbg last` and, in
        # non-streaming (Xcode) mode, the result block assembled by _debug.
        _LAST_TRANSCRIPT = "".join(transcript)


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


# --- persistent agent session ------------------------------------------------
# One long-lived `acpdbg-session` helper (and thus one agent conversation) per
# debugger session, opened lazily by the first ask when `config session` is on,
# or explicitly with `acpdbg session start`. See the protocol notes in config.py.


class _PersistentSession:
    def __init__(self, proc, bridge, agent: str) -> None:
        self.proc = proc          # the helper subprocess (stdin = command pipe)
        self.bridge = bridge      # CommandBridge kept alive for the whole session
        self.agent = agent        # agent the conversation was opened with
        self.turns = 0
        self.started = _time.time()
        self.stop_generation = None


_SESSION = None  # type: _PersistentSession | None


def _session_running() -> bool:
    """Whether the persistent helper is alive; reaps it silently if it died."""
    global _SESSION
    if _SESSION is None:
        return False
    if _SESSION.proc.poll() is not None:
        _log.debug("plugin", f"session helper exited (code={_SESSION.proc.returncode})")
        _session_teardown()
        return False
    return True


def _session_teardown() -> None:
    global _SESSION
    sess, _SESSION = _SESSION, None
    if sess is None:
        return
    _session_close(sess)


def _session_close(sess) -> None:
    try:
        if sess.proc.stdin is not None:
            sess.proc.stdin.close()  # EOF tells the helper to shut down
    except (OSError, ValueError):
        pass
    if sess.proc.poll() is None:
        try:
            sess.proc.wait(timeout=5)
        except Exception:
            try:
                sess.proc.kill()
            except Exception:
                pass
    if sess.bridge is not None:
        try:
            sess.bridge.stop()
        except Exception:
            pass
    _log.debug("plugin", "persistent session closed")


def _session_spawn(debugger, streaming, transcript):
    """Start helper + agent + conversation. Returns (session, None) or (None, error).

    On success the session is registered as the module-wide _SESSION. Startup
    output (heartbeat notes and any agent chatter) is forwarded like a turn.
    """
    global _SESSION
    try:
        session_cmd = resolve_session_command()
    except ConfigError as exc:
        return None, str(exc)

    _log.debug("plugin", f"session spawn: agent={_CONFIG.agent}")
    _log.debug("plugin", f"agent argv: {_CONFIG.describe_agent()}")
    _log.debug("plugin", f"session helper: {' '.join(session_cmd)}")

    # The bridge must outlive this call: its socket path is baked into the
    # agent's MCP config at session/new, so it is owned by the session.
    bridge = None
    bridge_env = {}
    if _CONFIG.use_mcp:
        try:
            inspector = LLDBInspector(debugger)
            control = inspector.control if _CONFIG.allow_control else None
            bridge = CommandBridge(inspector.run_command, control=control)
            info = bridge.start()
            bridge_env = info.env()
            _log.debug("plugin", f"session bridge listening on {info.socket_path}")
        except Exception:
            _log.exception("plugin", "session bridge failed to start")
            bridge = None
            bridge_env = {}

    env = dict(os.environ)
    env.update(_CONFIG.to_env())
    env[SESSION_PERSISTENT_ENV] = "1"
    env["ACPDBG_COLOR"] = "1" if streaming else "0"
    env.update(bridge_env)
    try:
        proc = subprocess.Popen(
            session_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
    except OSError as exc:
        if bridge is not None:
            bridge.stop()
        return None, f"failed to launch the session helper: {exc}"

    sess = _PersistentSession(proc, bridge, _CONFIG.agent)
    status = _session_read_turn(debugger, sess, streaming, transcript)
    if status != "ready":
        _session_close(sess)
        return None, "the agent session failed to start (see output above)."
    _SESSION = sess
    _log.debug("plugin", f"persistent session ready (helper pid={proc.pid})")
    return sess, None


def _session_send(debugger, sess, command: str, streaming, transcript):
    """Send one protocol command and forward output until the end-of-turn mark.

    Returns the helper's status word, or None if the helper went away.
    """
    try:
        sess.proc.stdin.write(command + "\n")
        sess.proc.stdin.flush()
    except (OSError, ValueError):
        return None
    return _session_read_turn(debugger, sess, streaming, transcript)


def _session_read_turn(debugger, sess, streaming, transcript):
    while True:
        line = sess.proc.stdout.readline()
        if not line:
            return None
        if line.startswith(SESSION_TURN_END):
            return line[len(SESSION_TURN_END):].strip() or "ok"
        if streaming:
            _out(debugger, line)
        transcript.append(line)
        _log.debug("plugin", f"» {line.rstrip()}")


def _session_cmd(debugger, args: str, result) -> None:
    verb = args.split()[0] if args.split() else "status"
    streaming = _console_isatty(debugger)

    if verb == "status":
        if _session_running():
            sess = _SESSION
            uptime = _time.time() - sess.started
            result.AppendMessage(
                f"acpdbg session: agent={sess.agent}, turns={sess.turns}, "
                f"uptime={uptime:.0f}s, helper pid={sess.proc.pid}, "
                f"debugger tools={'on' if sess.bridge is not None else 'off'}."
            )
        elif _CONFIG.session:
            result.AppendMessage(
                "acpdbg: no persistent session running — the first ask opens one "
                "(config `session on`)."
            )
        else:
            result.AppendMessage(
                "acpdbg: no persistent session running, and config `session off` "
                "makes every ask one-shot. `acpdbg session start` opens one anyway."
            )
        return

    if verb == "start":
        if _session_running():
            result.AppendMessage(
                "acpdbg: a persistent session is already running (`acpdbg session` for status)."
            )
            return
        if _CONFIG.permission_mode == "prompt":
            result.SetError(
                "acpdbg: `permission prompt` needs the console and can't drive a "
                "persistent session; run `acpdbg config permission auto` first."
            )
            return
        transcript = []
        sess, error = _session_spawn(debugger, streaming, transcript)
        if not streaming and transcript:
            result.AppendMessage("".join(transcript))
        if sess is None:
            result.SetError(f"acpdbg: {error}{_log_hint()}")
        else:
            result.AppendMessage(
                f"acpdbg: session started with {sess.agent} — `ask` now continues "
                "one conversation. `acpdbg session stop` ends it."
            )
        return

    if verb == "stop":
        if not _session_running():
            result.AppendMessage("acpdbg: no persistent session is running.")
            return
        _session_teardown()
        message = "acpdbg: session stopped."
        if _CONFIG.session:
            message += " The next ask opens a fresh one (`acpdbg config session off` to stay one-shot)."
        result.AppendMessage(message)
        return

    if verb == "reset":
        if not _session_running():
            result.AppendMessage("acpdbg: no persistent session is running — nothing to reset.")
            return
        sess = _SESSION
        transcript = []
        status = _session_send(debugger, sess, "RESET", streaming, transcript)
        if not streaming and transcript:
            result.AppendMessage("".join(transcript))
        if status == "ok":
            sess.turns = 0
            sess.stop_generation = None
            result.AppendMessage(
                "acpdbg: session reset — the agent forgot the conversation "
                "(same agent process, so no startup cost)."
            )
        else:
            _session_teardown()
            result.SetError("acpdbg: reset failed; the session was stopped." + _log_hint())
        return

    result.SetError("usage: acpdbg session [start|stop|reset]")


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
        if _session_running() and _SESSION.agent != value:
            return (
                f"the persistent session still talks to '{_SESSION.agent}'; asks run "
                f"one-shot with '{value}' until `acpdbg session stop`."
            )
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
    elif key == "session":
        _CONFIG.session = on
        if not on and _session_running():
            return "the running session stays until `acpdbg session stop`; new asks after that are one-shot."
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
        f"  session    = {'on' if _CONFIG.session else 'off'}"
        f"{'  (running: ' + _SESSION.agent + ', ' + str(_SESSION.turns) + ' turns)' if _session_running() else ''}\n"
        f"  timeout    = {_CONFIG.prompt_timeout:g}s\n"
        f"  debug      = {'on  (log: ' + _log.log_path() + ')' if _CONFIG.debug else 'off'}"
    )
