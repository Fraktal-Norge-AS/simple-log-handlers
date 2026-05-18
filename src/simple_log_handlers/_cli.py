from __future__ import annotations

import logging
import os
import sys
from typing import IO

_RESET = "\033[0m"
_COLORS = {
    logging.DEBUG: "\033[36m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[1;31m",
}
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def _use_colors(stream: IO[str]) -> bool:
    # NO_COLOR spec (https://no-color.org): any non-empty value disables color.
    if os.environ.get("NO_COLOR"):
        return False
    # FORCE_COLOR enables color regardless of TTY, but FORCE_COLOR=0 is the
    # conventional way to explicitly opt out even when the var is present.
    force = os.environ.get("FORCE_COLOR")
    if force is not None and force != "0":
        return True
    return stream.isatty()


class _ColorFormatter(logging.Formatter):
    def formatMessage(self, record: logging.LogRecord) -> str:
        # Override formatMessage (called by format() after getMessage() and
        # exception formatting) rather than format() itself, so we never
        # mutate record.levelname. Mutating it then restoring in a finally
        # block looks safe but is not: if two threads format the same record
        # concurrently (possible with shared handlers), the assignment races.
        # Here we substitute the coloured levelname only in the format string
        # result, leaving the record completely untouched.
        color = _COLORS.get(record.levelno, _RESET)
        colored_level = f"{color}{record.levelname:<8}{_RESET}"
        # _fmt is the compiled format string on the Formatter. We apply it
        # with a temporary dict rather than modifying the record in place.
        fmt = self._fmt or _FORMAT
        return fmt % {**record.__dict__, "levelname": colored_level}


def cli_handler(
    level: int = logging.DEBUG,
    stream: IO[str] = sys.stderr,
) -> logging.StreamHandler[IO[str]]:
    handler: logging.StreamHandler[IO[str]] = logging.StreamHandler(stream)
    handler.setLevel(level)
    fmt_cls = _ColorFormatter if _use_colors(stream) else logging.Formatter
    handler.setFormatter(fmt_cls(_FORMAT, datefmt=_DATEFMT))
    return handler
