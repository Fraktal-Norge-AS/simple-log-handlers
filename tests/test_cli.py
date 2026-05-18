import io
import logging

import pytest

from simple_log_handlers import cli_handler
from simple_log_handlers._cli import _use_colors


def test_returns_stream_handler():
    assert isinstance(cli_handler(), logging.StreamHandler)


def test_default_level_is_debug():
    assert cli_handler().level == logging.DEBUG


def test_custom_level():
    assert cli_handler(level=logging.ERROR).level == logging.ERROR


def test_output_contains_message_and_name():
    stream = io.StringIO()
    logger = logging.getLogger("test.cli")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(cli_handler(stream=stream))

    logger.info("hello there")

    out = stream.getvalue()
    assert "hello there" in out
    assert "test.cli" in out


def test_output_contains_ansi_when_forced(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    stream = io.StringIO()
    logger = logging.getLogger("test.cli.color")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(cli_handler(stream=stream))

    logger.info("hello there")

    assert "\033[" in stream.getvalue()


def test_does_not_crash_on_exception_record():
    stream = io.StringIO()
    logger = logging.getLogger("test.cli.exc")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(cli_handler(stream=stream))

    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("caught it")

    out = stream.getvalue()
    assert "caught it" in out
    assert "ValueError" in out
    assert "boom" in out


def test_stream_attribute_is_passed_stream():
    stream = io.StringIO()
    assert cli_handler(stream=stream).stream is stream


def test_no_color_env_disables_ansi(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    stream = io.StringIO()
    assert _use_colors(stream) is False


def test_force_color_env_enables_ansi(monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")
    stream = io.StringIO()
    assert _use_colors(stream) is True


def test_force_color_zero_does_not_enable_ansi(monkeypatch):
    # FORCE_COLOR=0 is the conventional opt-out even when the var is present.
    monkeypatch.setenv("FORCE_COLOR", "0")
    stream = io.StringIO()
    assert _use_colors(stream) is False


def test_formatter_does_not_mutate_record_levelname(monkeypatch):
    # _ColorFormatter must not mutate record.levelname even transiently,
    # because concurrent threads sharing a handler could race on the field.
    monkeypatch.setenv("FORCE_COLOR", "1")
    stream = io.StringIO()
    h = cli_handler(stream=stream)
    record = logging.LogRecord("t", logging.INFO, "", 0, "msg", (), None)
    original_levelname = record.levelname
    h.emit(record)
    assert record.levelname == original_levelname


def test_non_tty_stream_has_no_ansi(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    stream = io.StringIO()  # StringIO.isatty() returns False
    logger = logging.getLogger("test.cli.notty")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(cli_handler(stream=stream))
    logger.info("plain")
    assert "\033[" not in stream.getvalue()
