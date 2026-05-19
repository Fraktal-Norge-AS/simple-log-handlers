from __future__ import annotations

import gzip
import json
import logging
import threading
import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from simple_log_handlers import dd_handler
from simple_log_handlers._dd import _DatadogHandler, _resolve_hostname


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_record(msg: str = "test message", level: int = logging.ERROR) -> logging.LogRecord:
    return logging.LogRecord(
        name="test", level=level, pathname="", lineno=0,
        msg=msg, args=(), exc_info=None,
    )


def _payload(mock_post: MagicMock, h: _DatadogHandler, *, compressed: bool = True) -> dict:  # type: ignore[type-arg]
    """Flush the handler then decode the first log item from the captured post call."""
    h.flush()
    body = mock_post.call_args[1]["content"]
    if compressed:
        body = gzip.decompress(body)
    return json.loads(body)[0]


@pytest.fixture
def make_handler():
    """Factory that creates dd_handler instances and closes them after each test."""
    created: list[_DatadogHandler] = []

    def _factory(api_key: str | None = "a" * 32, **kwargs: object) -> _DatadogHandler:
        kwargs.setdefault("send_localhost_logs", True)  # type: ignore[call-overload]
        h = dd_handler(api_key, **kwargs)  # type: ignore[arg-type]
        created.append(h)
        return h

    yield _factory

    for h in created:
        if not h._closed:
            h.close()


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

def test_returns_datadog_handler(make_handler):
    assert isinstance(make_handler(), _DatadogHandler)


def test_default_level_is_warning(make_handler):
    assert make_handler().level == logging.WARNING


def test_custom_level(make_handler):
    assert make_handler( level=logging.DEBUG).level == logging.DEBUG


def test_empty_api_key_warns_when_no_env_fallback(monkeypatch):
    monkeypatch.delenv("DD_API_KEY", raising=False)
    with pytest.warns(UserWarning, match="api_key"):
        h = dd_handler()
    h.close()


# ---------------------------------------------------------------------------
# Env var resolution
# ---------------------------------------------------------------------------

def test_api_key_from_env_var(monkeypatch, make_handler):
    monkeypatch.setenv("DD_API_KEY", "e" * 32)
    h = make_handler(None)  # no explicit key so env var takes effect
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        assert mock_post.call_args[1]["headers"]["DD-API-KEY"] == "e" * 32


def test_explicit_api_key_takes_precedence_over_env(monkeypatch, make_handler):
    monkeypatch.setenv("DD_API_KEY", "e" * 32)
    h = make_handler("f" * 32)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        assert mock_post.call_args[1]["headers"]["DD-API-KEY"] == "f" * 32


def test_site_from_env_var(monkeypatch, make_handler):
    monkeypatch.setenv("DD_SITE", "datadoghq.eu")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        assert "datadoghq.eu" in mock_post.call_args[0][0]


def test_explicit_site_takes_precedence_over_env(monkeypatch, make_handler):
    monkeypatch.setenv("DD_SITE", "datadoghq.eu")
    h = make_handler( site="datadoghq.com")
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        assert "datadoghq.com" in mock_post.call_args[0][0]


# ---------------------------------------------------------------------------
# Hostname resolution
# ---------------------------------------------------------------------------

def test_hostname_explicit_arg():
    assert _resolve_hostname("myhost") == "myhost"


def test_hostname_from_dd_hostname_env(monkeypatch):
    monkeypatch.setenv("DD_HOSTNAME", "env-host")
    assert _resolve_hostname(None) == "env-host"


def test_explicit_hostname_takes_precedence_over_env(monkeypatch):
    monkeypatch.setenv("DD_HOSTNAME", "env-host")
    assert _resolve_hostname("explicit") == "explicit"


def test_hostname_from_logging_hostname_env(monkeypatch):
    monkeypatch.delenv("DD_HOSTNAME", raising=False)
    monkeypatch.setenv("LOGGING_HOSTNAME", "shared-host")
    assert _resolve_hostname(None) == "shared-host"


