"""The ``acpdbg`` command-line entry point.

    acpdbg -- ./a.out arg1 arg2
    acpdbg --agent gemini -q "why the segfault?" -- ./a.out

It launches your program under ``lldb`` in batch mode and, if the program
stops (crash or signal), automatically hands the stopped process to your coding
agent via the acpdbg LLDB plugin. Under the hood it runs roughly:

    lldb --batch \
      -o "command script import acpdbg.lldb_plugin" \
      -o "target stop-hook add --one-liner acpdbg-auto" \
      -o run \
      -o quit \
      -- ./a.out arg1 arg2

The stop-hook fires the moment the process stops on the fault, which is more
portable across LLDB builds than ``--one-line-on-crash``. The question is passed
to the plugin via the ``ACPDBG_QUESTION`` environment variable.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="acpdbg",
        description="Debug native crashes with your coding agent over ACP.",
    )
    parser.add_argument("--version", action="version", version=f"acpdbg {__version__}")
    parser.add_argument(
        "-a", "--agent", default=None,
        help="Agent to use: mock (default), copilot, gemini, claude-code, or a raw command.",
    )
    parser.add_argument(
        "-q", "--question", default="Why did this program stop, and how do I fix it?",
        help="The question to ask the agent when the program stops.",
    )
    parser.add_argument(
        "--permission", choices=["auto", "prompt"], default=None,
        help="Approve agent tool use automatically (default) or ask on the console.",
    )
    parser.add_argument("--no-mcp", action="store_true", help="Do not expose live debugger tools to the agent.")
    parser.add_argument(
        "--control", action="store_true",
        help="Let the agent drive execution (step/continue/breakpoints), like a human debugger.",
    )
    parser.add_argument("--unsafe", action="store_true", help="Disable the debugger-command safety filter.")
    parser.add_argument("--writes", action="store_true", help="Let the agent write files (e.g. apply a fix).")
    parser.add_argument(
        "--autoserve", action="store_true",
        help="Expose the session to external MCP clients automatically whenever "
             "the program stops (implies --control). See `acpdbg serve`.",
    )
    parser.add_argument("--agent-stderr", action="store_true", help="Show the agent subprocess's stderr.")
    parser.add_argument(
        "--debug", action="store_true",
        help="Log every acpdbg process to ~/.acpdbg/acpdbg.log.",
    )
    parser.add_argument("--lldb", default=None, help="Path to the lldb executable (default: found on PATH).")
    parser.add_argument("--dry-run", action="store_true", help="Print the lldb command that would run and exit.")
    parser.add_argument(
        "--install-lldbinit", action="store_true",
        help="Add acpdbg to your ~/.lldbinit so `ask`/`why` load in every lldb session, "
             "then exit. Options given alongside (--agent, --control, --debug, "
             "--autoserve, …) are baked in as session defaults.",
    )
    parser.add_argument(
        "--print-lldbinit", action="store_true",
        help="Print the ~/.lldbinit snippet that --install-lldbinit would add, then exit.",
    )
    parser.add_argument(
        "--lldbinit-path", default=None,
        help="Path to the init file to modify (default: ~/.lldbinit).",
    )
    parser.add_argument(
        "program", nargs=argparse.REMAINDER,
        help="The program to run, then its arguments. Prefix with `--`.",
    )
    return parser


def _child_env(args: argparse.Namespace) -> dict[str, str]:
    """Pass CLI options to the plugin (running inside lldb) via ACPDBG_* env."""
    env = dict(os.environ)
    if args.agent is not None:
        env["ACPDBG_AGENT"] = args.agent
    if args.permission is not None:
        env["ACPDBG_PERMISSION"] = args.permission
    if args.no_mcp:
        env["ACPDBG_MCP"] = "0"
    if args.control:
        env["ACPDBG_CONTROL"] = "1"
    if args.unsafe:
        env["ACPDBG_UNSAFE"] = "1"
    if args.writes:
        env["ACPDBG_ALLOW_WRITES"] = "1"
    if args.autoserve:
        env["ACPDBG_AUTOSERVE"] = "1"
    if args.agent_stderr:
        env["ACPDBG_AGENT_STDERR"] = "1"
    if args.debug:
        env["ACPDBG_DEBUG"] = "1"
    env["ACPDBG_QUESTION"] = args.question

    # Make the acpdbg package importable from lldb's bundled Python. Only the
    # package's parent dir: the plugin is pure stdlib, and injecting this
    # interpreter's full sys.path would shadow the (possibly older) embedded
    # Python's own standard library with ours.
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (_package_dir(), existing) if p)

    # Tell the plugin which Python to run the agent helper with (this interpreter,
    # where acpdbg and the ACP SDK are installed), regardless of lldb's Python.
    session_cmd = _session_command_path()
    if session_cmd:
        env["ACPDBG_SESSION_CMD"] = session_cmd
    return env


def build_lldb_command(lldb_path: str, program: list[str]) -> list[str]:
    # The question is passed via the ACPDBG_QUESTION env var (see _child_env),
    # so the stop-hook one-liner needs no arguments and no fragile quoting.
    return [
        lldb_path,
        "--batch",
        "-o", "command script import acpdbg.lldb_plugin",
        "-o", "target stop-hook add --one-liner acpdbg-auto",
        "-o", "run",
        "-o", "quit",
        "--",
        *program,
    ]


_LLDBINIT_BEGIN = "# >>> acpdbg >>>"
_LLDBINIT_END = "# <<< acpdbg <<<"


def _package_dir() -> str:
    """The directory that must be on sys.path for `import acpdbg` to work."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _session_command_path() -> str:
    """Absolute path to the `acpdbg-session` helper, or "" if not found.

    Baked into ~/.lldbinit so the plugin (which may run under an old lldb Python)
    launches the agent helper with the modern Python acpdbg is installed in.
    """
    found = shutil.which("acpdbg-session")
    if found:
        return found
    candidate = os.path.join(os.path.dirname(sys.executable), "acpdbg-session")
    return candidate if os.path.exists(candidate) else ""


