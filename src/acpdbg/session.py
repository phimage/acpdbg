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
"""

from __future__ import annotations

import os
import sys

from . import log
from .acp_client import run_prompt
from .bridge import BridgeInfo
from .config import Config


def _read_prompt() -> str:
    prompt_file = os.environ.get("ACPDBG_PROMPT_FILE")
    if prompt_file:
        with open(prompt_file, "r", errors="replace") as handle:
            return handle.read()
    return sys.stdin.read()


def main() -> int:
    # The plugin reads our stdout as UTF-8; make sure we emit it regardless of
    # the (possibly ASCII) locale inherited from the debugger.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    prompt_text = _read_prompt()
    config = Config.from_env()
    bridge_info = BridgeInfo.from_env() if config.use_mcp else None
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
