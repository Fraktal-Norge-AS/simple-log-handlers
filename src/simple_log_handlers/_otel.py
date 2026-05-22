from __future__ import annotations

import atexit
import gzip
import json
import logging
import os
import queue
import random
import re
import socket
import sys
import threading
import time
import traceback
import warnings
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any

import httpx

from simple_log_handlers._context import _executable_key_var
from simple_log_handlers._dlt import install_dlt_json_filter as _install_dlt_json_filter

try:
    _version = _pkg_version("simple-log-handlers")
except PackageNotFoundError:
    _version = "0.0.0"

# Python logging level → OTLP SeverityNumber (OTEL spec §data-model-log).
# These are stable integer constants from the specification; no SDK import needed.
_SEVERITY_MAP: dict[int, int] = {
    logging.DEBUG: 5,      # SeverityNumber.DEBUG
    logging.INFO: 9,       # SeverityNumber.INFO
    logging.WARNING: 13,   # SeverityNumber.WARN
    logging.ERROR: 17,     # SeverityNumber.ERROR
    logging.CRITICAL: 21,  # SeverityNumber.FATAL
}

# Ordered list of (level_threshold, severity_number) used to find the nearest
# mapped severity for non-standard logging levels (e.g. logging.addLevelName).
_SEVERITY_THRESHOLDS: tuple[tuple[int, int], ...] = (
    (logging.CRITICAL, 21),
    (logging.ERROR,    17),
    (logging.WARNING,  13),
    (logging.INFO,      9),
    (logging.DEBUG,     5),
)


def _severity_number(levelno: int) -> int:
    """Map a Python log level to the nearest OTLP SeverityNumber."""
    if levelno in _SEVERITY_MAP:
        return _SEVERITY_MAP[levelno]
    for threshold, sev in _SEVERITY_THRESHOLDS:
        if levelno >= threshold:
            return sev
    return 5  # anything below DEBUG → DEBUG

# Computed once at import time against the running Python version.
_STANDARD_RECORD_ATTRS: frozenset[str] = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime"}

# Keys that the handler owns in log record attributes. extra={} fields whose
# names collide are silently dropped so exception tracking cannot be spoofed.
_RESERVED_ATTRIBUTE_KEYS: frozenset[str] = frozenset({
    "exception.type",
    "exception.message",
    "exception.stacktrace",
    "executable_key",
})


class _Sentinel:
    pass


_STOP = _Sentinel()


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[mGKHF]")
_ENDPOINT_RE = re.compile(r"^https?://[^\s/]+", re.IGNORECASE)

_LOCAL_HOSTNAME_SET: frozenset[str] = frozenset({
    "localhost",
    "localhost.localdomain",
    "127.0.0.1",
    "::1",
})


def _is_local_hostname(hostname: str) -> bool:
    """Return True if hostname is unambiguously a local/loopback machine.

    Only matches exact values: 'localhost', 'localhost.localdomain', and
    loopback IP addresses. Deliberately does NOT match '.local' suffixes —
    those are used for LAN/mDNS names that can also appear in production.
    """
    return hostname.lower() in _LOCAL_HOSTNAME_SET


def _resolve_hostname(hostname: str | None) -> str:
    if hostname:
        return hostname
    # LOGGING_HOSTNAME is the shared fallback recognised by all handlers.
    if value := os.environ.get("LOGGING_HOSTNAME", "").strip():
        return value
    try:
        return socket.gethostname()
    except Exception:
        return ""


def _parse_headers(header_str: str) -> dict[str, str]:
    # OTEL spec: comma-separated "key=value" pairs. We partition on the first
    # "=" only so base64-padded Authorization values (containing "=") work.
    result: dict[str, str] = {}
    for item in header_str.split(","):
        k, found, v = item.partition("=")
        k = k.strip()
        if k and found:
            result[k] = v.strip()
    return result


def _otlp_value(v: object) -> dict[str, Any]:
    # Convert a Python value to an OTLP AnyValue dict.
    # bool must be checked before int because bool is a subclass of int.
    # int64 is encoded as a JSON string per the proto3 JSON mapping spec.
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def _otlp_attr(key: str, value: object) -> dict[str, Any]:
    return {"key": key, "value": _otlp_value(value)}


