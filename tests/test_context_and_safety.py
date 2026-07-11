"""Unit tests for the debugger-agnostic pieces: context rendering, prompts,
the command safety filter, and CLI command construction."""

from acpdbg.cli import (
    _LLDBINIT_BEGIN,
    _strip_separator,
    build_lldb_command,
    build_parser,
    install_lldbinit,
    lldbinit_block,
    lldbinit_env,
)
from acpdbg.config import Config, resolve_session_command
from acpdbg.context import Arg, CrashContext, Frame, SkippedFrames
from acpdbg.prompts import build_initial_prompt
from acpdbg.safety import command_is_safe


def _sample_context() -> CrashContext:
    return CrashContext(
        stop_reason="EXC_BAD_ACCESS (code=1, address=0x0)",
        command_line="./crash",
        frames=[
            Frame(
                index=0,
                function="describe",
                args=[Arg("const char *", "s", "0x0")],
                file="samples/crash.c",
                line=14,
                source="   13 |     printf(...)\n-> 14 |     return strlen(s);",
            ),
            SkippedFrames(3),
        ],
    )


def test_render_includes_key_sections():
    text = _sample_context().render()
    assert "## Stop reason" in text
    assert "EXC_BAD_ACCESS" in text
    assert "describe(const char * s = 0x0)" in text
    assert "at samples/crash.c:14" in text
    assert "3 library/system frames omitted" in text


def test_prompt_embeds_context_and_question():
    prompt = build_initial_prompt(_sample_context(), "why did it crash?")
    assert "why did it crash?" in prompt
    assert "EXC_BAD_ACCESS" in prompt
    # The default question is used when none is given.
    assert "Why did this program stop" in build_initial_prompt(_sample_context(), "")


def test_safety_allows_inspection():
    for cmd in ["bt", "frame variable", "p argc", "register read", "x/16xb $sp",
                "image lookup -a 0x1", "thread backtrace", "process status"]:
        assert command_is_safe(cmd), cmd


def test_safety_blocks_state_changes():
    for cmd in ["continue", "run", "next", "step", "process kill", "thread return 0",
                "settings set x y", "memory write 0x1 0", "register write pc 0",
                "p x = 5", "call foo()", "breakpoint set -n main"]:
        assert not command_is_safe(cmd), cmd


def test_safety_unsafe_overrides():
    assert command_is_safe("continue", unsafe=True)


def test_cli_builds_expected_lldb_invocation():
    cmd = build_lldb_command("/usr/bin/lldb", ["./crash", "arg"])
    assert cmd[0] == "/usr/bin/lldb"
    assert "--batch" in cmd
    assert "command script import acpdbg.lldb_plugin" in cmd
    # The agent is triggered by a stop-hook the moment the process stops.
    assert "target stop-hook add --one-liner acpdbg-auto" in cmd
    assert "run" in cmd
    assert cmd[-2:] == ["./crash", "arg"]


def test_cli_strips_leading_separator():
    assert _strip_separator(["--", "./x", "a"]) == ["./x", "a"]
    assert _strip_separator(["./x"]) == ["./x"]


def test_lldbinit_block_imports_plugin():
    block = lldbinit_block()
    assert "command script import acpdbg.lldb_plugin" in block
    # It puts acpdbg on the path so lldb's Python can import it without PYTHONPATH.
    assert "sys.path" in block


def test_config_env_roundtrip(monkeypatch):
    original = Config(agent="gemini", permission_mode="prompt", use_mcp=False,
                      unsafe=True, allow_writes=True, prompt_timeout=42.0)
    for key, value in original.to_env().items():
        monkeypatch.setenv(key, value)
    restored = Config.from_env()
    assert restored.agent == "gemini"
    assert restored.permission_mode == "prompt"
    assert restored.use_mcp is False
    assert restored.unsafe is True
    assert restored.allow_writes is True
    assert restored.prompt_timeout == 42.0


def test_resolve_session_command_honours_env(monkeypatch):
    monkeypatch.setenv("ACPDBG_SESSION_CMD", "/opt/acpdbg-session --flag")
    assert resolve_session_command() == ["/opt/acpdbg-session", "--flag"]


def test_lldbinit_block_bakes_agent():
    block = lldbinit_block(agent="copilot")
    assert "os.environ.setdefault('ACPDBG_AGENT', 'copilot')" in block
    # No agent baked when none requested.
    assert "ACPDBG_AGENT" not in lldbinit_block()


def test_lldbinit_env_records_only_non_default_options():
    parser = build_parser()
    assert lldbinit_env(parser.parse_args(["--install-lldbinit"])) == {}
    env = lldbinit_env(parser.parse_args(
        ["--install-lldbinit", "--control", "--debug", "--permission", "prompt"]
    ))
    assert env == {
        "ACPDBG_CONTROL": "1",
        "ACPDBG_DEBUG": "1",
        "ACPDBG_PERMISSION": "prompt",
    }


def test_lldbinit_block_bakes_extra_env_as_setdefault():
    block = lldbinit_block(extra_env={"ACPDBG_CONTROL": "1", "ACPDBG_AUTOSERVE": "1"})
    assert "os.environ.setdefault('ACPDBG_CONTROL', '1')" in block
    assert "os.environ.setdefault('ACPDBG_AUTOSERVE', '1')" in block
    assert "ACPDBG_CONTROL" not in lldbinit_block()


def test_session_config(monkeypatch):
    assert Config().session is True                     # persistent by default
    monkeypatch.setenv("ACPDBG_SESSION", "0")
    assert Config.from_env().session is False

    parser = build_parser()
    assert "ACPDBG_SESSION" not in lldbinit_env(parser.parse_args(["--install-lldbinit"]))
    env = lldbinit_env(parser.parse_args(["--install-lldbinit", "--no-session"]))
    assert env["ACPDBG_SESSION"] == "0"


def test_autoserve_env_implies_control(monkeypatch):
    monkeypatch.setenv("ACPDBG_AUTOSERVE", "1")
    config = Config.from_env()
    assert config.autoserve is True
    assert config.allow_control is True
    # An explicit ACPDBG_CONTROL still wins.
    monkeypatch.setenv("ACPDBG_CONTROL", "0")
    config = Config.from_env()
    assert config.autoserve is True
    assert config.allow_control is False


def test_install_lldbinit_is_idempotent_and_preserves_content(tmp_path):
    init = tmp_path / ".lldbinit"
    init.write_text("settings set target.x-arch arm64\n")

    for _ in range(3):
        install_lldbinit(str(init))

    text = init.read_text()
    assert text.count(_LLDBINIT_BEGIN) == 1          # no duplication
    assert "settings set target.x-arch arm64" in text  # user content preserved
