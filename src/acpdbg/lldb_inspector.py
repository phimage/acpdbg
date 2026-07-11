"""LLDB backend: turn a live ``SBDebugger`` into a :class:`CrashContext`.

When a native program is stopped, walk the failing thread, capture each
source-bearing frame with its arguments and a slice of surrounding source, and
package it as backend-neutral data that the agent layer can render into a
prompt. It also runs arbitrary LLDB commands on behalf of the agent (via the MCP
bridge).

``lldb`` is imported lazily so importing this module never requires being inside
a debugger.
"""

from __future__ import annotations

import os
from typing import Optional

from .context import Arg, CrashContext, Frame, SkippedFrames

SOURCE_CONTEXT_LINES = 5
MAX_SOURCE_FRAMES = 12


class LLDBInspector:
    def __init__(self, debugger) -> None:
        self._debugger = debugger

    # --- command execution (used by the MCP bridge) -----------------------
    def run_command(self, command: str) -> str:
        import lldb

        interpreter = self._debugger.GetCommandInterpreter()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(command, result)
        if result.Succeeded():
            return result.GetOutput() or ""
        return result.GetError() or ""

    # --- execution control (used by the MCP control tools) ----------------
    def control(self, action: str) -> str:
        """Drive execution via the SB API and describe where we end up.

        The command interpreter's step commands don't advance reliably when
        invoked in this embedded context, so we use the typed SB API and force
        synchronous mode so each call returns only once the process stops again.
        """
        import lldb

        target = self._debugger.GetSelectedTarget()
        process = target.GetProcess() if target else None
        if not process or not process.IsValid():
            return "No running process."
        thread = process.GetSelectedThread()
        if not thread or not thread.IsValid():
            return "No selected thread."

        was_async = self._debugger.GetAsync()
        self._debugger.SetAsync(False)
        try:
            if action == "step_over":
                thread.StepOver()
            elif action == "step_into":
                thread.StepInto()
            elif action == "step_out":
                thread.StepOut()
            elif action == "continue":
                process.Continue()
            else:
                return f"Unknown control action: {action}"
        finally:
            self._debugger.SetAsync(was_async)

        return self._describe_stop(process)

    def _describe_stop(self, process) -> str:
        import lldb

        state = process.GetState()
        if state == lldb.eStateExited:
            return f"Process exited with status {process.GetExitStatus()}."
        if state != lldb.eStateStopped:
            return f"Process state: {self._debugger.StateAsCString(state)}."

        thread = process.GetSelectedThread()
        stop = thread.GetStopDescription(256) or "stopped"
        frame = thread.GetSelectedFrame()
        function = frame.GetFunctionName() or "<unknown>"
        line_entry = frame.GetLineEntry()
        file_spec = line_entry.GetFileSpec()
        name = file_spec.GetFilename() if file_spec else None
        location = f"{function} at {name}:{line_entry.GetLine()}" if name else function

        lines = [f"stopped: {stop}", f"  now at {location}"]
        path = file_spec.fullpath if file_spec else None
        if path and line_entry.GetLine() > 0:
            snippet = self._source_snippet(path, line_entry.GetLine())
            if snippet:
                lines.append(snippet)
        return "\n".join(lines)

    # --- state checks -----------------------------------------------------
    def has_target(self) -> bool:
        target = self._debugger.GetSelectedTarget()
        return bool(target and target.IsValid())

    def stopped_thread(self):
        import lldb

        target = self._debugger.GetSelectedTarget()
        if not target:
            return None
        process = target.GetProcess()
        if not process or not process.IsValid():
            return None
        for thread in process:
            reason = thread.GetStopReason()
            if reason not in (lldb.eStopReasonNone, lldb.eStopReasonInvalid):
                return thread
        return None

    def is_reportable_stop(self) -> bool:
        """True only for genuine faults, not benign launch/continue stops.

        When a stop-hook drives the automatic CLI flow, lldb also fires it on the
        transient SIGSTOP/SIGCONT stops around process launch; those carry no
        useful crash context and should be ignored.
        """
        thread = self.stopped_thread()
        if thread is None:
            return False
        description = thread.GetStopDescription(256) or ""
        if not description.strip():
            return False
        return not any(sig in description for sig in ("SIGSTOP", "SIGCONT"))

    def stop_generation(self) -> "tuple | None":
        """A marker that changes whenever the program stops anew.

        Used by the persistent-session flow to notice that live state moved on
        (a re-run, a next breakpoint) and resend fresh context to the agent.
        """
        target = self._debugger.GetSelectedTarget()
        if not target:
            return None
        process = target.GetProcess()
        if not process or not process.IsValid():
            return None
        return (process.GetUniqueID(), process.GetStopID())

    def is_debug_build(self) -> bool:
        target = self._debugger.GetSelectedTarget()
        if not target:
            return False
        for module in target.module_iter():
            for cu in module.compile_unit_iter():
                for line_entry in cu:
                    if line_entry.GetLine() > 0:
                        return True
        return False

    # --- context capture --------------------------------------------------
    def crash_context(self) -> CrashContext:
        thread = self.stopped_thread()
        return CrashContext(
            stop_reason=self._stop_reason(thread),
            command_line=self._command_line(),
            frames=self._frames(thread),
        )

    def _stop_reason(self, thread) -> Optional[str]:
        if not thread:
            return None
        description = thread.GetStopDescription(1024)
        return description or None

    def _command_line(self) -> Optional[str]:
        target = self._debugger.GetSelectedTarget()
        if not target:
            return None
        executable = target.GetExecutable()
        path = os.path.join(executable.GetDirectory() or "", executable.GetFilename() or "")
        if not path:
            return None
        if path.startswith(os.getcwd()):
            path = os.path.relpath(path)
        launch = target.GetLaunchInfo()
        args = [launch.GetArgumentAtIndex(i) for i in range(launch.GetNumArguments())]
        return " ".join([path, *args]).strip()

    def _frames(self, thread) -> list:
        if not thread:
            return []

        frames: list = []
        skipped = 0
        source_frames = 0

        for index, frame in enumerate(thread):
            file_path, lineno = self._frame_location(frame)
            if not file_path or not os.path.exists(file_path):
                skipped += 1
                continue

            if skipped:
                frames.append(SkippedFrames(skipped))
                skipped = 0

            frames.append(
                Frame(
                    index=index,
                    function=self._frame_name(frame),
                    args=self._frame_args(frame),
                    file=self._relativize(file_path),
                    line=lineno,
                    source=self._source_snippet(file_path, lineno),
                )
            )
            source_frames += 1
            if source_frames >= MAX_SOURCE_FRAMES:
                break

        if skipped:
            frames.append(SkippedFrames(skipped))
        return frames

    def _frame_location(self, frame):
        line_entry = frame.GetLineEntry()
        file_spec = line_entry.GetFileSpec()
        path = file_spec.fullpath if file_spec else None
        return path, line_entry.GetLine()

    def _frame_name(self, frame) -> str:
        name = frame.GetDisplayFunctionName() or frame.GetFunctionName() or "<unknown>"
        return name.split("(")[0]

    def _frame_args(self, frame) -> list[Arg]:
        args: list[Arg] = []
        function = frame.GetFunction()
        if not function:
            return args
        arg_types = function.GetType().GetFunctionArgumentTypes()
        for j in range(arg_types.GetSize()):
            name = function.GetArgumentName(j) or f"arg{j}"
            variable = frame.FindVariable(name)
            if variable and variable.IsValid():
                args.append(
                    Arg(
                        type=variable.GetTypeName() or "?",
                        name=variable.GetName() or name,
                        value=variable.GetValue(),
                    )
                )
            else:
                args.append(Arg(type="?", name=name, value=None))
        return args

    def _relativize(self, path: str) -> str:
        if path.startswith(os.getcwd()):
            return os.path.relpath(path)
        return path

    def _source_snippet(self, path: str, lineno: int) -> Optional[str]:
        if lineno <= 0:
            return None
        try:
            with open(path, "r", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            return None
        start = max(lineno - SOURCE_CONTEXT_LINES, 1)
        end = min(lineno + SOURCE_CONTEXT_LINES, len(lines))
        width = len(str(end))
        out = []
        for n in range(start, end + 1):
            marker = "->" if n == lineno else "  "
            out.append(f"{marker} {str(n).rjust(width)} | {lines[n - 1].rstrip()}")
        return "\n".join(out)
