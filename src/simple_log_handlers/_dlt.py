from __future__ import annotations

import json
import logging
import os


class DltJsonFilter(logging.Filter):
    """Transform dlt JSON-formatted log records into plain structured records.

    When dlt is configured with ``DLT_LOG_FORMAT=json`` it emits log records
    whose message is a JSON object. This filter:

    - replaces the raw JSON envelope with the human-readable ``msg`` field
    - injects ``dlt_module`` (e.g. ``"client"``, ``"normalize"``) and
      ``dlt_version`` as record attributes so downstream handlers
      (DatadogHandler, OtelHandler) forward them as structured extras
    - optionally drops records below a per-module log level threshold
    - optionally rewrites ``record.name`` to ``"dlt.<module>"`` so that
      handlers with ``include_logger_name=True`` produce an ``executablekey``
      of the form ``"pipeline::dlt.client"``

    Add to the ``dlt`` logger so every attached handler benefits::

        logging.getLogger("dlt").addFilter(DltJsonFilter())

    Silence noisy subsystems with per-module level overrides::

        DltJsonFilter(module_levels={"client": logging.WARNING})

    Compose with ``include_logger_name=True`` on the handler for a rich key::

        logging.getLogger("dlt").addFilter(
            DltJsonFilter(include_module_in_logger_name=True)
        )
        handler = dd_handler(api_key=..., include_logger_name=True)
        # → executablekey = "my_pipeline::dlt.client"

    Or call :func:`install_dlt_json_filter` which handles env-var detection
    and idempotency automatically.
    """

    def __init__(
        self,
        module_levels: dict[str, int] | None = None,
        include_module_in_logger_name: bool = False,
        name: str = "",
    ) -> None:
        super().__init__(name)
        self._module_levels: dict[str, int] = dict(module_levels or {})
        self._include_module_in_logger_name = include_module_in_logger_name

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            data = json.loads(record.getMessage())
        except (json.JSONDecodeError, TypeError, ValueError):
            return True

        if not isinstance(data, dict) or "msg" not in data:
            return True

        module = data.get("module")
        module = module if isinstance(module, str) else None

        # Per-module level gate — drop before transforming to avoid wasted work.
        if module and module in self._module_levels:
            if record.levelno < self._module_levels[module]:
                return False

        record.msg = data["msg"]
        record.args = None

        if module:
            record.__dict__["dlt_module"] = module
            if self._include_module_in_logger_name:
                record.name = f"dlt.{module}"

        version = data.get("version")
        if isinstance(version, dict):
            dlt_version = version.get("dlt_version")
            if isinstance(dlt_version, str) and dlt_version:
                record.__dict__["dlt_version"] = dlt_version

        return True


def install_dlt_json_filter(
    module_levels: dict[str, int] | None = None,
    include_module_in_logger_name: bool = False,
) -> DltJsonFilter | None:
    """Install a :class:`DltJsonFilter` on the ``dlt`` logger when dlt logs JSON.

    Reads ``DLT_LOG_FORMAT`` from the environment; returns ``None`` and does
    nothing if the value is not ``"json"`` (case-insensitive).

    Idempotent: if a :class:`DltJsonFilter` is already present on the ``dlt``
    logger it is returned as-is without adding a duplicate.

    Called automatically by :func:`~simple_log_handlers.dd_handler` and
    :func:`~simple_log_handlers.otel_handler` (with default arguments).
    Call it explicitly *before* those functions to pass options::

        install_dlt_json_filter(
            module_levels={"client": logging.WARNING},
            include_module_in_logger_name=True,
        )
        handler = dd_handler(api_key=..., include_logger_name=True)
    """
    if os.environ.get("DLT_LOG_FORMAT", "").strip().lower() != "json":
        return None

    dlt_logger = logging.getLogger("dlt")
    for f in dlt_logger.filters:
        if isinstance(f, DltJsonFilter):
            return f

    flt = DltJsonFilter(
        module_levels=module_levels,
        include_module_in_logger_name=include_module_in_logger_name,
    )
    dlt_logger.addFilter(flt)
    return flt
