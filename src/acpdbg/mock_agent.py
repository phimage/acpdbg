"""A zero-setup ACP agent used as acpdbg's default backend.

It is a *real* ACP agent — it speaks the same protocol GitHub Copilot CLI,
Gemini CLI, or Claude Code speak — but it runs a fixed script rather than calling
a model:

1. parse the crash context out of the prompt,
2. read the faulting source line back through the ACP filesystem interface
   (proving the client/agent round-trip works end to end), and
3. stream a short, honest diagnosis.

This means ``pip install acpdbg`` gives you a working demo with nothing else to
install. Swap in a real agent for real analysis.
"""

from __future__ import annotations

import asyncio
import re
import sys
from typing import Any
from uuid import uuid4

from acp import (
    Agent,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    run_agent,
    update_agent_message_text,
    update_agent_thought_text,
)

_LOCATION = re.compile(r"^at\s+(\S+?):(\d+)\s*$", re.MULTILINE)
_FRAME = re.compile(r"### Frame #(\d+):\s*`([^(`]+)")
_STOP = re.compile(r"## Stop reason\s*\n+```\s*\n(.*?)\n```", re.DOTALL)


def _block_text(block: Any) -> str:
    if isinstance(block, dict):
        return block.get("text", "")
    return getattr(block, "text", "") or ""


class MockDebugAgent(Agent):
    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def initialize(self, protocol_version: int, **kwargs: Any) -> InitializeResponse:
        return InitializeResponse(protocol_version=protocol_version)

    async def new_session(self, cwd: str, **kwargs: Any) -> NewSessionResponse:
        return NewSessionResponse(session_id=uuid4().hex)

    async def prompt(self, session_id: str, prompt: list, **kwargs: Any) -> PromptResponse:
        text = "\n".join(_block_text(b) for b in prompt)

        await self._thought(session_id, "reading the captured crash context…")

        stop = self._first(_STOP, text)
        location = _LOCATION.search(text)
        frame = _FRAME.search(text)

        code_line = None
        if location:
            path, line = location.group(1), int(location.group(2))
            await self._thought(session_id, f"opening {path}:{line} via the ACP filesystem…")
            code_line = await self._read_line(session_id, path, line)

        await self._message(session_id, self._diagnosis(stop, frame, location, code_line))
        return PromptResponse(stop_reason="end_turn")

    # --- helpers ----------------------------------------------------------
    async def _thought(self, session_id: str, text: str) -> None:
        await self._conn.session_update(
            session_id=session_id, update=update_agent_thought_text(text)
        )

    async def _message(self, session_id: str, text: str) -> None:
        await self._conn.session_update(
            session_id=session_id, update=update_agent_message_text(text)
        )

    async def _read_line(self, session_id: str, path: str, line: int) -> str | None:
        try:
            response = await self._conn.read_text_file(
                session_id=session_id, path=path, line=line, limit=1
            )
        except Exception:
            return None
        content = getattr(response, "content", None)
        if content is None and isinstance(response, dict):
            content = response.get("content")
        return content.strip() if content else None

    @staticmethod
    def _first(pattern: re.Pattern, text: str) -> str | None:
        match = pattern.search(text)
        return match.group(1).strip() if match else None

    @staticmethod
    def _diagnosis(stop, frame, location, code_line) -> str:
        lines = ["**acpdbg mock agent — analysis**", ""]
        if stop:
            lines.append(f"The program stopped with: `{stop}`.")
        if frame and location:
            func = frame.group(2).strip()
            lines.append(
                f"The current frame is `{func}` at "
                f"`{location.group(1)}:{location.group(2)}`."
            )
        if code_line:
            lines += ["", "The current source line reads:", "", f"    {code_line}", ""]
        if stop and ("EXC_BAD_ACCESS" in stop or "SIGSEGV" in stop or "SIGABRT" in stop):
            lines.append(
                "That signal is a bad memory access — most often a NULL or dangling "
                "pointer dereference, or an out-of-bounds index. Check the pointer/index "
                "used on the line above before it is read or written."
            )
        lines += [
            "",
            "_This is the built-in mock agent: it demonstrates the full ACP loop "
            "(context capture → filesystem access → streamed reply). "
            "For a real root-cause analysis and fix, point acpdbg at a coding agent, "
            "e.g. `acpdbg config agent copilot`._",
        ]
        return "\n".join(lines)


async def _amain() -> None:
    await run_agent(MockDebugAgent())


def main() -> int:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
