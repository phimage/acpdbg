"""Out-of-process ACP helper (`acpdbg-session`).

The lldb plugin runs inside the debugger's embedded Python, which may be too old
for the ACP SDK (Xcode's lldb ships Python 3.9). So the plugin keeps only
stdlib-level work in-process and launches this helper — on a modern Python — to
actually drive the coding agent.

Inputs (all from the environment, plus the prompt):
  * the prompt text: read from the file named by ``ACPDBG_PROMPT_FILE`` (or, if
    that is unset, from stdin);
  * the configuration: the ``ACPDBG_*`` variables (see :class:`acpdbg.config.Config`);
  * the debugger bridge: ``ACPDBG_BRIDGE_SOCKET`` / ``ACPDBG_BRIDGE_TOKEN``, which
    point back at the live lldb session so the agent can run debugger commands.

The agent's streamed reply is written to stdout, which the plugin forwards to the
lldb console.

With ``ACPDBG_PERSISTENT=1`` the helper instead keeps one agent conversation
alive and serves many turns, one per ``PROMPT <path>`` line on stdin — see the
protocol notes next to :data:`acpdbg.config.SESSION_TURN_END`.
"""

from __future__ import annotations

import asyncio
import os
import sys

from . import log
from .acp_client import AgentSession, ConsolePrinter, run_prompt
from .bridge import BridgeInfo
from .config import SESSION_PERSISTENT_ENV, SESSION_TURN_END, Config


def _read_prompt() -> str:
    prompt_file = os.environ.get("ACPDBG_PROMPT_FILE")
    if prompt_file:
        with open(prompt_file, "r", errors="replace") as handle:
            return handle.read()
    return sys.stdin.read()


def _end_turn(status: str) -> None:
    print(f"{SESSION_TURN_END} {status}", flush=True)


async def _persistent_loop(config: Config, bridge_info: "BridgeInfo | None") -> int:
    session = AgentSession(config, bridge=bridge_info, printer=ConsolePrinter())
    try:
        try:
            await session.start()
        except Exception as exc:
            log.exception("session", "persistent start failed")
            print(f"\nacpdbg: {type(exc).__name__}: {exc}", flush=True)
            _end_turn("error")
            return 1
        _end_turn("ready")

        loop = asyncio.get_running_loop()
        while True:
            # stdin EOF means the plugin (or the whole debugger) went away.
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                log.debug("session", "stdin EOF — shutting down")
                break
            command, _, arg = line.strip().partition(" ")
            if command == "PROMPT":
                try:
                    with open(arg.strip(), "r", errors="replace") as handle:
                        prompt_text = handle.read()
                    await session.prompt(prompt_text)
                    _end_turn("ok")
                except Exception as exc:
                    log.exception("session", "persistent turn raised")
                    print(f"\nacpdbg: {type(exc).__name__}: {exc}", flush=True)
                    _end_turn("error")
            elif command == "RESET":
                try:
                    await session.reset()
                    _end_turn("ok")
                except Exception as exc:
                    log.exception("session", "persistent reset raised")
                    print(f"\nacpdbg: {type(exc).__name__}: {exc}", flush=True)
                    _end_turn("error")
            elif command:
                log.debug("session", f"unknown persistent command: {command!r}")
                _end_turn("error")
    finally:
        await session.close()
    return 0


def main() -> int:
    # The plugin reads our stdout as UTF-8; make sure we emit it regardless of
    # the (possibly ASCII) locale inherited from the debugger.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    config = Config.from_env()
    bridge_info = BridgeInfo.from_env() if config.use_mcp else None

    if os.environ.get(SESSION_PERSISTENT_ENV, "").strip() == "1":
        log.debug(
            "session",
            f"persistent helper started: python {sys.version.split()[0]}, "
            f"agent={config.agent}, bridge={'yes' if bridge_info else 'no'}",
        )
        return asyncio.run(_persistent_loop(config, bridge_info))

    prompt_text = _read_prompt()
    log.debug(
        "session",
        f"helper started: python {sys.version.split()[0]}, agent={config.agent}, "
        f"mcp={'on' if config.use_mcp else 'off'}, bridge={'yes' if bridge_info else 'no'}, "
        f"prompt={len(prompt_text)} chars",
    )
    try:
        run_prompt(config, prompt_text, bridge_info=bridge_info)
    except Exception as exc:  # surface failures to the lldb console via stdout
        log.exception("session", "run_prompt raised")
        print(f"\nacpdbg: {type(exc).__name__}: {exc}", flush=True)
        return 1
    log.debug("session", "helper finished cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
