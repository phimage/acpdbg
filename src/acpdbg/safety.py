"""A conservative allow/deny filter for debugger commands.

acpdbg auto-approves tool calls by default, so an agent could otherwise resume
or mutate the very process you are trying to inspect. This filter blocks the
dangerous families of LLDB commands — anything that resumes execution, controls
the process lifecycle, writes memory/registers/settings, or reloads scripts —
while leaving read-only inspection wide open.

It is a safety net, not a sandbox. Expression evaluation (``p``, ``expr``) is
allowed because it is essential for debugging, but note that evaluating an
expression that *calls a function* can still have side effects. Pass
``unsafe=True`` (``--unsafe`` / ``ACPDBG_UNSAFE=1``) to disable the filter.
"""

from __future__ import annotations

import re

# First token (or first two tokens) of commands we refuse in safe mode.
_DENIED_HEADS = {
    # Resume / single-step execution.
    "r", "run", "c", "cont", "continue",
    "s", "step", "si", "stepi", "so", "n", "next", "ni", "nexti",
    "finish", "fin", "j", "jump", "call",
    # Process lifecycle / attach / detach / kill.
    "process", "attach", "detach", "kill", "file", "gdb-remote", "target",
    # Anything that runs code or rewrites the environment.
    "script", "command", "platform", "plugin", "env",
}

# Two-word forms that are safe even though their first word is denied above.
_ALLOWED_PAIRS = {
    ("process", "status"),
    ("process", "info"),
    ("target", "list"),
    ("target", "modules"),
    ("target", "variable"),
    ("target", "select"),
}

# Two-word forms that are denied even though their first word looks harmless.
_DENIED_PAIRS = {
    ("thread", "step-in"), ("thread", "step-out"), ("thread", "step-over"),
    ("thread", "step-inst"), ("thread", "step-scripted"),
    ("thread", "continue"), ("thread", "until"), ("thread", "jump"),
    ("thread", "return"),
    ("memory", "write"),
    ("register", "write"),
    ("settings", "set"), ("settings", "remove"), ("settings", "clear"),
    ("settings", "write"), ("settings", "append"), ("settings", "insert-before"),
    ("settings", "insert-after"), ("settings", "replace"),
    ("breakpoint", "set"), ("breakpoint", "delete"), ("breakpoint", "command"),
    ("watchpoint", "set"), ("watchpoint", "delete"), ("watchpoint", "command"),
}

# Assignment inside an expression command, e.g. `p x = 5` or `expr obj->n = 0`.
_ASSIGNMENT = re.compile(r"(?<![=!<>+\-*/%&|^])=(?!=)")
_EXPR_HEADS = {"p", "print", "po", "expr", "expression", "v", "var", "variable"}


def command_is_safe(command: str, unsafe: bool = False) -> bool:
    if unsafe:
        return True

    tokens = command.strip().split()
    if not tokens:
        return False

    head = tokens[0].lower()
    pair = (head, tokens[1].lower()) if len(tokens) > 1 else None

    if pair in _DENIED_PAIRS:
        return False
    if pair in _ALLOWED_PAIRS:
        return True
    if head in _DENIED_HEADS:
        return False
    if head in _EXPR_HEADS and _ASSIGNMENT.search(command):
        # Reject expressions that assign; reading values is fine.
        return False
    return True
