from __future__ import annotations

import gzip
import json
import logging
import threading
import uuid
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from simple_log_handlers import otel_handler
from simple_log_handlers._otel import _OtelHandler, _parse_headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(msg: str = "test message", level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=level, pathname="", lineno=0,
        msg=msg, args=(), exc_info=None,
    )


def _envelope(body: bytes, *, compressed: bool = True) -> dict[str, Any]:
    if compressed:
        body = gzip.decompress(body)
    return json.loads(body)  # type: ignore[no-any-return]


def _log_record(body: bytes, *, compressed: bool = True) -> dict[str, Any]:
    env = _envelope(body, compressed=compressed)
    return env["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]  # type: ignore[no-any-return]


def _resource_attrs(body: bytes, *, compressed: bool = True) -> dict[str, Any]:
    """Return resource attributes as a flat dict with Python-typed values."""
    env = _envelope(body, compressed=compressed)
    attrs = env["resourceLogs"][0]["resource"]["attributes"]
    return _flatten_attrs(attrs)


def _log_attrs(log_rec: dict[str, Any]) -> dict[str, Any]:
    """Return log record attributes as a flat dict with Python-typed values."""
    return _flatten_attrs(log_rec.get("attributes", []))


def _flatten_attrs(attrs: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for a in attrs:
        v = a["value"]
        if "stringValue" in v:
            result[a["key"]] = v["stringValue"]
        elif "intValue" in v:
            result[a["key"]] = int(v["intValue"])
        elif "doubleValue" in v:
            result[a["key"]] = v["doubleValue"]
        elif "boolValue" in v:
            result[a["key"]] = v["boolValue"]
        else:
            result[a["key"]] = v
    return result


def _flush_body(mock_post: MagicMock, h: _OtelHandler, *, compressed: bool = True) -> bytes:
    h.flush()
    return mock_post.call_args[1]["content"]  # type: ignore[no-any-return]


@pytest.fixture
def make_handler():
    """Factory that creates otel_handler instances and closes them after each test."""
    created: list[_OtelHandler] = []

    def _factory(endpoint: str = "http://localhost:4318", **kwargs: object) -> _OtelHandler:
        kwargs.setdefault("send_localhost_logs", True)  # type: ignore[call-overload]
        h = otel_handler(endpoint, **kwargs)  # type: ignore[arg-type]
        created.append(h)
        return h

    yield _factory

    for h in created:
        if not h._closed:
            h.close()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_returns_otel_handler(make_handler):
    assert isinstance(make_handler(), _OtelHandler)


def test_default_level_is_warning(make_handler):
    assert make_handler().level == logging.WARNING


def test_custom_level(make_handler):
    assert make_handler(level=logging.DEBUG).level == logging.DEBUG


def test_empty_endpoint_warns_when_no_env_fallback(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", raising=False)
    with pytest.warns(UserWarning, match="endpoint"):
        h = otel_handler()
    h.close()


# ---------------------------------------------------------------------------
# Endpoint handling
# ---------------------------------------------------------------------------

def test_v1_logs_appended_to_endpoint(make_handler):
    h = make_handler("http://localhost:4318")
    assert h._url == "http://localhost:4318/v1/logs"


def test_v1_logs_not_appended_if_already_present(make_handler):
    h = make_handler("http://localhost:4318/v1/logs")
    assert h._url == "http://localhost:4318/v1/logs"


def test_trailing_slash_stripped_before_appending(make_handler):
    h = make_handler("http://localhost:4318/")
    assert h._url == "http://localhost:4318/v1/logs"


def test_openobserve_style_endpoint(make_handler):
    h = make_handler("http://openobserve:5080/api/default")
    assert h._url == "http://openobserve:5080/api/default/v1/logs"


# ---------------------------------------------------------------------------
# Env var resolution
# ---------------------------------------------------------------------------

def test_endpoint_from_otlp_logs_endpoint_env(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://logs-host:4318")
    h = otel_handler()
    assert "logs-host" in h._url
    h.close()


def test_endpoint_from_generic_otlp_endpoint_env(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://generic-host:4318")
    h = otel_handler()
    assert "generic-host" in h._url
    h.close()


def test_logs_endpoint_takes_precedence_over_generic(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://generic:4318")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://logs:4318")
    h = otel_handler()
    assert "logs:4318" in h._url
    h.close()


def test_explicit_endpoint_takes_precedence_over_env(monkeypatch, make_handler):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://env-host:4318")
    h = make_handler("http://explicit:4318")
    assert "explicit" in h._url


def test_service_from_env_var(monkeypatch, make_handler):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "env-service")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        body = _flush_body(mock_post, h)
    assert _resource_attrs(body).get("service.name") == "env-service"


def test_explicit_service_takes_precedence_over_env(monkeypatch, make_handler):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "env-service")
    h = make_handler(service="explicit-service")
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        body = _flush_body(mock_post, h)
    assert _resource_attrs(body).get("service.name") == "explicit-service"


def test_headers_from_env_var(monkeypatch, make_handler):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic dXNlcjpwYXNz")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        assert mock_post.call_args[1]["headers"]["Authorization"] == "Basic dXNlcjpwYXNz"


def test_explicit_headers_override_env_var(monkeypatch, make_handler):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Basic env")
    h = make_handler(headers={"Authorization": "Basic explicit"})
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        assert mock_post.call_args[1]["headers"]["Authorization"] == "Basic explicit"


def test_compression_off_from_env_var(monkeypatch, make_handler):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "none")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        assert "Content-Encoding" not in mock_post.call_args[1]["headers"]


def test_timeout_from_env_var_in_milliseconds(monkeypatch, make_handler):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "3000")
    h = make_handler()
    assert h._client.timeout.connect == pytest.approx(3.0)


def test_level_from_otel_log_level_env(monkeypatch, make_handler):
    monkeypatch.setenv("OTEL_LOG_LEVEL", "DEBUG")
    h = make_handler()
    assert h.level == logging.DEBUG


def test_explicit_level_takes_precedence_over_env(monkeypatch, make_handler):
    monkeypatch.setenv("OTEL_LOG_LEVEL", "DEBUG")
    h = make_handler(level=logging.ERROR)
    assert h.level == logging.ERROR


def test_invalid_otel_log_level_falls_back_to_warning(monkeypatch, make_handler):
    monkeypatch.setenv("OTEL_LOG_LEVEL", "NONSENSE")
    h = make_handler()
    assert h.level == logging.WARNING


# ---------------------------------------------------------------------------
# HTTP request
# ---------------------------------------------------------------------------

def test_emit_posts_to_configured_url(make_handler):
    h = make_handler("http://myhost:4318")
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        assert mock_post.call_args[0][0] == "http://myhost:4318/v1/logs"


def test_content_type_is_application_json(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        assert mock_post.call_args[1]["headers"]["Content-Type"] == "application/json"


def test_raise_for_status_called(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        mock_post.return_value.raise_for_status.assert_called_once()


def test_http_error_does_not_raise(make_handler):
    h = make_handler(max_retries=0)
    error = httpx.HTTPStatusError("403", request=MagicMock(), response=MagicMock())
    error.response.status_code = 403
    error.response.headers = {}
    with patch.object(h._client, "post") as mock_post:
        mock_post.return_value.raise_for_status.side_effect = error
        h.emit(_make_record())
        h.flush()  # must not raise


def test_close_shuts_down_client(make_handler):
    h = make_handler()
    with patch.object(h._client, "close") as mock_close:
        h.close()
        mock_close.assert_called_once()


# ---------------------------------------------------------------------------
# Worker thread and non-blocking behaviour
# ---------------------------------------------------------------------------

def test_emit_returns_immediately(make_handler):
    h = make_handler()
    ready = threading.Event()

    def slow_post(*args: object, **kwargs: object) -> MagicMock:
        ready.wait()
        return MagicMock()

    import time
    with patch.object(h._client, "post", side_effect=slow_post):
        start = time.monotonic()
        h.emit(_make_record())
        elapsed = time.monotonic() - start
        ready.set()
        assert elapsed < 0.5, f"emit() blocked for {elapsed:.2f}s"


def test_flush_waits_for_delivery(make_handler):
    h = make_handler()
    delivered = threading.Event()

    def noting_post(*args: object, **kwargs: object) -> MagicMock:
        delivered.set()
        return MagicMock()

    with patch.object(h._client, "post", side_effect=noting_post):
        h.emit(_make_record())
        h.flush()
        assert delivered.is_set()


def test_close_drains_queue(make_handler):
    h = make_handler(batch_timeout=10.0)
    call_count = 0

    def counting_post(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return MagicMock()

    with patch.object(h._client, "post", side_effect=counting_post):
        for _ in range(5):
            h.emit(_make_record())
        h.close()

    assert call_count >= 1


# ---------------------------------------------------------------------------
# OTLP envelope structure
# ---------------------------------------------------------------------------

def test_otlp_envelope_structure(make_handler):
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        env = _envelope(mock_post.call_args[1]["content"], compressed=False)

    assert "resourceLogs" in env
    rl = env["resourceLogs"][0]
    assert "resource" in rl
    assert "scopeLogs" in rl
    sl = rl["scopeLogs"][0]
    assert sl["scope"]["name"] == "simple-log-handlers"
    assert "logRecords" in sl
    assert len(sl["logRecords"]) == 1


def test_payload_is_valid_json(make_handler):
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        json.loads(mock_post.call_args[1]["content"])  # must not raise


# ---------------------------------------------------------------------------
# Log record fields
# ---------------------------------------------------------------------------

def test_body_contains_formatted_message(make_handler):
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record("something broke"))
        lr = _log_record(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert lr["body"]["stringValue"] == "something broke"


def test_severity_text_matches_level_name(make_handler):
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record(level=logging.WARNING))
        lr = _log_record(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert lr["severityText"] == "WARNING"


def test_timestamp_is_nanoseconds(make_handler):
    h = make_handler(compress=False)
    record = _make_record()
    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        lr = _log_record(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert int(lr["timeUnixNano"]) == round(record.created * 1_000_000_000)


# ---------------------------------------------------------------------------
# Severity number mapping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("level,expected", [
    (logging.DEBUG,    5),
    (logging.INFO,     9),
    (logging.WARNING,  13),
    (logging.ERROR,    17),
    (logging.CRITICAL, 21),
])
def test_severity_number_mapping(make_handler, level: int, expected: int):
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record(level=level))
        lr = _log_record(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert lr["severityNumber"] == expected


# ---------------------------------------------------------------------------
# Resource attributes
# ---------------------------------------------------------------------------

def test_service_name_in_resource_attributes(make_handler):
    h = make_handler(service="myapp", compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert ra["service.name"] == "myapp"


def test_service_version_in_resource_attributes(make_handler):
    h = make_handler(version="2.0.0", compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert ra["service.version"] == "2.0.0"


def test_deployment_environment_in_resource_attributes(make_handler):
    h = make_handler(env="prod", compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert ra["deployment.environment"] == "prod"


def test_no_service_name_when_omitted(make_handler):
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert "service.name" not in ra


# ---------------------------------------------------------------------------
# Compression (gzip)
# ---------------------------------------------------------------------------

def test_compress_default_on(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        headers = mock_post.call_args[1]["headers"]
        assert headers.get("Content-Encoding") == "gzip"
        gzip.decompress(mock_post.call_args[1]["content"])  # must not raise


def test_compress_false_sends_plain_json(make_handler):
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record("plain"))
        lr = _log_record(_flush_body(mock_post, h, compressed=False), compressed=False)
        assert lr["body"]["stringValue"] == "plain"
        assert "Content-Encoding" not in mock_post.call_args[1]["headers"]


def test_compressed_body_is_smaller_than_plain(make_handler):
    plain_h = make_handler(compress=False)
    compressed_h = make_handler(compress=True)
    record = _make_record("a" * 500)
    with patch.object(plain_h._client, "post") as p1:
        plain_h.emit(record)
        plain_h.flush()
        plain_size = len(p1.call_args[1]["content"])
    with patch.object(compressed_h._client, "post") as p2:
        compressed_h.emit(record)
        compressed_h.flush()
        compressed_size = len(p2.call_args[1]["content"])
    assert compressed_size < plain_size


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------

def test_multiple_records_sent_in_one_batch(make_handler):
    h = make_handler(batch_size=10, batch_timeout=5.0, compress=False)
    with patch.object(h._client, "post") as mock_post:
        for _ in range(3):
            h.emit(_make_record())
        h.flush()
        assert mock_post.call_count == 1
        env = _envelope(mock_post.call_args[1]["content"], compressed=False)
        records = env["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
        assert len(records) == 3


def test_batch_size_triggers_send(make_handler):
    h = make_handler(batch_size=2, batch_timeout=60.0, compress=False)
    sent: list[int] = []

    def capture_post(*args: object, **kwargs: object) -> MagicMock:
        env = _envelope(kwargs["content"], compressed=False)
        sent.append(len(env["resourceLogs"][0]["scopeLogs"][0]["logRecords"]))
        return MagicMock()

    with patch.object(h._client, "post", side_effect=capture_post):
        h.emit(_make_record())
        h.emit(_make_record())
        h.flush()

    assert 2 in sent


# ---------------------------------------------------------------------------
# Retry with backoff
# ---------------------------------------------------------------------------

def test_retry_on_transient_http_error(make_handler):
    h = make_handler(max_retries=2, batch_timeout=0.01)
    attempt = 0

    def flaky_post(*args: object, **kwargs: object) -> MagicMock:
        nonlocal attempt
        attempt += 1
        if attempt < 3:
            resp = MagicMock()
            resp.status_code = 503
            resp.headers = {}
            raise httpx.HTTPStatusError("503", request=MagicMock(), response=resp)
        return MagicMock()

    with patch.object(h._client, "post", side_effect=flaky_post):
        with patch.object(h._stop_event, "wait", return_value=False):
            h.emit(_make_record())
            h.flush()

    assert attempt == 3


def test_no_retry_on_non_transient_error(make_handler):
    h = make_handler(max_retries=3, batch_timeout=0.01)
    attempt = 0

    def bad_post(*args: object, **kwargs: object) -> MagicMock:
        nonlocal attempt
        attempt += 1
        resp = MagicMock()
        resp.status_code = 403
        resp.headers = {}
        raise httpx.HTTPStatusError("403", request=MagicMock(), response=resp)

    with patch.object(h._client, "post", side_effect=bad_post):
        h.emit(_make_record())
        h.flush()

    assert attempt == 1


def test_retry_after_header_honored(make_handler):
    h = make_handler(max_retries=1, batch_timeout=0.01)
    waits: list[float] = []
    attempt = 0

    def rate_limited(*args: object, **kwargs: object) -> MagicMock:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            resp = MagicMock()
            resp.status_code = 429
            resp.headers = {"Retry-After": "7"}
            raise httpx.HTTPStatusError("429", request=MagicMock(), response=resp)
        return MagicMock()

    with patch.object(h._client, "post", side_effect=rate_limited):
        with patch.object(h._stop_event, "wait", side_effect=lambda secs: waits.append(secs) or False):
            h.emit(_make_record())
            h.flush()

    assert waits[0] == pytest.approx(7.0)


def test_network_failure_does_not_crash_worker(make_handler):
    h = make_handler(max_retries=0, batch_timeout=0.01)
    with patch.object(h._client, "post", side_effect=ConnectionError("refused")):
        h.emit(_make_record())
        h.flush()  # must not raise or stall


# ---------------------------------------------------------------------------
# Exception tracking
# ---------------------------------------------------------------------------

def test_exception_populates_exception_attributes(make_handler):
    h = make_handler(compress=False)
    logger = logging.getLogger("test.otel.exc")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    logger.addHandler(h)

    with patch.object(h._client, "post") as mock_post:
        try:
            raise ValueError("something went wrong")
        except ValueError:
            logger.exception("caught it")
        lr = _log_record(_flush_body(mock_post, h, compressed=False), compressed=False)
        attrs = _log_attrs(lr)

    assert attrs["exception.type"] == "ValueError"
    assert attrs["exception.message"] == "something went wrong"
    assert "ValueError" in str(attrs["exception.stacktrace"])


def test_no_exception_attributes_without_exception(make_handler):
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record(level=logging.ERROR))
        lr = _log_record(_flush_body(mock_post, h, compressed=False), compressed=False)
        attrs = _log_attrs(lr)

    assert "exception.type" not in attrs
    assert "exception.message" not in attrs
    assert "exception.stacktrace" not in attrs


def test_user_formatter_not_overridden_by_handler(make_handler):
    h = make_handler(compress=False)
    h.setFormatter(logging.Formatter("CUSTOM %(message)s"))
    with patch.object(h._client, "post") as mock_post:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            import sys
            record = logging.LogRecord("t", logging.ERROR, "", 0, "oops", (), sys.exc_info())
        h.emit(record)
        lr = _log_record(_flush_body(mock_post, h, compressed=False), compressed=False)

    assert lr["body"]["stringValue"].startswith("CUSTOM")
    assert _log_attrs(lr)["exception.type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# extra={} forwarding as OTLP attributes
# ---------------------------------------------------------------------------

def test_extra_fields_forwarded_as_attributes(make_handler):
    h = make_handler(compress=False)
    record = logging.LogRecord(
        name="test.extra", level=logging.ERROR, pathname="", lineno=0,
        msg="something", args=(), exc_info=None,
    )
    record.order_id = "abc-123"  # type: ignore[attr-defined]
    record.amount = 99.0         # type: ignore[attr-defined]

    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        lr = _log_record(_flush_body(mock_post, h, compressed=False), compressed=False)
        attrs = _log_attrs(lr)

    assert attrs["order_id"] == "abc-123"
    assert attrs["amount"] == pytest.approx(99.0)


def test_standard_record_attrs_not_in_log_attributes(make_handler):
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        lr = _log_record(_flush_body(mock_post, h, compressed=False), compressed=False)
        attrs = _log_attrs(lr)

    assert "lineno" not in attrs
    assert "thread" not in attrs
    assert "name" not in attrs


def test_private_attrs_not_in_log_attributes(make_handler):
    h = make_handler(compress=False)
    record = _make_record()
    record.__dict__["_secret"] = "hidden"

    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        attrs = _log_attrs(_log_record(_flush_body(mock_post, h, compressed=False), compressed=False))

    assert "_secret" not in attrs


def test_reserved_attribute_keys_protected(make_handler):
    h = make_handler(compress=False)
    logger = logging.getLogger("test.otel.reserved")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    logger.addHandler(h)

    with patch.object(h._client, "post") as mock_post:
        try:
            raise ValueError("real error")
        except ValueError:
            logger.exception("oops", extra={"exception.type": "FakeError"})
        attrs = _log_attrs(_log_record(_flush_body(mock_post, h, compressed=False), compressed=False))

    assert attrs["exception.type"] == "ValueError"


def test_non_serialisable_extra_does_not_drop_log(make_handler):
    h = make_handler(compress=False)
    record = _make_record("event")
    record.ts = datetime(2026, 1, 1)   # type: ignore[attr-defined]
    record.uid = uuid.UUID(int=0)      # type: ignore[attr-defined]
    record.raw = b"bytes"              # type: ignore[attr-defined]

    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        h.flush()
        mock_post.assert_called_once()
        attrs = _log_attrs(_log_record(_flush_body(mock_post, h, compressed=False), compressed=False))

    assert "ts" in attrs
    assert "uid" in attrs
    assert "raw" in attrs


# ---------------------------------------------------------------------------
# Identity fields (resource attributes)
# ---------------------------------------------------------------------------

def test_identity_fields_in_resource_attributes(make_handler):
    h = make_handler(
        container_key="c1", customer_key="cust1", database="mydb",
        database_type="postgres", table="orders",
        compress=False,
    )
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)

    assert ra["container_key"] == "c1"
    assert ra["customer_key"] == "cust1"
    assert ra["database"] == "mydb"
    assert ra["database_type"] == "postgres"
    assert ra["table"] == "orders"
    assert "executable_key" not in ra  # moved to per-record attributes


def test_executable_key_in_log_record_attributes(make_handler):
    h = make_handler(executable_key="etl", compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        attrs = _log_attrs(_log_record(_flush_body(mock_post, h, compressed=False), compressed=False))

    assert attrs["executable_key"] == "etl"


def test_identity_fields_from_logging_env_vars(monkeypatch, make_handler):
    monkeypatch.setenv("LOGGING_CONTAINER_KEY", "env-container")
    monkeypatch.setenv("LOGGING_DATABASE", "env-db")
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)

    assert ra["container_key"] == "env-container"
    assert ra["database"] == "env-db"


def test_explicit_identity_field_takes_precedence_over_env(monkeypatch, make_handler):
    monkeypatch.setenv("LOGGING_DATABASE", "env-db")
    h = make_handler(database="explicit-db", compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)

    assert ra["database"] == "explicit-db"


def test_empty_identity_fields_not_in_resource(make_handler):
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)

    assert "container_key" not in ra
    assert "database" not in ra


def test_arbitrary_attributes_in_resource(make_handler):
    h = make_handler(attributes={"region": "eu-west-1", "team": "backend"}, compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)

    assert ra["region"] == "eu-west-1"
    assert ra["team"] == "backend"


def test_attributes_collision_emits_warning(make_handler):
    with pytest.warns(UserWarning, match="service.name"):
        make_handler(service="real", attributes={"service.name": "spoofed"})


def test_attributes_dict_cannot_overwrite_service_name(make_handler):
    with pytest.warns(UserWarning, match="service.name"):
        h = make_handler(service="real", attributes={"service.name": "spoofed"}, compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)

    assert ra["service.name"] == "real"


# ---------------------------------------------------------------------------
# _parse_headers
# ---------------------------------------------------------------------------

def test_parse_headers_basic():
    assert _parse_headers("x-key=val") == {"x-key": "val"}


def test_parse_headers_multiple():
    result = _parse_headers("Authorization=Basic foo,x-custom=bar")
    assert result == {"Authorization": "Basic foo", "x-custom": "bar"}


def test_parse_headers_base64_value_with_equals_sign():
    # base64 padding "==" must survive the partition-based split
    result = _parse_headers("Authorization=Basic dXNlcjpwYXNz==")
    assert result["Authorization"] == "Basic dXNlcjpwYXNz=="


def test_parse_headers_empty_string():
    assert _parse_headers("") == {}


# ---------------------------------------------------------------------------
# repr
# ---------------------------------------------------------------------------

def test_repr_redacts_authorization_header(make_handler):
    h = make_handler(headers={"Authorization": "Basic secrettoken"})
    r = repr(h)
    assert "Basic se***" in r
    assert "secrettoken" not in r


def test_repr_shows_none_when_no_auth(make_handler):
    h = make_handler()
    assert "auth=(none)" in repr(h)


def test_repr_shows_service_and_level(make_handler):
    h = make_handler(service="myapp", level=logging.ERROR)
    r = repr(h)
    assert "myapp" in r
    assert "ERROR" in r


# ---------------------------------------------------------------------------
# Public OtelHandler alias
# ---------------------------------------------------------------------------

def test_otel_handler_importable_from_package():
    from simple_log_handlers import OtelHandler
    assert OtelHandler is _OtelHandler


# ---------------------------------------------------------------------------
# Default timeout
# ---------------------------------------------------------------------------

def test_default_timeout_is_ten_seconds(make_handler):
    h = make_handler()
    assert h._client.timeout.connect == pytest.approx(10.0)


def test_custom_timeout(make_handler):
    h = make_handler(timeout=2.5)
    assert h._client.timeout.connect == pytest.approx(2.5)


# ---------------------------------------------------------------------------
# intValue encoding (proto3 JSON mapping: int64 must be a string)
# ---------------------------------------------------------------------------

def test_int_extra_encoded_as_string_in_raw_json(make_handler):
    h = make_handler(compress=False)
    record = _make_record()
    record.count = 42  # type: ignore[attr-defined]
    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        h.flush()
        raw = json.loads(mock_post.call_args[1]["content"])
    lr = raw["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    count_attr = next(a for a in lr["attributes"] if a["key"] == "count")
    # intValue must be a JSON string per the proto3 JSON mapping, not a number
    assert count_attr["value"] == {"intValue": "42"}


# ---------------------------------------------------------------------------
# boolValue must not be encoded as intValue (bool is a subclass of int)
# ---------------------------------------------------------------------------

def test_bool_extra_encoded_as_bool_value_not_int(make_handler):
    h = make_handler(compress=False)
    record = _make_record()
    record.flag = True  # type: ignore[attr-defined]
    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        h.flush()
        raw = json.loads(mock_post.call_args[1]["content"])
    lr = raw["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    flag_attr = next(a for a in lr["attributes"] if a["key"] == "flag")
    assert flag_attr["value"] == {"boolValue": True}
    assert "intValue" not in flag_attr["value"]


# ---------------------------------------------------------------------------
# Multi-record batch envelope correctness
# ---------------------------------------------------------------------------

def test_multi_record_batch_is_valid_json_array(make_handler):
    h = make_handler(batch_size=10, batch_timeout=5.0, compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record("first"))
        h.emit(_make_record("second"))
        h.emit(_make_record("third"))
        h.flush()
        raw = json.loads(mock_post.call_args[1]["content"])  # must not raise
    records = raw["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    assert len(records) == 3
    bodies = [r["body"]["stringValue"] for r in records]
    assert "first" in bodies
    assert "second" in bodies
    assert "third" in bodies


# ---------------------------------------------------------------------------
# Queue overflow writes to stderr
# ---------------------------------------------------------------------------

def test_queue_overflow_writes_to_stderr(make_handler, capsys):
    h = make_handler(queue_size=1, batch_timeout=60.0)
    ready = threading.Event()

    def slow_post(*args: object, **kwargs: object) -> MagicMock:
        ready.wait()
        return MagicMock()

    with patch.object(h._client, "post", side_effect=slow_post):
        h.emit(_make_record())  # fills the queue or is consumed immediately
        # Flood until the overflow warning is triggered
        for _ in range(20):
            h.emit(_make_record())
        ready.set()

    captured = capsys.readouterr()
    assert "otel delivery queue is full" in captured.err


# ---------------------------------------------------------------------------
# Non-standard level maps to nearest severity (not 0)
# ---------------------------------------------------------------------------

def test_non_standard_level_maps_to_nearest_severity(make_handler):
    h = make_handler(compress=False)
    record = _make_record(level=25)  # between INFO(20) and WARNING(30)
    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        lr = _log_record(_flush_body(mock_post, h, compressed=False), compressed=False)
    # 25 >= INFO(20) but < WARNING(30) → nearest is INFO → 9
    assert lr["severityNumber"] == 9


# ---------------------------------------------------------------------------
# Shared LOGGING_* env vars
# ---------------------------------------------------------------------------

def test_service_from_logging_service_env(monkeypatch, make_handler):
    monkeypatch.setenv("LOGGING_SERVICE", "shared-svc")
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert ra["service.name"] == "shared-svc"


def test_otel_service_name_takes_precedence_over_logging_service(monkeypatch, make_handler):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "otel-svc")
    monkeypatch.setenv("LOGGING_SERVICE", "shared-svc")
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert ra["service.name"] == "otel-svc"


def test_env_from_logging_env_var(monkeypatch, make_handler):
    monkeypatch.setenv("LOGGING_ENV", "shared-env")
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert ra["deployment.environment"] == "shared-env"


def test_version_from_logging_version_var(monkeypatch, make_handler):
    monkeypatch.setenv("LOGGING_VERSION", "3.0.0")
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert ra["service.version"] == "3.0.0"


def test_hostname_from_logging_hostname_env(monkeypatch, make_handler):
    monkeypatch.setenv("LOGGING_HOSTNAME", "shared-host")
    h = make_handler(compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        ra = _resource_attrs(_flush_body(mock_post, h, compressed=False), compressed=False)
    assert ra["host.name"] == "shared-host"


# ---------------------------------------------------------------------------
# Localhost suppression
# ---------------------------------------------------------------------------

def test_localhost_hostname_suppresses_delivery():
    with patch("simple_log_handlers._otel.socket.gethostname", return_value="localhost"):
        with pytest.warns(UserWarning, match="local/loopback"):
            h = otel_handler("http://backend:4318")
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        mock_post.assert_not_called()
    h.close()


def test_loopback_ip_suppresses_delivery():
    with patch("simple_log_handlers._otel.socket.gethostname", return_value="127.0.0.1"):
        with pytest.warns(UserWarning, match="local/loopback"):
            h = otel_handler("http://backend:4318")
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        mock_post.assert_not_called()
    h.close()


def test_send_localhost_logs_true_bypasses_suppression():
    with patch("simple_log_handlers._otel.socket.gethostname", return_value="localhost"):
        h = otel_handler("http://backend:4318", send_localhost_logs=True)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        mock_post.assert_called_once()
    h.close()


def test_non_local_hostname_not_suppressed(make_handler):
    h = make_handler(hostname="prod-server-01")
    assert not h._suppressed


def test_dot_local_hostname_not_suppressed(make_handler):
    # .local suffix is intentionally NOT suppressed — it can appear in
    # production LAN environments.
    h = make_handler(hostname="myserver.local")
    assert not h._suppressed


# ---------------------------------------------------------------------------
# Endpoint validation and suppression
# ---------------------------------------------------------------------------

def test_empty_endpoint_suppresses_delivery(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", raising=False)
    with pytest.warns(UserWarning):
        h = otel_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        mock_post.assert_not_called()
    h.close()


def test_malformed_endpoint_warns_and_suppresses():
    with pytest.warns(UserWarning, match="does not look like a valid"):
        h = otel_handler("not-a-url")
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        mock_post.assert_not_called()
    h.close()


def test_valid_https_endpoint_not_suppressed(make_handler):
    h = make_handler("https://collector.example.com:4318")
    assert not h._suppressed


# ---------------------------------------------------------------------------
# executable_key / with_executable_key / include_logger_name
# ---------------------------------------------------------------------------

from simple_log_handlers._context import _executable_key_var


def test_static_executable_key_in_log_record(make_handler):
    h = make_handler(executable_key="my-etl", compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        attrs = _log_attrs(_log_record(_flush_body(mock_post, h, compressed=False), compressed=False))
    assert attrs["executable_key"] == "my-etl"


def test_ctx_key_overrides_static_executable_key(make_handler):
    h = make_handler(executable_key="static", compress=False)
    token = _executable_key_var.set("dynamic")
    try:
        with patch.object(h._client, "post") as mock_post:
            h.emit(_make_record())
            attrs = _log_attrs(_log_record(_flush_body(mock_post, h, compressed=False), compressed=False))
        assert attrs["executable_key"] == "dynamic"
    finally:
        _executable_key_var.reset(token)


def test_ctx_key_alone_no_logger_name_appended(make_handler):
    h = make_handler(compress=False)
    record = _make_record()
    record.name = "prefect.flow_run"
    token = _executable_key_var.set("my_flow")
    try:
        with patch.object(h._client, "post") as mock_post:
            h.emit(record)
            attrs = _log_attrs(_log_record(_flush_body(mock_post, h, compressed=False), compressed=False))
        assert attrs["executable_key"] == "my_flow"
    finally:
        _executable_key_var.reset(token)


def test_include_logger_name_true_combines_ctx_key_and_log_name(make_handler):
    h = make_handler(include_logger_name=True, compress=False)
    record = _make_record()
    record.name = "my_module.helper"
    token = _executable_key_var.set("my_function")
    try:
        with patch.object(h._client, "post") as mock_post:
            h.emit(record)
            attrs = _log_attrs(_log_record(_flush_body(mock_post, h, compressed=False), compressed=False))
        assert attrs["executable_key"] == "my_function::my_module.helper"
    finally:
        _executable_key_var.reset(token)


def test_executable_key_not_spoofable_via_extra(make_handler):
    h = make_handler(executable_key="real", compress=False)
    record = _make_record()
    record.__dict__["executable_key"] = "spoofed"  # type: ignore[assignment]
    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        attrs = _log_attrs(_log_record(_flush_body(mock_post, h, compressed=False), compressed=False))
    assert attrs["executable_key"] == "real"
