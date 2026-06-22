from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _pkg_version

from simple_log_handlers._cli import cli_handler
from simple_log_handlers._context import with_executable_key
from simple_log_handlers._dd import DatadogHandler, dd_handler
from simple_log_handlers._dlt import DltJsonFilter, install_dlt_json_filter
from simple_log_handlers._otel import OtelHandler, otel_handler

try:
    __version__: str = _pkg_version("simple-log-handlers")
except PackageNotFoundError:
    __version__ = "unknown"

__all__ = [
    "cli_handler",
    "dd_handler",
    "DatadogHandler",
    "otel_handler",
    "OtelHandler",
    "with_executable_key",
    "DltJsonFilter",
    "install_dlt_json_filter",
    "__version__",
]
