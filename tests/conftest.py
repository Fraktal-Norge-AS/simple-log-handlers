import pytest

_DD_ENV_VARS = [
    "DD_API_KEY", "DD_SITE", "DD_HOSTNAME",
    "DD_SERVICE", "DD_ENV", "DD_VERSION", "DD_LOG_LEVEL",
]

_LOGGING_ENV_VARS = [
    # Shared across all handlers
    "LOGGING_SERVICE", "LOGGING_ENV", "LOGGING_VERSION", "LOGGING_HOSTNAME",
    # Identity fields
    "LOGGING_CONTAINER_KEY", "LOGGING_CUSTOMER_KEY", "LOGGING_DATABASE",
    "LOGGING_DATABASE_TYPE", "LOGGING_TABLE", "LOGGING_EXECUTABLE_KEY",
]

_OTEL_ENV_VARS = [
    "OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
    "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_EXPORTER_OTLP_COMPRESSION",
    "OTEL_EXPORTER_OTLP_TIMEOUT",
    "OTEL_SERVICE_NAME", "OTEL_LOG_LEVEL",
]

_COLOR_ENV_VARS = ["NO_COLOR", "FORCE_COLOR"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove all env vars this library reads so developer shell state cannot
    affect test results or leak real credentials into the test suite."""
    for var in _DD_ENV_VARS + _LOGGING_ENV_VARS + _OTEL_ENV_VARS + _COLOR_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
