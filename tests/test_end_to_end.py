"""End-to-end test of the ACP path with the bundled mock agent.

This spawns the real mock agent as a subprocess and drives it through the real
ACP client, proving the whole coding-agent-over-ACP loop works: the prompt is
sent, the agent reads source through the ACP filesystem callback, and a streamed
diagnosis comes back — with nothing else to install.
"""

import io
import textwrap
from pathlib import Path

from acpdbg.acp_client import ConsolePrinter, ask
from acpdbg.config import Config
from acpdbg.context import Arg, CrashContext, Frame
from acpdbg.prompts import build_initial_prompt


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
