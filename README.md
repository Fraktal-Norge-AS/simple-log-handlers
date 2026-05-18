# simple-log-handlers

Opinionated logging handlers for Datadog, OpenTelemetry, and CLI.

```python
from simple_log_handlers import cli_handler, dd_handler, otel_handler
import logging

logger = logging.getLogger("myapp")
logger.setLevel(logging.DEBUG)
logger.addHandler(cli_handler())
logger.addHandler(dd_handler(service="myapp", env="prod"))
logger.addHandler(otel_handler("http://openobserve:5080/api/default", service="myapp"))
```

## Installation

```
pip install simple-log-handlers
```

No extra dependencies are required for any handler — `httpx` is the only runtime dependency and is included by default.

If you want the full OpenTelemetry SDK alongside `otel_handler` (for correlated traces and metrics in the same process):

```
pip install simple-log-handlers[otel]
```

## Handlers

### `cli_handler`

A colorised `StreamHandler` for local development. Colors are level-aware (cyan → green → yellow → red → bold red) and are automatically disabled when the stream is not a TTY, or when `NO_COLOR` is set. Set `FORCE_COLOR=1` to enable them unconditionally (`FORCE_COLOR=0` disables).

```python
cli_handler(
    level=logging.DEBUG,   # handler level
    stream=sys.stderr,     # output stream
)
```

Output format: `2026-05-12 15:04:00 [INFO    ] myapp: message`

---

### `dd_handler`

