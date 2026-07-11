"""Debugger-agnostic model of a crash/stop and how to render it for an agent.

This module deliberately knows nothing about LLDB. A debugger backend (see
``lldb_inspector.py``) is responsible for producing a :class:`CrashContext`;
everything here is plain data plus formatting, which makes it trivial to unit
test without a live debugger.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Arg:
    """A single function argument captured from a stack frame."""

    type: str
    name: str
    value: Optional[str]

    def render(self) -> str:
        value = self.value if self.value is not None else "<unavailable>"
        return f"{self.type} {self.name} = {value}"


@dataclass
class Frame:
    """One frame of the stopped thread's backtrace."""

    index: int
    function: str
    args: list[Arg] = field(default_factory=list)
    file: Optional[str] = None
    line: Optional[int] = None
    # A few lines of source centred on ``line`` (already formatted with numbers).
    source: Optional[str] = None

    def signature(self) -> str:
        args = ", ".join(a.render() for a in self.args)
        return f"{self.function}({args})"

    def location(self) -> str:
        if self.file and self.line:
            return f"{self.file}:{self.line}"
        if self.file:
            return self.file
        return "<unknown location>"


@dataclass
class SkippedFrames:
    """Marker for a run of frames with no source (system/library code)."""

    count: int


@dataclass
class CrashContext:
    """Everything acpdbg knows about a stopped program at one moment in time."""

    stop_reason: Optional[str] = None
    command_line: Optional[str] = None
    program_input: Optional[str] = None
    frames: list = field(default_factory=list)  # list[Frame | SkippedFrames]
    extra: Optional[str] = None

    def render(self) -> str:
        """Render the context as Markdown suitable for a prompt."""
        parts: list[str] = []

        if self.stop_reason:
            parts.append(f"## Stop reason\n\n```\n{self.stop_reason.strip()}\n```")

        if self.command_line:
            parts.append(f"## Command line\n\n```\n{self.command_line.strip()}\n```")

        if self.program_input:
            parts.append(
                "## Program input\n\n```\n" + self.program_input.strip() + "\n```"
            )

        parts.append("## Backtrace\n\n" + self._render_backtrace())

        if self.extra:
            parts.append(self.extra.strip())

        return "\n\n".join(parts)

    def _render_backtrace(self) -> str:
        if not self.frames:
            return "_No stack frames with source were available._"

        blocks: list[str] = []
        for entry in self.frames:
            if isinstance(entry, SkippedFrames):
                noun = "frame" if entry.count == 1 else "frames"
                blocks.append(f"... {entry.count} library/system {noun} omitted ...")
                continue

            header = f"### Frame #{entry.index}: `{entry.signature()}`"
            loc = f"at {entry.location()}"
            block = f"{header}\n{loc}"
            if entry.source:
                block += f"\n\n```\n{entry.source.rstrip()}\n```"
            blocks.append(block)

        return "\n\n".join(blocks)