def _build_resource_attrs(
    service: str | None,
    env: str | None,
    version: str | None,
    hostname: str,
    container_key: str | None,
    customer_key: str | None,
    database: str | None,
    database_type: str | None,
    table: str | None,
    attributes: dict[str, str] | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    result: list[dict[str, Any]] = []

    for key, value in (
        ("service.name", service),
        ("service.version", version),
        ("deployment.environment", env),
        ("host.name", hostname or None),
    ):
        if value:
            result.append(_otlp_attr(key, value))

    # Fraktal identity fields — use snake_case to follow OTEL attribute
    # naming conventions (contrast: camelCase in the Datadog handler).
    # executable_key is intentionally excluded here — it is invocation-scoped
    # and is written as a per-record attribute in emit() instead.
    for attr_key, value in (
        ("container_key",  container_key),
        ("customer_key",   customer_key),
        ("database",       database),
        ("database_type",  database_type),
        ("table",          table),
    ):
        if value:
            result.append(_otlp_attr(attr_key, value))

    collisions: list[str] = []
    if attributes:
        owned = {a["key"] for a in result}
        for k, v in attributes.items():
            stripped = v.strip()
            if not stripped:
                continue
            if k in owned:
                collisions.append(k)
            else:
                result.append(_otlp_attr(k, stripped))

    return result, collisions


class _OtelHandler(logging.Handler):
    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str],
        resource_attrs: list[dict[str, Any]],
        service: str | None,
        compress: bool,
        timeout: float,
        queue_size: int,
        batch_size: int,
        batch_timeout: float,
        max_batch_bytes: int,
        max_retries: int,
        shutdown_timeout: float,
        suppressed: bool = False,
        executable_key: str | None = None,
        include_logger_name: bool = False,
    ) -> None:
        super().__init__()
        self._suppressed = suppressed
        self._executable_key = executable_key
        self._include_logger_name = include_logger_name
        self._url = url
        self._headers = headers
        self._service = service
        self._compress = compress
        self._timeout_val = timeout
        self._batch_size = batch_size
        self._batch_timeout = batch_timeout
        self._max_batch_bytes = max_batch_bytes
        self._max_retries = max_retries
        self._shutdown_timeout = shutdown_timeout
        self._queue_size = queue_size
        self._overflow_warned = False
        self._closed = False
        self._stop_event = threading.Event()

        # Pre-serialise the resource and scope fragments once at construction.
        # _send_batch() assembles the full OTLP envelope by byte-concatenation —
        # the same "avoid decode/re-encode" optimisation as _DatadogHandler.
        resource_json = json.dumps({"attributes": resource_attrs}).encode()
        scope_json = json.dumps({"name": "simple-log-handlers", "version": _version}).encode()
        self._envelope_prefix: bytes = (
            b'{"resourceLogs":[{"resource":'
            + resource_json
            + b',"scopeLogs":[{"scope":'
            + scope_json
            + b',"logRecords":['
        )
        self._envelope_suffix: bytes = b"]}]}]}"

        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": f"simple-log-handlers/{_version}"},
        )
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._start_worker()

        # Fork-safety: same three-hook pattern as _DatadogHandler.
        # os.register_at_fork is POSIX-only (not available on Windows).
        if hasattr(os, "register_at_fork"):
            getattr(os, "register_at_fork")(
                before=self._before_fork,
                after_in_parent=self._after_fork_parent,
                after_in_child=self._after_fork_child,
            )

    def __repr__(self) -> str:
        auth = self._headers.get("Authorization", "")
        auth_hint = f"{auth[:8]}***" if auth else "(none)"
        return (
            f"<OtelHandler url={self._url!r} "
            f"service={self._service!r} "
            f"auth={auth_hint} "
            f"level={logging.getLevelName(self.level)}>"
        )

    # ------------------------------------------------------------------
    # Worker thread lifecycle
    # ------------------------------------------------------------------

    def _start_worker(self) -> None:
        self._worker_thread = threading.Thread(
            target=self._run_worker,
            name="simple-log-handlers-otel-worker",
            daemon=False,
        )
        self._worker_thread.start()
        atexit.register(self._atexit_close)

    def _atexit_close(self) -> None:
        if not self._closed:
            self.close()

    # ------------------------------------------------------------------
    # emit() — called by the logging framework on the caller's thread
    # ------------------------------------------------------------------

    def emit(self, record: logging.LogRecord) -> None:
        if self._suppressed:
            return
        # Build and serialise the OTLP log record entirely on the caller's
        # thread. Same rationale as _DatadogHandler: arg mutation safety,
        # captureWarnings() reentrancy, and handleError() availability.
        try:
            log_record: dict[str, Any] = {
                "timeUnixNano": str(round(record.created * 1_000_000_000)),
                "observedTimeUnixNano": str(time.time_ns()),
                "severityNumber": _severity_number(record.levelno),
                "severityText": record.levelname,
                "body": {"stringValue": _ANSI_RE.sub("", self.format(record))},
            }

            attributes: list[dict[str, Any]] = []

            # Exception tracking — OTEL semantic conventions for exceptions.
            # Extracted from record.exc_info directly so the user's formatter
            # cannot affect these structured fields (same reasoning as the
            # Datadog error.* fields).
            if record.exc_info and record.exc_info[0] is not None:
                exc_type, exc_value, exc_tb = record.exc_info
                attributes.append(_otlp_attr("exception.type", exc_type.__qualname__))
                attributes.append(_otlp_attr("exception.message", str(exc_value)))
                attributes.append(_otlp_attr("exception.stacktrace", "".join(
                    traceback.format_exception(exc_type, exc_value, exc_tb)
                )))
            elif record.stack_info:
                attributes.append(_otlp_attr("exception.stacktrace", record.stack_info))

            # Forward non-standard LogRecord fields set via extra={}.
            # Keys colliding with _RESERVED_ATTRIBUTE_KEYS are dropped.
            for k, v in vars(record).items():
                if (
                    k not in _STANDARD_RECORD_ATTRS
                    and k not in _RESERVED_ATTRIBUTE_KEYS
                    and not k.startswith("_")
                ):
                    attributes.append(_otlp_attr(k, v))

            # Resolve executable_key: context var overrides static key.
            # Logger name appended only when include_logger_name=True.
            ctx_key = _executable_key_var.get()
            exec_value: str | None = None
            if self._include_logger_name:
                log_name = record.name if record.name not in ("root", "__main__") else None
                if ctx_key and log_name:
                    exec_value = f"{ctx_key}::{log_name}"
                elif ctx_key:
                    exec_value = ctx_key
                elif log_name and not self._executable_key:
                    exec_value = log_name
            else:
                if ctx_key:
                    exec_value = ctx_key
            if exec_value is None and self._executable_key:
                exec_value = self._executable_key
            if exec_value:
                attributes.append(_otlp_attr("executable_key", exec_value))

            if attributes:
                log_record["attributes"] = attributes

            item = json.dumps(log_record, default=str).encode()
        except Exception:
            self.handleError(record)
            return

        try:
            self._queue.put_nowait(item)
        except queue.Full:
            if not self._overflow_warned:
                self._overflow_warned = True
                sys.stderr.write(
                    f"simple-log-handlers: otel delivery queue is full ({self._queue_size} slots); "
                    "log records are being dropped. Increase queue_size or reduce log volume.\n"
                )

    # ------------------------------------------------------------------
    # Worker thread — drains queue, batches, sends
    # ------------------------------------------------------------------

    def _run_worker(self) -> None:
        while True:
            batch: list[bytes] = []
            batch_bytes = 0
            # Deadline set once per batch window — same fixed-deadline design
            # as _DatadogHandler to prevent a steady log stream from holding
            # a batch open indefinitely.
            deadline = time.monotonic() + self._batch_timeout

            while len(batch) < self._batch_size and batch_bytes < self._max_batch_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    item = self._queue.get(timeout=max(0.001, remaining))
                except queue.Empty:
                    break

                if isinstance(item, _Sentinel):
                    if batch:
                        self._send_batch(batch)
                    return

                if isinstance(item, threading.Event):
                    if batch:
                        self._send_batch(batch)
                        batch = []
                        batch_bytes = 0
                    item.set()
                    deadline = time.monotonic() + self._batch_timeout
                    continue

                batch.append(item)
                batch_bytes += len(item)

            if batch:
                self._send_batch(batch)

    def _send_batch(self, batch: list[bytes]) -> None:
        # Assemble the OTLP envelope by byte-concatenation. The pre-serialised
        # prefix (resource + scope) is joined with the N log record bytes and
        # the closing suffix — no decode/re-encode round-trip required.
        body = self._envelope_prefix + b",".join(batch) + self._envelope_suffix

        req_headers: dict[str, str] = {
            **self._headers,
            "Content-Type": "application/json",
        }
        if self._compress:
            body = gzip.compress(body)
            req_headers["Content-Encoding"] = "gzip"

        for attempt in range(self._max_retries + 1):
            try:
                self._client.post(self._url, content=body, headers=req_headers).raise_for_status()
                return
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                    if self._stop_event.wait(self._backoff(attempt, exc.response.headers.get("Retry-After"))):
                        return
                    continue
                sys.stderr.write(
                    f"simple-log-handlers: failed to deliver {len(batch)} log(s): HTTP {status}\n"
                )
                return
            except Exception as exc:
                if attempt < self._max_retries:
                    if self._stop_event.wait(self._backoff(attempt)):
                        return
                    continue
                sys.stderr.write(
                    f"simple-log-handlers: failed to deliver {len(batch)} log(s): {exc}\n"
                )
                return

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        base = min(30.0, 0.5 * (2 ** attempt))
        return base + random.uniform(0, base * 0.1)

    # ------------------------------------------------------------------
    # flush() and close()
    # ------------------------------------------------------------------

    def flush(self) -> None:
        if self._closed:
            return
        event = threading.Event()
        try:
            self._queue.put(event, timeout=self._shutdown_timeout)
        except queue.Full:
            return
        event.wait(timeout=self._shutdown_timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        self.flush()
        try:
            self._queue.put(_STOP, timeout=self._shutdown_timeout)
        except queue.Full:
            pass
        self._worker_thread.join(timeout=self._shutdown_timeout)
        self._client.close()
        super().close()

    # ------------------------------------------------------------------
    # Fork hooks (POSIX only)
    # ------------------------------------------------------------------

    def _before_fork(self) -> None:
        self._queue.mutex.acquire()  # type: ignore[attr-defined]

    def _after_fork_parent(self) -> None:
        self._queue.mutex.release()  # type: ignore[attr-defined]

    def _after_fork_child(self) -> None:
        self._queue.mutex.release()  # type: ignore[attr-defined]
        self._queue = queue.Queue(maxsize=self._queue_size)
        self._overflow_warned = False
        self._closed = False
        self._stop_event = threading.Event()
        self._client = httpx.Client(
            timeout=self._timeout_val,
            headers={"User-Agent": f"simple-log-handlers/{_version}"},
        )
        self._start_worker()


def otel_handler(
    endpoint: str | None = None,
    *,
    service: str | None = None,
    env: str | None = None,
    version: str | None = None,
    hostname: str | None = None,
    headers: dict[str, str] | None = None,
    compress: bool | None = None,
    timeout: float | None = None,
    container_key: str | None = None,
    customer_key: str | None = None,
    database: str | None = None,
    database_type: str | None = None,
    table: str | None = None,
    executable_key: str | None = None,
    attributes: dict[str, str] | None = None,
    queue_size: int = 10_000,
    batch_size: int = 100,
    batch_timeout: float = 0.5,
    max_batch_bytes: int = 4 * 1024 * 1024,
    max_retries: int = 3,
    shutdown_timeout: float = 5.0,
    send_localhost_logs: bool = False,
    level: int | None = None,
    include_logger_name: bool = False,
) -> _OtelHandler:
    # Endpoint: logs-specific env var takes precedence over the generic one,
    # matching the OTEL SDK's own resolution order.
    resolved_endpoint = (
        endpoint
        or os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or ""
    ).strip()
    endpoint_invalid = False
    if not resolved_endpoint:
        endpoint_invalid = True
        warnings.warn(
            "otel_handler: endpoint is empty — logs will not be delivered",
            UserWarning,
            stacklevel=2,
        )
    elif not _ENDPOINT_RE.match(resolved_endpoint):
        endpoint_invalid = True
        warnings.warn(
            f"otel_handler: endpoint {resolved_endpoint!r} does not look like a valid "
            "http(s):// URL — logs will not be delivered",
            UserWarning,
            stacklevel=2,
        )
    else:
        # Loose suffix check: any path ending in "/v1/logs" is left as-is,
        # everything else gets the standard OTLP logs path appended.
        if not resolved_endpoint.rstrip("/").endswith("/v1/logs"):
            resolved_endpoint = resolved_endpoint.rstrip("/") + "/v1/logs"

    # Headers: env var provides defaults, explicit dict wins on collision.
    resolved_headers: dict[str, str] = {}
    env_header_str = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS", "").strip()
    if env_header_str:
        resolved_headers.update(_parse_headers(env_header_str))
    if headers:
        resolved_headers.update(headers)

    # Compression: OTEL_EXPORTER_OTLP_COMPRESSION accepts "gzip" or "none".
    resolved_compress: bool
    if compress is not None:
        resolved_compress = compress
    else:
        env_compress = os.environ.get("OTEL_EXPORTER_OTLP_COMPRESSION", "").strip().lower()
        resolved_compress = env_compress != "none"

    # Timeout: OTEL_EXPORTER_OTLP_TIMEOUT is in milliseconds per the spec.
    resolved_timeout: float
    if timeout is not None:
        resolved_timeout = timeout
    else:
        env_timeout_ms = os.environ.get("OTEL_EXPORTER_OTLP_TIMEOUT", "").strip()
        if env_timeout_ms:
            try:
                resolved_timeout = int(env_timeout_ms) / 1000.0
            except ValueError:
                resolved_timeout = 10.0
        else:
            resolved_timeout = 10.0

    # Log level: explicit arg > OTEL_LOG_LEVEL > WARNING.
    resolved_level = logging.WARNING
    if level is not None:
        resolved_level = level
    else:
        env_level = os.environ.get("OTEL_LOG_LEVEL", "").strip().upper()
        if env_level and hasattr(logging, env_level):
            resolved_level = int(getattr(logging, env_level))

    # Service identity fields. OTEL_SERVICE_NAME is a real OTEL spec var;
    # env/version fall back to the shared LOGGING_* vars (no fake OTEL equivalents).
    resolved_service = (service or os.environ.get("OTEL_SERVICE_NAME", "") or os.environ.get("LOGGING_SERVICE", "")).strip() or None
    resolved_env     = (env     or os.environ.get("LOGGING_ENV",     "")).strip() or None
    resolved_version = (version or os.environ.get("LOGGING_VERSION", "")).strip() or None

    def _field(arg: str | None, env_var: str) -> str | None:
        return (arg or os.environ.get(env_var, "")).strip() or None

    resolved_hostname = _resolve_hostname(hostname)
    suppressed = endpoint_invalid or (not send_localhost_logs and _is_local_hostname(resolved_hostname))
    if suppressed and not endpoint_invalid:
        warnings.warn(
            f"otel_handler: hostname {resolved_hostname!r} is a local/loopback address; "
            "log delivery is suppressed to avoid accidentally shipping to a production "
            "backend from a development machine. Pass send_localhost_logs=True to deliver "
            "anyway, or set a real hostname via hostname= or LOGGING_HOSTNAME.",
            UserWarning,
            stacklevel=2,
        )

    resolved_executable_key = _field(executable_key, "LOGGING_EXECUTABLE_KEY")

    resource_attrs, attr_collisions = _build_resource_attrs(
        service=resolved_service,
        env=resolved_env,
        version=resolved_version,
        hostname=resolved_hostname,
        container_key=_field(container_key, "LOGGING_CONTAINER_KEY"),
        customer_key=_field(customer_key,   "LOGGING_CUSTOMER_KEY"),
        database=_field(database,           "LOGGING_DATABASE"),
        database_type=_field(database_type, "LOGGING_DATABASE_TYPE"),
        table=_field(table,                 "LOGGING_TABLE"),
        attributes=attributes,
    )
    for _k in attr_collisions:
        warnings.warn(
            f"otel_handler: attributes key {_k!r} is already set via its dedicated "
            "parameter or env var; the dedicated value takes precedence.",
            UserWarning,
            stacklevel=2,
        )

    handler = _OtelHandler(
        resolved_endpoint,
        headers=resolved_headers,
        resource_attrs=resource_attrs,
        service=resolved_service,
        compress=resolved_compress,
        timeout=resolved_timeout,
        queue_size=queue_size,
        batch_size=batch_size,
        batch_timeout=batch_timeout,
        max_batch_bytes=max_batch_bytes,
        max_retries=max_retries,
        shutdown_timeout=shutdown_timeout,
        suppressed=suppressed,
        executable_key=resolved_executable_key,
        include_logger_name=include_logger_name,
    )
    handler.setLevel(resolved_level)
    _install_dlt_json_filter()
    return handler


# Public alias — the leading underscore on _OtelHandler signals it is an
# implementation detail; users who want isinstance checks or type hints
# should import OtelHandler from simple_log_handlers.
OtelHandler = _OtelHandler