def _agent_bin_dir(agent: str) -> str:
    """Directory holding the agent's executable, resolved at install time.

    Baked onto the plugin's PATH so the agent is found even under Xcode's minimal
    GUI environment (which lacks e.g. /opt/homebrew/bin).
    """
    from .config import Config, ConfigError

    try:
        argv = Config(agent=agent).agent_argv()
    except ConfigError:
        return ""
    exe = argv[0]
    return os.path.dirname(exe) if os.path.isabs(exe) else ""


def lldbinit_env(args: argparse.Namespace) -> dict[str, str]:
    """ACPDBG_* defaults to bake into ~/.lldbinit for non-default CLI options.

    Only options that differ from the built-in defaults are recorded, and each
    is written with ``setdefault`` so a real environment variable at lldb's
    launch still wins.
    """
    env: dict[str, str] = {}
    if args.permission is not None:
        env["ACPDBG_PERMISSION"] = args.permission
    if args.no_mcp:
        env["ACPDBG_MCP"] = "0"
    if args.control:
        env["ACPDBG_CONTROL"] = "1"
    if args.unsafe:
        env["ACPDBG_UNSAFE"] = "1"
    if args.writes:
        env["ACPDBG_ALLOW_WRITES"] = "1"
    if args.autoserve:
        env["ACPDBG_AUTOSERVE"] = "1"
    if args.agent_stderr:
        env["ACPDBG_AGENT_STDERR"] = "1"
    if args.debug:
        env["ACPDBG_DEBUG"] = "1"
    return env


def lldbinit_block(agent: str | None = None, extra_env: dict[str, str] | None = None) -> str:
    """The self-contained snippet that makes lldb load the acpdbg commands.

    The plugin itself is pure standard library, so it only needs acpdbg's own
    directory on the path (appended, so it can't shadow lldb's stdlib). The agent
    helper runs out-of-process, so its path is recorded separately. When an
    ``agent`` is given, it is baked in as the default (with its bin directory
    added to PATH) so it works from GUI-launched debuggers like Xcode; any
    ``extra_env`` ACPDBG_* defaults (see :func:`lldbinit_env`) are baked in the
    same way.
    """
    lines = [
        _LLDBINIT_BEGIN,
        "# Managed by `acpdbg --install-lldbinit`. Loads the ask/why/acpdbg commands.",
        f"script import sys; _p = {_package_dir()!r}; _ = (_p in sys.path) or sys.path.append(_p)",
    ]
    # Assignment statements: `script <expr>` echoes the expression's value to
    # the console on every launch, an assignment stays quiet.
    session_cmd = _session_command_path()
    if session_cmd:
        lines.append(
            f"script import os; _ = os.environ.setdefault('ACPDBG_SESSION_CMD', {session_cmd!r})"
        )
    if agent:
        bin_dir = _agent_bin_dir(agent)
        if bin_dir:
            lines.append(
                f"script import os; os.environ['PATH'] = {bin_dir!r} + os.pathsep + os.environ.get('PATH', '')"
            )
        lines.append(
            f"script import os; _ = os.environ.setdefault('ACPDBG_AGENT', {agent!r})"
        )
    for key, value in (extra_env or {}).items():
        lines.append(
            f"script import os; _ = os.environ.setdefault({key!r}, {value!r})"
        )
    lines += [
        "command script import acpdbg.lldb_plugin",
        _LLDBINIT_END,
        "",
    ]
    return "\n".join(lines)


