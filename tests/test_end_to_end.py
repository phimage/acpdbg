"""End-to-end test of the ACP path with the bundled mock agent.

This spawns the real mock agent as a subprocess and drives it through the real
ACP client, proving the whole coding-agent-over-ACP loop works: the prompt is
sent, the agent reads source through the ACP filesystem callback, and a streamed
diagnosis comes back — with nothing else to install.
"""

import io
import os
import subprocess
import sys
import textwrap
from pathlib import Path

from acpdbg.acp_client import ConsolePrinter, ask
from acpdbg.config import SESSION_PERSISTENT_ENV, SESSION_TURN_END, Config
from acpdbg.context import Arg, CrashContext, Frame
from acpdbg.prompts import build_followup_prompt, build_initial_prompt


def test_mock_agent_round_trip(tmp_path: Path):
    source = tmp_path / "crash.c"
    source.write_text(
        textwrap.dedent(
            """\
            static size_t describe(const char *label, const char *s) {
                return strlen(s); /* boom */
            }
            """
        )
    )

    ctx = CrashContext(
        stop_reason="EXC_BAD_ACCESS (code=1, address=0x0)",
        command_line="./crash",
        frames=[
            Frame(
                index=0,
                function="describe",
                args=[Arg("const char *", "s", "0x0")],
                file="crash.c",
                line=2,
                source="-> 2 |     return strlen(s);",
            )
        ],
    )
    prompt = build_initial_prompt(ctx, "why did this crash?")

    buffer = io.StringIO()
    config = Config(agent="mock", use_mcp=False, prompt_timeout=60)

    ask(config, prompt, executor=None, cwd=str(tmp_path), printer=ConsolePrinter(buffer))

    output = buffer.getvalue()
    assert "mock agent" in output.lower()
    # Proof the agent read the real file over ACP (line 2 content came back).
    assert "strlen(s)" in output
    # It recognised the bad-memory signal.
    assert "memory access" in output.lower()


def test_persistent_helper_serves_many_turns(tmp_path: Path):
    """The persistent helper keeps one agent conversation across PROMPT/RESET
    commands, marking each with the end-of-turn sentinel, and exits on EOF."""
    env = dict(os.environ)
    env.update(
        {
            SESSION_PERSISTENT_ENV: "1",
            "ACPDBG_AGENT": "mock",
            "ACPDBG_MCP": "0",
            "ACPDBG_TIMEOUT": "60",
            "ACPDBG_COLOR": "0",
        }
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "acpdbg.session"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=env,
        cwd=str(tmp_path),
    )

    def read_turn() -> tuple:
        lines = []
        while True:
            line = proc.stdout.readline()
            assert line, "helper exited before the end-of-turn sentinel"
            if line.startswith(SESSION_TURN_END):
                return line[len(SESSION_TURN_END):].strip(), "".join(lines)
            lines.append(line)

    def send(command: str) -> None:
        proc.stdin.write(command + "\n")
        proc.stdin.flush()

    try:
        status, _ = read_turn()  # agent spawned + session opened
        assert status == "ready"

        first = tmp_path / "p1.md"
        first.write_text("A program stopped. Why?")
        send(f"PROMPT {first}")
        status, output = read_turn()
        assert status == "ok"
        assert "mock agent" in output.lower()

        second = tmp_path / "p2.md"
        second.write_text(build_followup_prompt("and how would you fix it?"))
        send(f"PROMPT {second}")
        status, output = read_turn()
        assert status == "ok"
        assert "mock agent" in output.lower()

        send("RESET")  # fresh conversation on the same agent process
        status, _ = read_turn()
        assert status == "ok"

        proc.stdin.close()  # EOF: shut down cleanly
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
