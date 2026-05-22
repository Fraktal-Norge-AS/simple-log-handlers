from __future__ import annotations

import gzip
import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from simple_log_handlers import DltJsonFilter, install_dlt_json_filter


# ---------------------------------------------------------------------------
# Shared sample — a realistic dlt JSON record
# ---------------------------------------------------------------------------

_DLT_JSON: dict[str, object] = {
    "written_at": "2026-05-22T06:23:56.267Z",
    "written_ts": 1779431036267625000,
    "process": 29240,
    "msg": "Making GET request to https://example.com",
    "type": "log",
    "logger": "dlt",
    "thread": "MainThread",
    "level": "INFO",
    "module": "client",
    "line_no": 166,
    "version": {"dlt_version": "1.24.0"},
}


def _make_record(
    msg: str | None = None,
    level: int = logging.INFO,
    name: str = "dlt",
) -> logging.LogRecord:
    return logging.LogRecord(
        name=name, level=level, pathname="", lineno=0,
        msg=msg if msg is not None else json.dumps(_DLT_JSON),
        args=(), exc_info=None,
    )


@pytest.fixture(autouse=True)
def _clean_dlt_filters() -> object:
    dlt_logger = logging.getLogger("dlt")
    original = list(dlt_logger.filters)
    dlt_logger.filters.clear()
    yield
    dlt_logger.filters.clear()
    dlt_logger.filters.extend(original)


# ---------------------------------------------------------------------------
# Basic transformation
# ---------------------------------------------------------------------------

class TestDltJsonFilter:
    def test_replaces_json_message_with_msg_field(self) -> None:
        flt = DltJsonFilter()
        record = _make_record()
        flt.filter(record)
        assert record.getMessage() == "Making GET request to https://example.com"

    def test_returns_true_for_valid_record(self) -> None:
        assert DltJsonFilter().filter(_make_record()) is True

    def test_clears_args_after_transform(self) -> None:
        flt = DltJsonFilter()
        record = _make_record()
        flt.filter(record)
        assert record.args is None

    def test_injects_dlt_module(self) -> None:
        flt = DltJsonFilter()
        record = _make_record()
        flt.filter(record)
        assert record.__dict__["dlt_module"] == "client"

    def test_injects_dlt_version(self) -> None:
        flt = DltJsonFilter()
        record = _make_record()
        flt.filter(record)
        assert record.__dict__["dlt_version"] == "1.24.0"

    def test_strips_envelope_fields(self) -> None:
        flt = DltJsonFilter()
        record = _make_record()
        flt.filter(record)
        # "thread" is a standard LogRecord attribute (thread ID), not checked here.
        for key in ("written_at", "written_ts", "type", "logger", "line_no"):
            assert key not in record.__dict__

    def test_passes_through_plain_text(self) -> None:
        flt = DltJsonFilter()
        record = _make_record("plain text message")
        assert flt.filter(record) is True
        assert record.getMessage() == "plain text message"

    def test_passes_through_json_without_msg_key(self) -> None:
        flt = DltJsonFilter()
        raw = json.dumps({"key": "value"})
        record = _make_record(raw)
        assert flt.filter(record) is True
        assert record.getMessage() == raw

    def test_passes_through_non_dict_json(self) -> None:
        flt = DltJsonFilter()
        record = _make_record("[1, 2, 3]")
        assert flt.filter(record) is True

    def test_no_dlt_module_when_module_field_absent(self) -> None:
        flt = DltJsonFilter()
        data = {**_DLT_JSON}
        del data["module"]
        flt.filter(_make_record(json.dumps(data)))
        assert "dlt_module" not in logging.LogRecord.__dict__

    def test_no_dlt_version_when_version_field_absent(self) -> None:
        flt = DltJsonFilter()
        data = {**_DLT_JSON}
        del data["version"]
        record = _make_record(json.dumps(data))
        flt.filter(record)
        assert "dlt_version" not in record.__dict__


# ---------------------------------------------------------------------------
# module_levels
# ---------------------------------------------------------------------------

