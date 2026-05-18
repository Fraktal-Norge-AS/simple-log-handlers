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

try:
    _version = _pkg_version("simple-log-handlers")
except PackageNotFoundError:
    _version = "0.0.0"

_STATUS_MAP: dict[int, str] = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warn",
    logging.ERROR: "error",
    logging.CRITICAL: "critical",
}

# Computed once at import time against the running Python version so the
# exclusion list is always accurate without manual maintenance.
_STANDARD_RECORD_ATTRS: frozenset[str] = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", (), None)).keys()
) | {"message", "asctime"}

# Keys that the handler owns in the Datadog payload. extra={} fields whose
# names collide with these are silently dropped so that structural fields
# (level, service, error tracking) cannot be spoofed by log callers.
# IMPORTANT: error.* keys must be listed here because they are added by
# the handler (from record.exc_info) before extras are merged. If you add
# new reserved payload fields, register them here too.
_RESERVED_PAYLOAD_KEYS: frozenset[str] = frozenset({
    # Datadog standard intake fields
    "message", "status", "service", "ddsource", "hostname", "timestamp",
    "ddtags", "version",
    # Datadog error tracking fields — populated from record.exc_info
    "error.kind", "error.message", "error.stack",
    # Custom identity fields (camelCase JSON keys mirror legacy Fraktal logger)
    "containerKey", "customerKey", "database", "databaseType", "table", "executableKey",
})

# Permissive regex for Datadog site hostnames. We warn rather than raise on
# unknown values because Datadog adds regions periodically and hard-coding an
# allow-list would break users on new or custom endpoints.
_SITE_RE = re.compile(r"^[a-z0-9][a-z0-9.\-]*$")

# Hostnames that strongly indicate a local or development machine.
# socket.gethostname() returning one of these means logs should not be
# shipped to a production backend by default.
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


# Sentinel placed on the worker queue to signal a clean shutdown.
# Using a dedicated class rather than object() makes isinstance checks
# readable and avoids accidental identity collisions.
class _Sentinel:
    pass

_STOP = _Sentinel()


def _resolve_hostname(hostname: str | None) -> str:
    if hostname:
        return hostname
    # DD_HOSTNAME is Datadog-specific; LOGGING_HOSTNAME is the shared fallback
    # recognised by all simple-log-handlers handlers.
    for env_var in ("DD_HOSTNAME", "LOGGING_HOSTNAME"):
        if value := os.environ.get(env_var, "").strip():
            return value
    try:
        return socket.gethostname()
    except Exception:
        return ""


def _build_attributes(
    container_key: str | None,
    customer_key: str | None,
    database: str | None,
    database_type: str | None,
    table: str | None,
    executable_key: str | None,
    attributes: dict[str, str] | None = None,
) -> dict[str, str]:
    # Resolve from explicit arg first, then LOGGING_* env var.
    # Strip whitespace so "  " (a common misconfigured env var) is treated as
    # absent and omitted from the payload rather than sent as a blank string.
    fields: list[tuple[str | None, str, str]] = [
        (container_key, "LOGGING_CONTAINER_KEY", "containerKey"),
        (customer_key,  "LOGGING_CUSTOMER_KEY",  "customerKey"),
        (database,      "LOGGING_DATABASE",       "database"),
        (database_type, "LOGGING_DATABASE_TYPE",  "databaseType"),
        (table,         "LOGGING_TABLE",           "table"),
        (executable_key,"LOGGING_EXECUTABLE_KEY", "executableKey"),
    ]
    result = {
        json_key: value
        for arg, env_var, json_key in fields
        if (value := (arg or os.environ.get(env_var, "")).strip())
    }
    # attributes provides arbitrary additional identity fields without
    # requiring a code change. Keys in _RESERVED_PAYLOAD_KEYS are silently
    # dropped so callers cannot use this path to spoof structural fields.
    if attributes:
        for k, v in attributes.items():
            stripped = v.strip()
            if stripped and k not in _RESERVED_PAYLOAD_KEYS:
                result[k] = stripped
    return result