def test_dd_hostname_takes_precedence_over_logging_hostname(monkeypatch):
    monkeypatch.setenv("DD_HOSTNAME", "dd-host")
    monkeypatch.setenv("LOGGING_HOSTNAME", "shared-host")
    assert _resolve_hostname(None) == "dd-host"


def test_hostname_falls_back_to_gethostname(monkeypatch):
    monkeypatch.delenv("DD_HOSTNAME", raising=False)
    monkeypatch.delenv("LOGGING_HOSTNAME", raising=False)
    with patch("simple_log_handlers._dd.socket.gethostname", return_value="socket-host"):
        assert _resolve_hostname(None) == "socket-host"


def test_hostname_returns_empty_string_on_socket_error(monkeypatch):
    monkeypatch.delenv("DD_HOSTNAME", raising=False)
    monkeypatch.delenv("LOGGING_HOSTNAME", raising=False)
    with patch("simple_log_handlers._dd.socket.gethostname", side_effect=OSError):
        assert _resolve_hostname(None) == ""


# ---------------------------------------------------------------------------
# HTTP request
# ---------------------------------------------------------------------------

def test_emit_posts_to_correct_url(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        url = mock_post.call_args[0][0]
        assert "http-intake.logs.datadoghq.com" in url
        assert "/api/v2/logs" in url


def test_emit_sends_api_key_header(make_handler):
    h = make_handler("b" * 32)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        assert mock_post.call_args[1]["headers"]["DD-API-KEY"] == "b" * 32


def test_eu_site(make_handler):
    h = make_handler( site="datadoghq.eu")
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        assert "datadoghq.eu" in mock_post.call_args[0][0]


def test_raise_for_status_called(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        mock_post.return_value.raise_for_status.assert_called_once()


def test_http_error_response_does_not_raise(make_handler):
    h = make_handler( max_retries=0)
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
    # emit() must not block on I/O — it puts to the queue and returns.
    h = make_handler()
    ready = threading.Event()

    def slow_post(*args: object, **kwargs: object) -> MagicMock:
        ready.wait()  # blocks until test releases it
        return MagicMock()

    with patch.object(h._client, "post", side_effect=slow_post):
        import time
        start = time.monotonic()
        h.emit(_make_record())
        elapsed = time.monotonic() - start
        ready.set()  # unblock worker so close() can finish
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
    h = make_handler( batch_timeout=10.0)
    call_count = 0

    def counting_post(*args: object, **kwargs: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        return MagicMock()

    with patch.object(h._client, "post", side_effect=counting_post):
        for _ in range(5):
            h.emit(_make_record())
        h.close()

    assert call_count >= 1  # at least one batch was sent before exit


# ---------------------------------------------------------------------------
# Batching
# ---------------------------------------------------------------------------

def test_multiple_records_sent_in_one_batch(make_handler):
    # batch_size=10, emit 3 records, flush — should be one POST with 3 items.
    h = make_handler( batch_size=10, batch_timeout=5.0, compress=False)
    with patch.object(h._client, "post") as mock_post:
        for _ in range(3):
            h.emit(_make_record())
        h.flush()
        assert mock_post.call_count == 1
        body = mock_post.call_args[1]["content"]
        items = json.loads(body)
        assert len(items) == 3


def test_batch_size_triggers_send(make_handler):
    # batch_size=2: after 2 records a batch fires without waiting for timeout.
    h = make_handler( batch_size=2, batch_timeout=60.0, compress=False)
    sent: list[int] = []

    def capture_post(*args: object, **kwargs: object) -> MagicMock:
        body = kwargs["content"]
        sent.append(len(json.loads(body)))
        return MagicMock()

    with patch.object(h._client, "post", side_effect=capture_post):
        h.emit(_make_record())
        h.emit(_make_record())
        h.flush()  # first batch of 2 already sent; flush delivers any remainder

    assert 2 in sent


# ---------------------------------------------------------------------------
# Retry with backoff
# ---------------------------------------------------------------------------

def test_retry_on_transient_http_error(make_handler):
    h = make_handler( max_retries=2, batch_timeout=0.01)
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
        with patch.object(h._stop_event, "wait", return_value=False):  # skip backoff wait
            h.emit(_make_record())
            h.flush()

    assert attempt == 3  # two failures then one success


def test_no_retry_on_non_transient_error(make_handler):
    h = make_handler( max_retries=3, batch_timeout=0.01)
    attempt = 0

    def bad_key_post(*args: object, **kwargs: object) -> MagicMock:
        nonlocal attempt
        attempt += 1
        resp = MagicMock()
        resp.status_code = 403
        resp.headers = {}
        raise httpx.HTTPStatusError("403", request=MagicMock(), response=resp)

    with patch.object(h._client, "post", side_effect=bad_key_post):
        h.emit(_make_record())
        h.flush()

    assert attempt == 1  # 403 is not retried


def test_retry_after_header_honored(make_handler):
    h = make_handler( max_retries=1, batch_timeout=0.01)
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
    h = make_handler( max_retries=0, batch_timeout=0.01)
    with patch.object(h._client, "post", side_effect=ConnectionError("refused")):
        h.emit(_make_record())
        h.flush()  # must not raise or stall


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------

def test_default_timeout(make_handler):
    h = make_handler()
    assert h._client.timeout.connect == 5.0


def test_custom_timeout(make_handler):
    h = make_handler( timeout=1.5)
    assert h._client.timeout.connect == 1.5


# ---------------------------------------------------------------------------
# Payload fields
# ---------------------------------------------------------------------------

def test_emit_payload_fields(make_handler):
    h = make_handler( service="svc", env="prod", source="myapp")
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record("something broke", logging.ERROR))
        p = _payload(mock_post, h)

    assert p["message"] == "something broke"
    assert p["status"] == "error"
    assert p["service"] == "svc"
    assert p["ddsource"] == "myapp"
    assert "env:prod" in p["ddtags"]


def test_emit_timestamp_is_milliseconds(make_handler):
    h = make_handler()
    record = _make_record()
    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        assert _payload(mock_post, h)["timestamp"] == round(record.created * 1000)


def test_emit_tags_merged_with_env(make_handler):
    h = make_handler( env="staging", tags=["team:backend", "version:2"])
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        tags = _payload(mock_post, h)["ddtags"].split(",")

    assert "env:staging" in tags
    assert "team:backend" in tags
    assert "version:2" in tags


def test_emit_no_service_key_when_omitted(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert "service" not in _payload(mock_post, h)


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------

def test_status_warning_maps_to_warn(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record(level=logging.WARNING))
        assert _payload(mock_post, h)["status"] == "warn"


def test_status_critical_maps_to_critical(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record(level=logging.CRITICAL))
        assert _payload(mock_post, h)["status"] == "critical"


def test_status_info_maps_to_info(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record(level=logging.INFO))
        assert _payload(mock_post, h)["status"] == "info"


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
    h = make_handler( compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record("plain"))
        p = _payload(mock_post, h, compressed=False)
        assert p["message"] == "plain"
        assert "Content-Encoding" not in mock_post.call_args[1]["headers"]


def test_compressed_body_is_smaller_than_plain(make_handler):
    plain_h = make_handler( compress=False)
    compressed_h = make_handler( compress=True)
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
# Failure handling
# ---------------------------------------------------------------------------

def test_emit_does_not_raise_on_network_failure(make_handler):
    h = make_handler( max_retries=0)
    with patch.object(h._client, "post", side_effect=ConnectionError("refused")):
        h.emit(_make_record())
        h.flush()  # must not propagate


# ---------------------------------------------------------------------------
# Traceback forwarding (task 21)
# ---------------------------------------------------------------------------

def test_exception_populates_error_fields(make_handler):
    h = make_handler()
    logger = logging.getLogger("test.exc")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    logger.addHandler(h)

    with patch.object(h._client, "post") as mock_post:
        try:
            raise ValueError("something went wrong")
        except ValueError:
            logger.exception("caught it")
        p = _payload(mock_post, h)

    assert p["error.kind"] == "ValueError"
    assert p["error.message"] == "something went wrong"
    assert "ValueError" in p["error.stack"]


def test_no_error_fields_without_exception(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record(level=logging.ERROR))
        p = _payload(mock_post, h)

    assert "error.kind" not in p
    assert "error.message" not in p
    assert "error.stack" not in p


def test_user_formatter_not_overridden_by_handler(make_handler):
    h = make_handler()
    h.setFormatter(logging.Formatter("CUSTOM %(message)s"))
    with patch.object(h._client, "post") as mock_post:
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            import sys
            record = logging.LogRecord(
                "t", logging.ERROR, "", 0, "oops", (), sys.exc_info()
            )
        h.emit(record)
        p = _payload(mock_post, h)

    assert p["message"].startswith("CUSTOM")
    assert p["error.kind"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Non-JSON-serialisable extras (task 22)
# ---------------------------------------------------------------------------

def test_non_serialisable_extra_does_not_drop_log(make_handler):
    h = make_handler()
    record = _make_record("event")
    record.ts = datetime(2026, 1, 1)       # type: ignore[attr-defined]
    record.uid = uuid.UUID(int=0)          # type: ignore[attr-defined]
    record.raw = b"bytes"                  # type: ignore[attr-defined]

    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        h.flush()
        mock_post.assert_called_once()
        p = _payload(mock_post, h)

    assert "ts" in p
    assert "uid" in p
    assert "raw" in p


# ---------------------------------------------------------------------------
# Reserved key protection (task 23)
# ---------------------------------------------------------------------------

def test_extra_cannot_overwrite_status(make_handler):
    h = make_handler()
    record = _make_record(level=logging.ERROR)
    record.status = "fake"  # type: ignore[attr-defined]

    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        assert _payload(mock_post, h)["status"] == "error"


def test_extra_cannot_overwrite_service(make_handler):
    h = make_handler( service="real-service")
    record = _make_record()
    record.service = "spoofed"  # type: ignore[attr-defined]

    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        assert _payload(mock_post, h)["service"] == "real-service"


def test_extra_cannot_overwrite_error_fields(make_handler):
    h = make_handler()
    logger = logging.getLogger("test.reserved")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.ERROR)
    logger.addHandler(h)

    with patch.object(h._client, "post") as mock_post:
        try:
            raise ValueError("real error")
        except ValueError:
            logger.exception("oops", extra={"error.kind": "FakeError"})
        p = _payload(mock_post, h)

    assert p["error.kind"] == "ValueError"


# ---------------------------------------------------------------------------
# extra={} forwarding
# ---------------------------------------------------------------------------

def test_extra_fields_forwarded_to_payload(make_handler):
    h = make_handler()
    record = logging.LogRecord(
        name="test.extra", level=logging.ERROR, pathname="", lineno=0,
        msg="something", args=(), exc_info=None,
    )
    record.order_id = "abc-123"  # type: ignore[attr-defined]
    record.user_id = 42          # type: ignore[attr-defined]
    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        p = _payload(mock_post, h)

    assert p["order_id"] == "abc-123"
    assert p["user_id"] == 42


def test_standard_record_attrs_not_in_payload(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        p = _payload(mock_post, h)

    assert "lineno" not in p
    assert "thread" not in p
    assert "name" not in p


def test_private_attrs_not_in_payload(make_handler):
    h = make_handler()
    record = _make_record()
    record.__dict__["_secret"] = "hidden"

    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        assert "_secret" not in _payload(mock_post, h)


# ---------------------------------------------------------------------------
# Custom identity fields
# ---------------------------------------------------------------------------

def test_custom_fields_in_payload(make_handler):
    h = make_handler(
        container_key="c1", customer_key="cust1", database="mydb",
        database_type="postgres", table="orders", executable_key="etl-job",
    )
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        p = _payload(mock_post, h)

    assert p["containerKey"] == "c1"
    assert p["customerKey"] == "cust1"
    assert p["database"] == "mydb"
    assert p["databaseType"] == "postgres"
    assert p["table"] == "orders"
    assert p["executablekey"] == "etl-job"


def test_custom_fields_from_env_vars(monkeypatch, make_handler):
    monkeypatch.setenv("LOGGING_CONTAINER_KEY", "env-container")
    monkeypatch.setenv("LOGGING_DATABASE", "env-db")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        p = _payload(mock_post, h)

    assert p["containerKey"] == "env-container"
    assert p["database"] == "env-db"


def test_explicit_custom_field_takes_precedence_over_env(monkeypatch, make_handler):
    monkeypatch.setenv("LOGGING_DATABASE", "env-db")
    h = make_handler( database="explicit-db")
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert _payload(mock_post, h)["database"] == "explicit-db"


def test_empty_custom_fields_not_in_payload(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        p = _payload(mock_post, h)

    assert "containerKey" not in p
    assert "database" not in p


def test_whitespace_only_custom_field_env_var_ignored(monkeypatch, make_handler):
    monkeypatch.setenv("LOGGING_DATABASE", "   ")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert "database" not in _payload(mock_post, h)


# ---------------------------------------------------------------------------
# Site validation (task 25)
# ---------------------------------------------------------------------------

def test_invalid_site_emits_warning():
    with pytest.warns(UserWarning, match="does not look like a valid hostname"):
        h = dd_handler("a" * 32, site="not a hostname!")
    h.close()


def test_valid_site_no_warning():
    for s in ("datadoghq.eu", "us3.datadoghq.com", "ddog-gov.com"):
        h = dd_handler("a" * 32, site=s)
        h.close()


# ---------------------------------------------------------------------------
# DD_SERVICE / DD_ENV / DD_VERSION env vars (task 26)
# ---------------------------------------------------------------------------

def test_service_from_dd_service_env(monkeypatch, make_handler):
    monkeypatch.setenv("DD_SERVICE", "env-service")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert _payload(mock_post, h)["service"] == "env-service"


def test_explicit_service_takes_precedence_over_dd_service(monkeypatch, make_handler):
    monkeypatch.setenv("DD_SERVICE", "env-service")
    h = make_handler( service="explicit-service")
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert _payload(mock_post, h)["service"] == "explicit-service"


def test_version_from_dd_version_env(monkeypatch, make_handler):
    monkeypatch.setenv("DD_VERSION", "1.2.3")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert _payload(mock_post, h)["version"] == "1.2.3"


def test_version_not_in_payload_when_unset(make_handler):
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert "version" not in _payload(mock_post, h)


# ---------------------------------------------------------------------------
# Whitespace stripping (task 27)
# ---------------------------------------------------------------------------

def test_whitespace_api_key_treated_as_empty(monkeypatch):
    monkeypatch.setenv("DD_API_KEY", "   ")
    with pytest.warns(UserWarning, match="api_key"):
        h = dd_handler()
    h.close()


def test_whitespace_dd_service_treated_as_unset(monkeypatch, make_handler):
    monkeypatch.setenv("DD_SERVICE", "   ")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert "service" not in _payload(mock_post, h)


# ---------------------------------------------------------------------------
# User-Agent header (task 31)
# ---------------------------------------------------------------------------

def test_user_agent_header_sent(make_handler):
    h = make_handler()
    assert "simple-log-handlers/" in h._client.headers["user-agent"]


# ---------------------------------------------------------------------------
# Payload shape
# ---------------------------------------------------------------------------

def test_payload_is_a_json_list(make_handler):
    h = make_handler( compress=False)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        body = mock_post.call_args[1]["content"]
        assert isinstance(json.loads(body), list)


# ---------------------------------------------------------------------------
# Public DatadogHandler alias (task 35)
# ---------------------------------------------------------------------------

def test_datadog_handler_importable_from_package():
    from simple_log_handlers import DatadogHandler
    assert DatadogHandler is _DatadogHandler


def test_level_from_dd_log_level_env(monkeypatch, make_handler):
    monkeypatch.setenv("DD_LOG_LEVEL", "DEBUG")
    h = make_handler()
    assert h.level == logging.DEBUG


def test_explicit_level_takes_precedence_over_env(monkeypatch, make_handler):
    monkeypatch.setenv("DD_LOG_LEVEL", "DEBUG")
    h = make_handler( level=logging.ERROR)
    assert h.level == logging.ERROR


def test_invalid_dd_log_level_env_falls_back_to_warning(monkeypatch, make_handler):
    monkeypatch.setenv("DD_LOG_LEVEL", "NONSENSE")
    h = make_handler()
    assert h.level == logging.WARNING


# ---------------------------------------------------------------------------
# __repr__ redacts api_key (task 33)
# ---------------------------------------------------------------------------

def test_repr_redacts_api_key(make_handler):
    key = "abcdef01" * 4
    h = make_handler(key)
    r = repr(h)
    assert "abcd***" in r
    assert key not in r


def test_repr_shows_service_and_level(make_handler):
    h = make_handler( service="myapp", level=logging.ERROR)
    r = repr(h)
    assert "myapp" in r
    assert "ERROR" in r


# ---------------------------------------------------------------------------
# Arbitrary attributes dict (task 36)
# ---------------------------------------------------------------------------

def test_arbitrary_attributes_in_payload(make_handler):
    h = make_handler( attributes={"region": "eu-west-1", "team": "backend"})
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        p = _payload(mock_post, h)

    assert p["region"] == "eu-west-1"
    assert p["team"] == "backend"


def test_attributes_dict_cannot_overwrite_reserved_keys(make_handler):
    h = make_handler( service="real", attributes={"service": "spoofed"})
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert _payload(mock_post, h)["service"] == "real"


def test_whitespace_values_in_attributes_dict_dropped(make_handler):
    h = make_handler( attributes={"region": "  "})
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert "region" not in _payload(mock_post, h)


# ---------------------------------------------------------------------------
# Shared LOGGING_* env vars
# ---------------------------------------------------------------------------

def test_service_from_logging_service_env(monkeypatch, make_handler):
    monkeypatch.setenv("LOGGING_SERVICE", "shared-service")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert _payload(mock_post, h)["service"] == "shared-service"


def test_dd_service_takes_precedence_over_logging_service(monkeypatch, make_handler):
    monkeypatch.setenv("DD_SERVICE", "dd-service")
    monkeypatch.setenv("LOGGING_SERVICE", "shared-service")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert _payload(mock_post, h)["service"] == "dd-service"


def test_env_from_logging_env_env_var(monkeypatch, make_handler):
    monkeypatch.setenv("LOGGING_ENV", "shared-env")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert "env:shared-env" in _payload(mock_post, h)["ddtags"]


def test_dd_env_takes_precedence_over_logging_env(monkeypatch, make_handler):
    monkeypatch.setenv("DD_ENV", "dd-env")
    monkeypatch.setenv("LOGGING_ENV", "shared-env")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert "env:dd-env" in _payload(mock_post, h)["ddtags"]


def test_version_from_logging_version_env(monkeypatch, make_handler):
    monkeypatch.setenv("LOGGING_VERSION", "9.9.9")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert _payload(mock_post, h)["version"] == "9.9.9"


def test_dd_version_takes_precedence_over_logging_version(monkeypatch, make_handler):
    monkeypatch.setenv("DD_VERSION", "1.0.0")
    monkeypatch.setenv("LOGGING_VERSION", "9.9.9")
    h = make_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        assert _payload(mock_post, h)["version"] == "1.0.0"


# ---------------------------------------------------------------------------
# Localhost suppression
# ---------------------------------------------------------------------------

def test_localhost_hostname_suppresses_delivery():
    with patch("simple_log_handlers._dd.socket.gethostname", return_value="localhost"):
        with pytest.warns(UserWarning, match="local/loopback"):
            h = dd_handler("a" * 32)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        mock_post.assert_not_called()
    h.close()


def test_loopback_ip_suppresses_delivery():
    with patch("simple_log_handlers._dd.socket.gethostname", return_value="127.0.0.1"):
        with pytest.warns(UserWarning, match="local/loopback"):
            h = dd_handler("a" * 32)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        mock_post.assert_not_called()
    h.close()


def test_send_localhost_logs_true_bypasses_suppression():
    with patch("simple_log_handlers._dd.socket.gethostname", return_value="localhost"):
        h = dd_handler("a" * 32, send_localhost_logs=True)
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        mock_post.assert_called_once()
    h.close()


def test_non_local_hostname_not_suppressed(make_handler):
    h = make_handler( hostname="prod-server-01")
    assert not h._suppressed


def test_real_hostname_not_treated_as_local(make_handler):
    # A hostname like "myserver.local" should NOT be suppressed —
    # .local is explicitly excluded from the check.
    h = make_handler( hostname="myserver.local")
    assert not h._suppressed


# ---------------------------------------------------------------------------
# include_logger_name / executablekey logic
# ---------------------------------------------------------------------------

from simple_log_handlers._dd import _executable_key_var


def test_static_executable_key_not_overridden_by_logger_name(make_handler):
    # include_logger_name=False (default): logger name must never stomp the static key.
    h = make_handler( executable_key="my-etl")
    record = _make_record()
    record.name = "prefect.flow_run"
    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        assert _payload(mock_post, h)["executablekey"] == "my-etl"


def test_ctx_key_overrides_static_executable_key(make_handler):
    h = make_handler( executable_key="static-key")
    token = _executable_key_var.set("dynamic-key")
    try:
        with patch.object(h._client, "post") as mock_post:
            h.emit(_make_record())
            assert _payload(mock_post, h)["executablekey"] == "dynamic-key"
    finally:
        _executable_key_var.reset(token)


def test_ctx_key_alone_no_logger_name_appended(make_handler):
    # Default include_logger_name=False: ctx_key must not gain "::logger.name".
    h = make_handler()
    record = _make_record()
    record.name = "prefect.flow_run"
    token = _executable_key_var.set("my_flow")
    try:
        with patch.object(h._client, "post") as mock_post:
            h.emit(record)
            assert _payload(mock_post, h)["executablekey"] == "my_flow"
    finally:
        _executable_key_var.reset(token)


def test_include_logger_name_true_combines_ctx_key_and_log_name(make_handler):
    h = make_handler( include_logger_name=True)
    record = _make_record()
    record.name = "my_module.helper"
    token = _executable_key_var.set("my_function")
    try:
        with patch.object(h._client, "post") as mock_post:
            h.emit(record)
            assert _payload(mock_post, h)["executablekey"] == "my_function::my_module.helper"
    finally:
        _executable_key_var.reset(token)


def test_include_logger_name_true_uses_log_name_as_fallback(make_handler):
    # No static key, no ctx_key → log_name is the fallback executablekey.
    h = make_handler( include_logger_name=True)
    record = _make_record()
    record.name = "my_module.helper"
    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        assert _payload(mock_post, h)["executablekey"] == "my_module.helper"


def test_include_logger_name_true_log_name_does_not_override_static_key(make_handler):
    # With static key set and no ctx_key, log_name must not stomp it.
    h = make_handler( executable_key="my-etl", include_logger_name=True)
    record = _make_record()
    record.name = "some.logger"
    with patch.object(h._client, "post") as mock_post:
        h.emit(record)
        assert _payload(mock_post, h)["executablekey"] == "my-etl"


# ---------------------------------------------------------------------------
# API key validation and suppression (blocking fix)
# ---------------------------------------------------------------------------

def test_malformed_api_key_warns():
    with pytest.warns(UserWarning, match="does not look like a valid Datadog API key"):
        h = dd_handler("not-a-valid-key")
    h.close()


def test_malformed_api_key_suppresses_delivery():
    with pytest.warns(UserWarning):
        h = dd_handler("not-a-valid-key")
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        mock_post.assert_not_called()
    h.close()


def test_empty_api_key_suppresses_delivery(monkeypatch):
    monkeypatch.delenv("DD_API_KEY", raising=False)
    with pytest.warns(UserWarning):
        h = dd_handler()
    with patch.object(h._client, "post") as mock_post:
        h.emit(_make_record())
        h.flush()
        mock_post.assert_not_called()
    h.close()


def test_valid_32_hex_key_not_suppressed(make_handler):
    h = make_handler("a" * 32)
    assert not h._suppressed


def test_stop_event_set_on_close(make_handler):
    # close() must set _stop_event so any in-progress backoff wait returns immediately.
    h = make_handler()
    assert not h._stop_event.is_set()
    h.close()
    assert h._stop_event.is_set()