class TestModuleLevels:
    def test_drops_record_below_module_threshold(self) -> None:
        flt = DltJsonFilter(module_levels={"client": logging.WARNING})
        assert flt.filter(_make_record(level=logging.INFO)) is False

    def test_passes_record_at_threshold(self) -> None:
        flt = DltJsonFilter(module_levels={"client": logging.WARNING})
        record = _make_record(level=logging.WARNING)
        assert flt.filter(record) is True
        assert record.getMessage() == "Making GET request to https://example.com"

    def test_passes_record_above_threshold(self) -> None:
        flt = DltJsonFilter(module_levels={"client": logging.WARNING})
        assert flt.filter(_make_record(level=logging.ERROR)) is True

    def test_unlisted_module_unaffected(self) -> None:
        flt = DltJsonFilter(module_levels={"client": logging.WARNING})
        data = {**_DLT_JSON, "module": "normalize"}
        assert flt.filter(_make_record(json.dumps(data), level=logging.INFO)) is True

    def test_missing_module_field_skips_level_gate(self) -> None:
        flt = DltJsonFilter(module_levels={"client": logging.WARNING})
        data = {**_DLT_JSON}
        del data["module"]
        assert flt.filter(_make_record(json.dumps(data), level=logging.INFO)) is True

    def test_plain_text_unaffected_by_module_levels(self) -> None:
        flt = DltJsonFilter(module_levels={"client": logging.ERROR})
        assert flt.filter(_make_record("plain text", level=logging.DEBUG)) is True


# ---------------------------------------------------------------------------
# include_module_in_logger_name
# ---------------------------------------------------------------------------

class TestIncludeModuleInLoggerName:
    def test_renames_record_to_dlt_dot_module(self) -> None:
        flt = DltJsonFilter(include_module_in_logger_name=True)
        record = _make_record()
        flt.filter(record)
        assert record.name == "dlt.client"

    def test_disabled_by_default(self) -> None:
        flt = DltJsonFilter()
        record = _make_record()
        flt.filter(record)
        assert record.name == "dlt"

    def test_no_rename_when_module_absent(self) -> None:
        flt = DltJsonFilter(include_module_in_logger_name=True)
        data = {**_DLT_JSON}
        del data["module"]
        record = _make_record(json.dumps(data))
        flt.filter(record)
        assert record.name == "dlt"

    def test_no_rename_for_plain_text(self) -> None:
        flt = DltJsonFilter(include_module_in_logger_name=True)
        record = _make_record("plain text")
        flt.filter(record)
        assert record.name == "dlt"

    def test_different_modules_produce_different_names(self) -> None:
        flt = DltJsonFilter(include_module_in_logger_name=True)
        for module in ("client", "normalize", "worker", "validate"):
            data = {**_DLT_JSON, "module": module}
            record = _make_record(json.dumps(data))
            flt.filter(record)
            assert record.name == f"dlt.{module}"

    def test_executablekey_composed_with_include_logger_name(self) -> None:
        """filter(include_module_in_logger_name=True) + handler(include_logger_name=True)
        produces executablekey = "<ctx>::dlt.<module>"."""
        from simple_log_handlers import dd_handler
        from simple_log_handlers._context import _executable_key_var

        flt = DltJsonFilter(include_module_in_logger_name=True)
        with patch("httpx.Client.post", return_value=MagicMock(raise_for_status=MagicMock())) as mock_post:
            h = dd_handler("a" * 32, send_localhost_logs=True, include_logger_name=True)
            try:
                token = _executable_key_var.set("my_pipeline")
                try:
                    record = _make_record()
                    flt.filter(record)
                    h.emit(record)
                    h.flush()
                finally:
                    _executable_key_var.reset(token)
                body = gzip.decompress(mock_post.call_args[1]["content"])
                payload = json.loads(body)[0]
                assert payload["executablekey"] == "my_pipeline::dlt.client"
            finally:
                h.close()

    def test_executablekey_module_only_when_no_context_key(self) -> None:
        """Without a context key, executablekey falls back to "dlt.<module>"."""
        from simple_log_handlers import dd_handler

        flt = DltJsonFilter(include_module_in_logger_name=True)
        with patch("httpx.Client.post", return_value=MagicMock(raise_for_status=MagicMock())) as mock_post:
            h = dd_handler("a" * 32, send_localhost_logs=True, include_logger_name=True)
            try:
                record = _make_record()
                flt.filter(record)
                h.emit(record)
                h.flush()
                body = gzip.decompress(mock_post.call_args[1]["content"])
                payload = json.loads(body)[0]
                assert payload["executablekey"] == "dlt.client"
            finally:
                h.close()


# ---------------------------------------------------------------------------
# install_dlt_json_filter
# ---------------------------------------------------------------------------