class _DatadogHandler(logging.Handler):
    def __init__(
        self,
        api_key: str,
        *,
        service: str | None = None,
        env: str | None = None,
        version: str | None = None,
        hostname: str = "",
        source: str = "python",
        tags: list[str] | None = None,
        site: str = "datadoghq.com",
        compress: bool = True,
        timeout: float = 5.0,
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
        suppressed: bool = False,
    ) -> None:
        super().__init__()
        self._suppressed = suppressed
        self._api_key = api_key
        self._service = service
        self._env = env
        self._version = version
        self._hostname = hostname
        self._source = source
        self._tags = list(tags or [])
        self._url = f"https://http-intake.logs.{site}/api/v2/logs"
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

        self._attributes = _build_attributes(
            container_key, customer_key, database, database_type, table, executable_key,
            attributes,
        )
        # User-Agent identifies this library in Datadog's access logs and
        # any proxy sitting in front of the intake — useful for debugging.
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": f"simple-log-handlers/{_version}"},
        )
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_size)
        self._start_worker()

        # Fork-safety: a forked child inherits this handler's open sockets
        # and worker thread — but the thread doesn't exist in the child and
        # the sockets are invalid. We register three hooks:
        #   before       — acquire the queue's internal mutex so the child
        #                  never inherits it in a mid-operation state
        #   after_parent — release it (parent continues normally)
        #   after_child  — release, discard inherited queue, recreate fresh
        #                  client and worker thread for the child process
        # os.register_at_fork is POSIX-only (not available on Windows).
        if hasattr(os, "register_at_fork"):
            # os.register_at_fork is POSIX-only; not in pyright's type stubs.
            getattr(os, "register_at_fork")(
                before=self._before_fork,
                after_in_parent=self._after_fork_parent,
                after_in_child=self._after_fork_child,
            )

    def __repr__(self) -> str:
        # Redact the API key so it never appears in tracebacks, pytest output,
        # or repr() calls on the handler. Show just enough to identify which
        # key is in use without exposing the secret.
        if self._api_key:
            key_hint = f"{self._api_key[:4]}***"
        else:
            key_hint = "(empty)"
        return (
            f"<DatadogHandler url={self._url!r} "
            f"service={self._service!r} "
            f"api_key={key_hint} "
            f"level={logging.getLevelName(self.level)}>"
        )

    # ------------------------------------------------------------------
    # Worker thread lifecycle
    # ------------------------------------------------------------------

    def _start_worker(self) -> None:
        # Non-daemon so the thread is not killed mid-write at interpreter
        # shutdown. We register an atexit hook to flush and close cleanly
        # even if the user never calls logging.shutdown().
        self._worker_thread = threading.Thread(
            target=self._run_worker,
            name="simple-log-handlers-worker",
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
        # Build and serialise the payload entirely on the caller's thread.
        # This is the critical design decision: we do NOT enqueue the raw
        # LogRecord and format it in the worker.
        #
        # Why: if we enqueued the LogRecord, the worker would format it later
        # on a background thread. By that time, record.args might have been
        # mutated by the caller (e.g. a dict passed as a format argument and
        # then modified). Formatting late produces wrong log messages silently.
        # Serialising here also means handleError() can still reference the
        # record if json.dumps fails, and avoids captureWarnings() reentrancy
        # since warnings.warn() is never called from the worker thread.
        try:
            tags = list(self._tags)
            if self._env:
                tags.append(f"env:{self._env}")

            payload: dict[str, Any] = {
                "ddsource": self._source,
                "hostname": self._hostname,
                # self.format() respects any formatter the user attached via
                # setFormatter(), falling back to logging's default formatter.
                # The default formatter DOES include the traceback in the
                # message string when record.exc_info is set.
                "message": self.format(record),
                "status": _STATUS_MAP.get(record.levelno, record.levelname.lower()),
                "timestamp": round(record.created * 1000),
            }
            if self._service:
                payload["service"] = self._service
            if self._version:
                payload["version"] = self._version
            if tags:
                payload["ddtags"] = ",".join(tags)

            # Populate Datadog error tracking fields from exc_info.
            # These are SEPARATE from the traceback already in `message` —
            # they enable Datadog's error grouping, faceting by error type,
            # and the APM error tracking UI. Extracted from the record
            # directly so user-supplied formatters never affect this.
            if record.exc_info and record.exc_info[0] is not None:
                exc_type, exc_value, exc_tb = record.exc_info
                payload["error.kind"] = exc_type.__qualname__
                payload["error.message"] = str(exc_value)
                payload["error.stack"] = "".join(
                    traceback.format_exception(exc_type, exc_value, exc_tb)
                )
            elif record.stack_info:
                payload["error.stack"] = record.stack_info

            # Per-handler identity attributes merged before extras so
            # reserved keys cannot be overwritten by log callers.
            payload.update(self._attributes)

            # Forward non-standard LogRecord fields set via extra={}.
            # Keys colliding with _RESERVED_PAYLOAD_KEYS are dropped —
            # structural fields must not be spoofable by callers.
            # Non-JSON-serialisable values are coerced to str() so that a
            # datetime or UUID in extras never silently drops the whole record.
            extras = {
                k: v for k, v in vars(record).items()
                if k not in _STANDARD_RECORD_ATTRS
                and k not in _RESERVED_PAYLOAD_KEYS
                and not k.startswith("_")
            }
            if extras:
                payload.update(extras)

            # Serialise to bytes here, not in the worker. Each item on the
            # queue is a JSON-encoded dict (no outer brackets). The worker
            # concatenates N items into a single array: b"[" + b",".join(...) + b"]"
            item = json.dumps(payload, default=str).encode()
        except Exception:
            self.handleError(record)
            return

        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # Write directly to stderr — NOT via warnings.warn() — because if
            # the user has called logging.captureWarnings(True), the warning
            # would re-enter the logging system, potentially re-entering this
            # handler, creating a deadlock or infinite recursion.
            # One-time: a double-warn due to the non-atomic flag read is
            # harmless compared to the cost of a lock on the hot emit path.
            if not self._overflow_warned:
                self._overflow_warned = True
                sys.stderr.write(
                    f"simple-log-handlers: delivery queue is full ({self._queue_size} slots); "
                    "log records are being dropped. Increase queue_size or reduce log volume.\n"
                )

    # ------------------------------------------------------------------
    # Worker thread — runs on a background thread, never touches the record
    # ------------------------------------------------------------------

    def _run_worker(self) -> None:
        while True:
            batch: list[bytes] = []
            batch_bytes = 0
            # Set the deadline once per batch window. Every queue.get() call
            # uses the *remaining* time, not a fresh timeout. This prevents
            # a steady log stream from holding a batch open indefinitely.
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
                    # Clean shutdown: deliver the current batch then exit.
                    if batch:
                        self._send_batch(batch)
                    return

                if isinstance(item, threading.Event):
                    # flush() request: deliver current batch, signal caller,
                    # then start a fresh batch window.
                    if batch:
                        self._send_batch(batch)
                        batch = []
                        batch_bytes = 0
                    item.set()
                    deadline = time.monotonic() + self._batch_timeout
                    continue

                # Regular payload bytes
                batch.append(item)
                batch_bytes += len(item)

            if batch:
                self._send_batch(batch)

    def _send_batch(self, batch: list[bytes]) -> None:
        # Merge individual serialised payload dicts into a single JSON array.
        # b"[" + b",".join(items) + b"]" avoids decode/re-encode overhead —
        # each item is already valid JSON so concatenation is safe.
        body = b"[" + b",".join(batch) + b"]"

        headers: dict[str, str] = {
            "DD-API-KEY": self._api_key,
            "Content-Type": "application/json",
        }
        if self._compress:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"

        for attempt in range(self._max_retries + 1):
            try:
                self._client.post(self._url, content=body, headers=headers).raise_for_status()
                return
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in {429, 500, 502, 503, 504} and attempt < self._max_retries:
                    time.sleep(self._backoff(attempt, exc.response.headers.get("Retry-After")))
                    continue
                sys.stderr.write(
                    f"simple-log-handlers: failed to deliver {len(batch)} log(s): HTTP {status}\n"
                )
                return
            except Exception as exc:
                if attempt < self._max_retries:
                    time.sleep(self._backoff(attempt))
                    continue
                sys.stderr.write(
                    f"simple-log-handlers: failed to deliver {len(batch)} log(s): {exc}\n"
                )
                return

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None = None) -> float:
        # Honor Retry-After if Datadog returns one (common on 429).
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
        # Place a threading.Event on the queue. The worker, upon processing
        # it, delivers any buffered batch and sets the event — at which point
        # this call unblocks. This guarantees all records enqueued *before*
        # this flush() call have been delivered.
        if self._closed:
            return
        event = threading.Event()
        try:
            self._queue.put(event, timeout=self._shutdown_timeout)
        except queue.Full:
            return  # best-effort; cannot block
        event.wait(timeout=self._shutdown_timeout)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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
        # Acquire the queue's internal mutex before the OS duplicates the
        # process. Without this, the child could inherit the queue while
        # another thread holds its lock — that thread won't exist in the child,
        # so the lock is held forever and the first queue operation deadlocks.
        self._queue.mutex.acquire()  # type: ignore[attr-defined]

    def _after_fork_parent(self) -> None:
        self._queue.mutex.release()  # type: ignore[attr-defined]

    def _after_fork_child(self) -> None:
        # Release the lock we held before the fork, then discard everything
        # inherited from the parent: queue contents (parent will deliver them),
        # open sockets (invalid in child), and the now-dead worker thread.
        self._queue.mutex.release()  # type: ignore[attr-defined]
        self._queue = queue.Queue(maxsize=self._queue_size)
        self._overflow_warned = False
        self._closed = False
        self._client = httpx.Client(
            timeout=self._timeout_val,
            headers={"User-Agent": f"simple-log-handlers/{_version}"},
        )
        self._start_worker()


