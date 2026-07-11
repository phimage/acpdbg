"""acpdbg — debug native crashes with a coding agent over ACP.

When a native program stops (crash, signal, breakpoint), acpdbg captures an
enriched snapshot of the failure — the backtrace, each frame's arguments, and
the surrounding source — and hands it to an AI that can, in turn, run live
debugger commands to investigate further.

acpdbg speaks the Agent Client Protocol (ACP) to whatever coding agent you
already use (GitHub Copilot CLI, Gemini CLI, the Claude Code ACP adapter, or the
bundled zero-setup mock agent).
"""

__version__ = "0.1.1"