class TestInstallDltJsonFilter:
    def test_returns_none_without_env_var(self) -> None:
        assert install_dlt_json_filter() is None

    def test_returns_none_for_non_json_format(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DLT_LOG_FORMAT", "plain")
        assert install_dlt_json_filter() is None

    def test_installs_filter_on_dlt_logger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DLT_LOG_FORMAT", "json")
        flt = install_dlt_json_filter()
        assert isinstance(flt, DltJsonFilter)
        assert flt in logging.getLogger("dlt").filters

    def test_case_insensitive_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DLT_LOG_FORMAT", "JSON")
        assert isinstance(install_dlt_json_filter(), DltJsonFilter)

    def test_idempotent_same_object_returned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DLT_LOG_FORMAT", "json")
        flt1 = install_dlt_json_filter()
        flt2 = install_dlt_json_filter()
        assert flt1 is flt2

    def test_idempotent_no_duplicate_added(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DLT_LOG_FORMAT", "json")
        install_dlt_json_filter()
        install_dlt_json_filter()
        dlt_filters = [f for f in logging.getLogger("dlt").filters if isinstance(f, DltJsonFilter)]
        assert len(dlt_filters) == 1

    def test_existing_filter_wins_over_new_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A manually installed filter is returned unchanged on subsequent calls."""
        monkeypatch.setenv("DLT_LOG_FORMAT", "json")
        first = install_dlt_json_filter(module_levels={"client": logging.WARNING})
        second = install_dlt_json_filter(module_levels={"normalize": logging.ERROR})
        assert first is second
        assert first._module_levels == {"client": logging.WARNING}

    def test_passes_module_levels_to_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DLT_LOG_FORMAT", "json")
        flt = install_dlt_json_filter(module_levels={"client": logging.WARNING})
        assert isinstance(flt, DltJsonFilter)
        assert flt._module_levels == {"client": logging.WARNING}

    def test_passes_include_module_in_logger_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DLT_LOG_FORMAT", "json")
        flt = install_dlt_json_filter(include_module_in_logger_name=True)
        assert isinstance(flt, DltJsonFilter)
        assert flt._include_module_in_logger_name is True


# ---------------------------------------------------------------------------
# Auto-install via factory functions
# ---------------------------------------------------------------------------

class TestAutoInstall:
    def test_dd_handler_auto_installs_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from simple_log_handlers import dd_handler

        monkeypatch.setenv("DLT_LOG_FORMAT", "json")
        with patch("httpx.Client.post", return_value=MagicMock(raise_for_status=MagicMock())):
            h = dd_handler("a" * 32, send_localhost_logs=True)
            h.close()
        assert any(isinstance(f, DltJsonFilter) for f in logging.getLogger("dlt").filters)

    def test_otel_handler_auto_installs_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from simple_log_handlers import otel_handler

        monkeypatch.setenv("DLT_LOG_FORMAT", "json")
        with patch("httpx.Client.post", return_value=MagicMock(raise_for_status=MagicMock())):
            h = otel_handler("https://example.com/v1/logs", send_localhost_logs=True)
            h.close()
        assert any(isinstance(f, DltJsonFilter) for f in logging.getLogger("dlt").filters)

    def test_no_auto_install_without_env_var(self) -> None:
        from simple_log_handlers import dd_handler

        with patch("httpx.Client.post", return_value=MagicMock(raise_for_status=MagicMock())):
            h = dd_handler("a" * 32, send_localhost_logs=True)
            h.close()
        assert not any(isinstance(f, DltJsonFilter) for f in logging.getLogger("dlt").filters)

    def test_manual_install_before_factory_is_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """install_dlt_json_filter() called before dd_handler() with options keeps those options."""
        from simple_log_handlers import dd_handler

        monkeypatch.setenv("DLT_LOG_FORMAT", "json")
        manual = install_dlt_json_filter(
            module_levels={"client": logging.WARNING},
            include_module_in_logger_name=True,
        )
        with patch("httpx.Client.post", return_value=MagicMock(raise_for_status=MagicMock())):
            h = dd_handler("a" * 32, send_localhost_logs=True)
            h.close()
        dlt_filters = [f for f in logging.getLogger("dlt").filters if isinstance(f, DltJsonFilter)]
        assert len(dlt_filters) == 1
        assert dlt_filters[0] is manual
        assert dlt_filters[0]._module_levels == {"client": logging.WARNING}
        assert dlt_filters[0]._include_module_in_logger_name is True
