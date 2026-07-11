"""The shared debug log: off by default, opt-in, readable, multi-process-safe."""

import os

import pytest

from acpdbg import log
from acpdbg.config import Config


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    path = tmp_path / "acpdbg.log"
    monkeypatch.setenv("ACPDBG_LOG_FILE", str(path))
    monkeypatch.delenv("ACPDBG_DEBUG", raising=False)
    return path


def test_disabled_by_default_writes_nothing(log_file):
    log.debug("test", "should not appear")
    assert not log_file.exists()


def test_enabled_writes_tagged_timestamped_lines(log_file, monkeypatch):
    monkeypatch.setenv("ACPDBG_DEBUG", "1")
    log.debug("plugin", "hello world")
    text = log_file.read_text()
    assert "hello world" in text
    assert f"[{os.getpid():>6} plugin]" in text


def test_set_enabled_roundtrip(log_file):
    log.set_enabled(True)
    assert log.enabled()
    log.debug("test", "on now")
    log.set_enabled(False)
    log.debug("test", "off again")
    text = log_file.read_text()
    assert "on now" in text
    assert "off again" not in text


def test_tail_and_clear(log_file, monkeypatch):
    monkeypatch.setenv("ACPDBG_DEBUG", "1")
    for index in range(10):
        log.debug("test", f"line {index}")
    tail = log.tail(3)
    assert "line 9" in tail and "line 6" not in tail
    log.clear()
    assert log.tail() == ""


def test_exception_includes_traceback(log_file, monkeypatch):
    monkeypatch.setenv("ACPDBG_DEBUG", "1")
    try:
        raise ValueError("boom")
    except ValueError:
        log.exception("test", "it failed")
    text = log_file.read_text()
    assert "it failed" in text
    assert "ValueError: boom" in text


def test_stderr_sink_only_in_debug_mode(log_file, monkeypatch):
    assert log.stderr_sink("session") is None
    monkeypatch.setenv("ACPDBG_DEBUG", "1")
    sink = log.stderr_sink("session")
    assert sink is not None
    sink.write("agent stderr line\n")
    sink.close()
    assert "agent stderr line" in log_file.read_text()


def test_config_debug_env_roundtrip(monkeypatch):
    monkeypatch.setenv("ACPDBG_DEBUG", "1")
    config = Config.from_env()
    assert config.debug
    assert config.to_env()["ACPDBG_DEBUG"] == "1"