def dd_handler(
    api_key: str | None = None,
    *,
    service: str | None = None,
    env: str | None = None,
    version: str | None = None,
    hostname: str | None = None,
    source: str = "python",
    tags: list[str] | None = None,
    site: str | None = None,
    compress: bool = True,
    timeout: float = 5.0,
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
) -> _DatadogHandler:
    # Strip whitespace from all env var resolutions so that a value like
    # "\n" or "  " from a misconfigured environment is treated as absent.
    resolved_key = (api_key or os.environ.get("DD_API_KEY", "")).strip()
    if not resolved_key:
        warnings.warn(
            "dd_handler: api_key is empty (DD_API_KEY not set) — logs will not be delivered",
            UserWarning,
            stacklevel=2,
        )

    resolved_site = (site or os.environ.get("DD_SITE", "datadoghq.com")).strip()
    if not _SITE_RE.match(resolved_site):
        # Warn rather than raise: Datadog adds regions periodically and an
        # allow-list would break users on new or custom on-prem endpoints.
        warnings.warn(
            f"dd_handler: site {resolved_site!r} does not look like a valid hostname "
            f"(expected e.g. 'datadoghq.com' or 'datadoghq.eu') — delivery may fail",
            UserWarning,
            stacklevel=2,
        )

    # Datadog unified service tagging: DD_* vars take precedence, then the
    # shared LOGGING_* vars recognised by all simple-log-handlers handlers.
    resolved_service = (service or os.environ.get("DD_SERVICE", "") or os.environ.get("LOGGING_SERVICE", "")).strip() or None
    resolved_env     = (env     or os.environ.get("DD_ENV",     "") or os.environ.get("LOGGING_ENV",     "")).strip() or None
    resolved_version = (version or os.environ.get("DD_VERSION", "") or os.environ.get("LOGGING_VERSION", "")).strip() or None

    # Suppress delivery when the resolved hostname is unambiguously local
    # (e.g. "localhost") and the caller has not opted in via send_localhost_logs.
    resolved_hostname = _resolve_hostname(hostname)
    suppressed = not send_localhost_logs and _is_local_hostname(resolved_hostname)
    if suppressed:
        warnings.warn(
            f"dd_handler: hostname {resolved_hostname!r} is a local/loopback address; "
            "log delivery is suppressed to avoid accidentally shipping to a production "
            "backend from a development machine. Pass send_localhost_logs=True to deliver "
            "anyway, or set a real hostname via hostname= or LOGGING_HOSTNAME.",
            UserWarning,
            stacklevel=2,
        )

    # Resolve log level: explicit arg > DD_LOG_LEVEL env var > WARNING.
    # Using None as the default lets us distinguish "not set" from
    # "explicitly passed WARNING", so the env var only applies when the
    # caller didn't provide a level.
    resolved_level = logging.WARNING
    if level is not None:
        resolved_level = level
    else:
        env_level = os.environ.get("DD_LOG_LEVEL", "").strip().upper()
        if env_level and hasattr(logging, env_level):
            resolved_level = int(getattr(logging, env_level))

    handler = _DatadogHandler(
        resolved_key,
        service=resolved_service,
        env=resolved_env,
        version=resolved_version,
        hostname=resolved_hostname,
        source=source,
        tags=tags,
        site=resolved_site,
        compress=compress,
        timeout=timeout,
        container_key=container_key,
        customer_key=customer_key,
        database=database,
        database_type=database_type,
        table=table,
        executable_key=executable_key,
        attributes=attributes,
        queue_size=queue_size,
        batch_size=batch_size,
        batch_timeout=batch_timeout,
        max_batch_bytes=max_batch_bytes,
        max_retries=max_retries,
        shutdown_timeout=shutdown_timeout,
        suppressed=suppressed,
    )
    handler.setLevel(resolved_level)
    return handler


# Public alias — the leading underscore on _DatadogHandler signals it is an
# implementation detail, but users who want isinstance checks or type hints
# should import DatadogHandler from simple_log_handlers rather than _DatadogHandler
# from simple_log_handlers._dd.
DatadogHandler = _DatadogHandler