Sends logs to the [Datadog HTTP Log Intake API v2](https://docs.datadoghq.com/api/latest/logs/). Defaults to `WARNING` and above.

```python
dd_handler(
    api_key="...",              # falls back to DD_API_KEY
    service="myapp",            # falls back to DD_SERVICE
    env="prod",                 # falls back to DD_ENV
    version="1.0.0",            # falls back to DD_VERSION
    hostname=None,              # falls back to DD_HOSTNAME, then socket.gethostname()
    source="python",            # ddsource field
    tags=["team:backend"],      # additional ddtags
    site="datadoghq.com",       # falls back to DD_SITE; use "datadoghq.eu" for EU
    compress=True,              # gzip payloads (recommended)
    timeout=5.0,                # HTTP request timeout in seconds
    container_key="...",        # falls back to LOGGING_CONTAINER_KEY
    customer_key="...",         # falls back to LOGGING_CUSTOMER_KEY
    database="...",             # falls back to LOGGING_DATABASE
    database_type="...",        # falls back to LOGGING_DATABASE_TYPE
    table="...",                # falls back to LOGGING_TABLE
    executable_key="...",       # falls back to LOGGING_EXECUTABLE_KEY
    attributes={"region": "eu-west-1"},  # arbitrary additional identity fields
    queue_size=10_000,          # max queued records before dropping
    batch_size=100,             # max records per HTTP request
    batch_timeout=0.5,          # max seconds to hold a partial batch
    max_batch_bytes=4*1024*1024,# byte ceiling per batch (Datadog limit is 5 MB)
    max_retries=3,              # retries on 429/5xx with exponential backoff
    shutdown_timeout=5.0,       # seconds to wait for flush on close()
    level=logging.WARNING,      # falls back to DD_LOG_LEVEL
)
```

#### Environment variables

| Variable | Used by | Default |
|---|---|---|
| `DD_API_KEY` | `api_key` | — |
| `DD_SITE` | `site` | `datadoghq.com` |
| `DD_HOSTNAME` | `hostname` | `socket.gethostname()` |
| `DD_SERVICE` | `service` | — |
| `DD_ENV` | `env` | — |
| `DD_VERSION` | `version` | — |
| `DD_LOG_LEVEL` | `level` | `WARNING` |
| `LOGGING_CONTAINER_KEY` | `container_key` | — |
| `LOGGING_CUSTOMER_KEY` | `customer_key` | — |
| `LOGGING_DATABASE` | `database` | — |
| `LOGGING_DATABASE_TYPE` | `database_type` | — |
| `LOGGING_TABLE` | `table` | — |
| `LOGGING_EXECUTABLE_KEY` | `executable_key` | — |

Explicit keyword arguments always take precedence over env vars. Fields with no value (neither arg nor env var) are omitted from the payload. Whitespace-only values are treated as absent.

#### Per-event fields via `extra`

Any key passed through `extra={}` on a log call is forwarded as a top-level attribute on the Datadog log item:

```python
logger.info("order placed", extra={"order_id": "abc-123", "amount": 99.0})
```

Reserved payload keys (`message`, `status`, `service`, `error.*`, etc.) cannot be overwritten this way — the handler's own values always win.

#### Exception tracking

`logger.exception()` and any call with `exc_info=True` automatically populates the Datadog structured error fields:

- `error.kind` — exception class name (e.g. `ValueError`)
- `error.message` — exception string
- `error.stack` — full traceback

These are separate from the traceback that may already appear in `message` via the formatter. They enable Datadog's error tracking UI, grouping, and faceting by error type.

#### Non-blocking delivery

`emit()` serialises the log record and places it on an internal queue — it returns immediately without performing any I/O. A background thread drains the queue, assembles batches of up to `batch_size` records or `batch_timeout` seconds (whichever comes first), and sends them to Datadog. Failed deliveries are retried with exponential backoff, honouring `Retry-After` on 429 responses.

```python
import queue
import logging.handlers

# If you prefer explicit control over the background thread:
q = queue.Queue(-1)
logger.addHandler(logging.handlers.QueueHandler(q))
listener = logging.handlers.QueueListener(q, dd_handler(service="myapp"))
listener.start()
```

**Shutdown:** call `logging.shutdown()` or `handler.close()` before your process exits to ensure all queued records are flushed. The handler registers an `atexit` hook as a safety net for processes that skip this step.

**Graceful failure:** HTTP errors and network failures are written to `stderr` and never raised — your application keeps running. An empty `api_key` with no `DD_API_KEY` env var emits a `UserWarning` at construction time.

---

### `otel_handler`

Sends logs to any [OTLP/HTTP](https://opentelemetry.io/docs/specs/otlp/#otlphttp) compatible backend — [OpenObserve](https://openobserve.ai), [Grafana](https://grafana.com/oss/opentelemetry/), [Honeycomb](https://www.honeycomb.io), [New Relic](https://newrelic.com), a local [OpenTelemetry Collector](https://opentelemetry.io/docs/collector/), and others. Uses the OTLP/HTTP JSON wire format. Defaults to `WARNING` and above.

```python
otel_handler(
    endpoint="http://openobserve:5080/api/default",  # falls back to OTEL_EXPORTER_OTLP_LOGS_ENDPOINT
                                                      # then OTEL_EXPORTER_OTLP_ENDPOINT
    service="myapp",           # falls back to OTEL_SERVICE_NAME
    env="prod",                # falls back to OTEL_DEPLOYMENT_ENVIRONMENT
    version="1.0.0",           # falls back to OTEL_SERVICE_VERSION
    hostname=None,             # falls back to socket.gethostname()
    headers={"Authorization": "Basic ..."},  # falls back to OTEL_EXPORTER_OTLP_HEADERS
    compress=True,             # falls back to OTEL_EXPORTER_OTLP_COMPRESSION ("gzip"/"none")
    timeout=10.0,              # falls back to OTEL_EXPORTER_OTLP_TIMEOUT (milliseconds)
    container_key="...",       # falls back to LOGGING_CONTAINER_KEY
    customer_key="...",        # falls back to LOGGING_CUSTOMER_KEY
    database="...",            # falls back to LOGGING_DATABASE
    database_type="...",       # falls back to LOGGING_DATABASE_TYPE
    table="...",               # falls back to LOGGING_TABLE
    executable_key="...",      # falls back to LOGGING_EXECUTABLE_KEY
    attributes={"region": "eu-west-1"},  # arbitrary additional resource attributes
    queue_size=10_000,         # max queued records before dropping
    batch_size=100,            # max records per HTTP request
    batch_timeout=0.5,         # max seconds to hold a partial batch
    max_batch_bytes=4*1024*1024,
    max_retries=3,             # retries on 429/5xx with exponential backoff
    shutdown_timeout=5.0,      # seconds to wait for flush on close()
    level=logging.WARNING,     # falls back to OTEL_LOG_LEVEL
)
```

#### Endpoint format

The handler appends `/v1/logs` to the endpoint automatically, unless it already ends with that path:

| Backend | `endpoint` value |
|---|---|
| OpenObserve | `http://host:5080/api/{org}` |
| OTEL Collector | `http://host:4318` |
| Grafana Cloud | `https://otlp-gateway-{region}.grafana.net/otlp` |

#### Environment variables

| Variable | Used by | Default |
|---|---|---|
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | `endpoint` | — |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `endpoint` (fallback) | — |
| `OTEL_EXPORTER_OTLP_HEADERS` | `headers` | — |
| `OTEL_EXPORTER_OTLP_COMPRESSION` | `compress` (`gzip`/`none`) | `gzip` |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `timeout` (milliseconds) | `10000` |
| `OTEL_SERVICE_NAME` | `service` | — |
| `OTEL_SERVICE_VERSION` | `version` | — |
| `OTEL_DEPLOYMENT_ENVIRONMENT` | `env` | — |
| `OTEL_LOG_LEVEL` | `level` | `WARNING` |
| `LOGGING_CONTAINER_KEY` | `container_key` | — |
| `LOGGING_CUSTOMER_KEY` | `customer_key` | — |
| `LOGGING_DATABASE` | `database` | — |
| `LOGGING_DATABASE_TYPE` | `database_type` | — |
| `LOGGING_TABLE` | `table` | — |
| `LOGGING_EXECUTABLE_KEY` | `executable_key` | — |

Explicit keyword arguments always take precedence over env vars. The `OTEL_EXPORTER_OTLP_HEADERS` env var is parsed as comma-separated `key=value` pairs; explicit `headers={}` is merged on top. `OTEL_EXPORTER_OTLP_TIMEOUT` follows the OTEL spec (milliseconds). Whitespace-only values are treated as absent.

#### Resource attributes

Service identity fields (`service`, `env`, `version`, `hostname`, `container_key`, etc.) are emitted as [OTLP resource attributes](https://opentelemetry.io/docs/specs/semconv/resource/) — metadata that describes the entity producing the logs, shared across every record from this handler. Standard OTEL semantic convention keys are used where they exist:

| Field | Resource attribute key |
|---|---|
| `service` | `service.name` |
| `version` | `service.version` |
| `env` | `deployment.environment` |
| `hostname` | `host.name` |
| `container_key` | `container_key` |
| `customer_key` | `customer_key` |
| `database` | `database` |
| `database_type` | `database_type` |
| `table` | `table` |
| `executable_key` | `executable_key` |

The `attributes` dict accepts arbitrary additional resource attributes. Keys that the handler has already resolved to a value (from the table above) cannot be overwritten — the dedicated value always takes precedence, and a `UserWarning` is emitted at construction time to flag the conflict.

Note: identity field keys use **snake_case** in OTEL resource attributes, unlike the **camelCase** used in the Datadog handler (`containerKey` → `container_key`). This follows OTEL attribute naming conventions.

#### Per-event fields via `extra`

Any key passed through `extra={}` on a log call is forwarded as an OTLP log record attribute:

```python
logger.info("order placed", extra={"order_id": "abc-123", "amount": 99.0})
```

Standard `LogRecord` fields and `_private` keys are filtered out. Keys matching the reserved exception fields (`exception.type`, `exception.message`, `exception.stacktrace`) cannot be overwritten by `extra={}` — the handler's own values always win.

#### Exception tracking

`logger.exception()` and any call with `exc_info=True` automatically populates the [OTEL exception semantic convention](https://opentelemetry.io/docs/specs/semconv/exceptions/exceptions-logs/) attributes:

- `exception.type` — exception class name (e.g. `ValueError`)
- `exception.message` — exception string
- `exception.stacktrace` — full traceback

These are extracted directly from `record.exc_info`, independently of what the user's formatter produces.

#### Wire format

Logs are sent as **OTLP/HTTP JSON** (`Content-Type: application/json`, gzip by default). This is a first-class encoding defined in the OTEL specification and accepted by all OTLP-compatible backends. Protobuf encoding is not used; `httpx` (already a dependency) handles transport so no second HTTP client is added.

#### Non-blocking delivery

`emit()` serialises the log record and places it on an internal queue — it returns immediately without performing any I/O. A background thread drains the queue, assembles batches of up to `batch_size` records or `batch_timeout` seconds (whichever comes first), and sends them to the endpoint. Failed deliveries are retried with exponential backoff, honouring `Retry-After` on 429 responses.

**Shutdown:** call `logging.shutdown()` or `handler.close()` before your process exits to flush all queued records. An `atexit` hook is registered as a safety net.

**Graceful failure:** HTTP errors and network failures are written to `stderr` and never raised.

---

## Design notes

These notes explain decisions that are not obvious from the code. They are written down so that future maintainers understand *why* things are done the way they are, not just *what* they do.

### Why `emit()` serialises on the caller thread

The payload is built and serialised to bytes in `emit()`, before being placed on the queue. The worker thread handles only network I/O — it never touches the `LogRecord`.

This resolves three subtle failure modes:

1. **Arg mutation.** Python's logging passes `record.args` by reference. If a caller logs `logger.info("user=%s", user_obj)` and then mutates `user_obj`, a worker that formats the record later would produce the wrong message. Serialising eagerly captures the values at call time.

2. **`captureWarnings()` reentrancy.** If `logging.captureWarnings(True)` is set and the worker calls `warnings.warn()`, the warning re-enters the logging system, which can re-enter this handler — deadlocking or producing infinite recursion. The worker uses `sys.stderr.write()` directly for diagnostics to avoid this path entirely.

3. **`handleError()` requires the record.** If serialisation fails (e.g. an unserializable object slips through despite `default=str`), `handleError(record)` can still be called with the original record in `emit()`. If we deferred serialisation to the worker, the record would no longer be available.

### Why non-daemon thread + atexit

The worker thread is created with `daemon=False`. Daemon threads are killed mid-write at interpreter shutdown, which would silently drop the tail of the queue. The non-daemon thread keeps the interpreter alive until it exits.

An `atexit` hook is registered to call `close()` for processes that never call `logging.shutdown()`. The two mechanisms are complementary: `logging.shutdown()` goes through the handler's `close()` method via the logging machinery; `atexit` catches everything else.

### Why three fork hooks

Forked child processes (gunicorn `preload_app=True`, Celery prefork, `multiprocessing` with the `fork` start method) inherit the parent's open sockets and worker thread. The thread doesn't exist in the child, and the sockets are invalid.

We register three `os.register_at_fork` hooks:

- **`before`**: acquires `queue.Queue.mutex` before the fork. Without this, the child could inherit the queue while another thread holds the lock — and that thread doesn't exist in the child — so the first queue operation deadlocks.
- **`after_in_parent`**: releases the mutex (parent continues normally).
- **`after_in_child`**: releases the mutex, discards the inherited queue and client, and starts a fresh worker thread. The parent delivers the items that were in the queue before the fork.

### Why the batch deadline is set once per batch

The worker sets `deadline = now + batch_timeout` once at the start of each batch accumulation window. Each `queue.get()` call uses the **remaining** time (`deadline - now`), not a fresh `batch_timeout`.

If we reset the timeout on every `get()`, a service emitting logs at a steady 10/s with `batch_timeout=0.5s` would never flush — each new record would restart the timer. The fixed deadline ensures batches are delivered within a predictable time bound regardless of log rate.

### Why `dd_handler` uses gzip instead of deflate

`Content-Encoding: deflate` per RFC 7230 means raw DEFLATE without a zlib header. Python's `zlib.compress()` produces zlib-wrapped data (with a 2-byte header and Adler-32 trailer), which is technically wrong for this Content-Encoding. Datadog's v2 intake explicitly documents `gzip` as the recommended encoding and accepts it unambiguously.

`otel_handler` also uses gzip for the same practical reason — it is the compression scheme with universal OTLP backend support — but the OTLP spec defines gzip as a first-class option rather than a workaround.

### Why `error.*` fields are extracted independently of the formatter

Datadog's error tracking UI (grouping, faceting, APM integration) requires `error.kind`, `error.message`, and `error.stack` as structured top-level fields. These are populated directly from `record.exc_info`, regardless of what the user's formatter does.

If we relied on the formatted `message` string to contain the traceback, we would need to parse it back out — fragile, locale-dependent, and formatter-dependent. Extracting from `record.exc_info` is always correct.

The traceback also appears in `message` via the formatter (the default formatter includes it). This duplication is intentional: `message` is for human readability, `error.*` is for machine-readable error tracking.

### Why `sys.stderr.write()` for queue overflow, not `warnings.warn()`

If the delivery queue is full and we call `warnings.warn()`, and the user has called `logging.captureWarnings(True)`, the warning is routed through the logging system. If this handler is attached to the root logger, the warning re-enters `emit()` — which tries to put another item on the already-full queue, triggering another warning, and so on. `sys.stderr.write()` bypasses the logging system entirely.

### Why reserved payload keys exist

`extra={}` fields are forwarded as top-level Datadog attributes. Without a reserved-key check, a caller could write:

```python
logger.info("ok", extra={"status": "info", "service": "attacker"})
```

and silently override the log level or service routing in Datadog. The `_RESERVED_PAYLOAD_KEYS` set is checked before merging extras, and any colliding key is dropped. The handler's own values always win.

### `logging.captureWarnings(True)` interaction

`dd_handler()` itself calls `warnings.warn()` if the API key is missing. This happens at construction time — before the handler is attached to any logger — so even with `captureWarnings(True)` active, the warning is not routed through this handler. It is safe.

If you call `dd_handler()` inside a logging configuration that is already live and has `captureWarnings(True)`, the warning will be captured by the existing logging setup, not by the handler being constructed.

### Why `otel_handler` uses OTLP JSON instead of protobuf

The `opentelemetry-exporter-otlp-proto-http` package uses `requests` as its HTTP transport. Adding it would introduce a second HTTP client alongside `httpx` (which `dd_handler` already uses), and would make OTEL support a heavier optional dependency. Instead, `otel_handler` sends **OTLP/HTTP JSON** over `httpx`. OTLP JSON is a first-class encoding in the OTEL specification — it is not a fallback or unofficial format — and is accepted by every OTLP-compatible backend. Gzip compression closes the size gap versus protobuf for typical log payloads.

This also means `opentelemetry-sdk` is not a runtime dependency of the handler itself. The `[otel]` extras group installs it as a convenience for users who want OTEL traces and metrics running in the same process.

### Why `otel_handler` identity fields use snake_case

The Datadog handler uses camelCase attribute keys (`containerKey`, `customerKey`) to match the field names from the legacy Fraktal logger. The OTEL handler uses snake_case (`container_key`, `customer_key`) to follow [OTEL attribute naming conventions](https://opentelemetry.io/docs/specs/semconv/general/attribute-naming/), which specify lowercase with underscores for multi-word names. The factory parameter names are the same in both handlers; only the wire representation differs.