def install_lldbinit(path: str, agent: str | None = None, extra_env: dict[str, str] | None = None) -> None:
    block = lldbinit_block(agent, extra_env)
    existing = ""
    if os.path.exists(path):
        with open(path, "r") as handle:
            existing = handle.read()

    if _LLDBINIT_BEGIN in existing and _LLDBINIT_END in existing:
        head = existing[: existing.index(_LLDBINIT_BEGIN)]
        tail = existing[existing.index(_LLDBINIT_END) + len(_LLDBINIT_END):]
        tail = tail.lstrip("\n")
        updated = head.rstrip("\n") + ("\n\n" if head.strip() else "") + block + tail
        action = "Updated"
    else:
        sep = "" if not existing or existing.endswith("\n") else "\n"
        updated = existing + sep + ("\n" if existing.strip() else "") + block
        action = "Added acpdbg to"

    with open(path, "w") as handle:
        handle.write(updated)
    baked = [f"agent={agent}"] if agent else []
    baked += [f"{k}={v}" for k, v in (extra_env or {}).items()]
    print(f"{action} {path}." + (f" Baked-in defaults: {', '.join(baked)}." if baked else ""))
    print("Open any program with `lldb ./a.out`; after it stops, run `ask why did this stop?`")
    _warn_on_python_mismatch()


def _warn_on_python_mismatch() -> None:
    lldb_path = shutil.which("lldb")
    if not lldb_path:
        return
    try:
        out = os.popen(f'"{lldb_path}" -P 2>/dev/null').read()
    except OSError:
        return
    import re

    match = re.search(r"python(\d+)\.(\d+)", out)
    if not match:
        return
    lldb_ver = (int(match.group(1)), int(match.group(2)))
    if lldb_ver != sys.version_info[:2]:
        print(
            f"\nNote: lldb uses Python {lldb_ver[0]}.{lldb_ver[1]} but acpdbg is installed "
            f"under Python {sys.version_info[0]}.{sys.version_info[1]}.\n"
            "If the import fails, install acpdbg into lldb's Python interpreter instead."
        )


def _strip_separator(program: list[str]) -> list[str]:
    # argparse.REMAINDER keeps a leading "--" if the user wrote `acpdbg -- prog`.
    if program and program[0] == "--":
        return program[1:]
    return program


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.print_lldbinit:
        print(lldbinit_block(args.agent, lldbinit_env(args)), end="")
        return 0
    if args.install_lldbinit:
        path = args.lldbinit_path or os.path.expanduser("~/.lldbinit")
        install_lldbinit(path, args.agent, lldbinit_env(args))
        return 0

    program = _strip_separator(args.program)

    if not program:
        print("acpdbg: no program given. Try: acpdbg -- ./a.out", file=sys.stderr)
        return 2

    lldb_path = args.lldb or shutil.which("lldb")
    if not lldb_path:
        print(
            "acpdbg: could not find `lldb` on PATH. Install LLDB or pass --lldb <path>.\n"
            "  macOS:  xcode-select --install\n"
            "  Debian: sudo apt install lldb",
            file=sys.stderr,
        )
        return 1

    command = build_lldb_command(lldb_path, program)

    if args.dry_run:
        print(" ".join(_shell_quote(c) for c in command))
        return 0

    env = _child_env(args)
    try:
        completed = os.spawnve(os.P_WAIT, lldb_path, command, env)
    except OSError as exc:
        print(f"acpdbg: failed to launch lldb: {exc}", file=sys.stderr)
        return 1
    return completed


def _shell_quote(text: str) -> str:
    if text and all(c.isalnum() or c in "-_./=:" for c in text):
        return text
    return "'" + text.replace("'", "'\\''") + "'"


if __name__ == "__main__":
    raise SystemExit(main())
