"""Retained diagnostics preserve sinks, severity, redaction and structured values."""

from __future__ import annotations

import io
import json
import logging
import subprocess
import sys

import pytest
from rich.console import Console
from rich.logging import RichHandler

from uber_mcp import identity, logs


def event(record=None, original=None):
    return json.loads(
        logs.JsonFormatter(original).format(
            record or logging.LogRecord("uber.tools", logging.INFO, "", 0, "retained", (), None)
        )
    )


def assert_context(payload):
    assert payload.items() >= identity.log_fields().items()
    assert payload["level"] and payload["component"]


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL])
def test_retained_levels_have_trusted_context_and_typed_extras(level):
    record = logging.LogRecord("uber.tools", level, "", 0, "retained %s", ("event",), None)
    record.service = "original"
    record.version = 7
    record.instance = None
    record.source_revision = False
    record.details = {"count": 3, "ok": True, "items": [1, None]}
    payload = event(record)
    assert_context(payload)
    assert payload["level"] == logging.getLevelName(level)
    assert payload["msg"] == "retained event" and payload["component"] == "uber.tools"
    assert payload["details"] == {"count": 3, "ok": True, "items": [1, None]}
    assert payload["service_"] == "original" and payload["version_"] == 7
    assert payload["instance_"] is None and payload["source_revision_"] is False


def test_existing_json_formatter_preserves_collisions_and_types():
    original = logging.Formatter(
        '{"service":"original","service_":"existing","version":7,"component":[1,2],'
        '"level":false,"nested":{"count":3},"instance":null,"msg":"retained"}',
        validate=False,
    )
    payload = event(original=original)
    assert_context(payload)
    assert payload["service_"] == "existing" and payload["service__"] == "original"
    assert payload["version_"] == 7 and payload["component_"] == [1, 2]
    assert payload["level_"] is False and payload["instance_"] is None
    assert payload["nested"] == {"count": 3}


def test_default_formatter_preserves_stack_and_cached_exception_diagnostics():
    record = logging.LogRecord("uber", logging.DEBUG, "", 0, "retained", (), None)
    record.stack_info = "Stack (most recent call last): synthetic stack"
    record.exc_text = "synthetic cached exception"
    record.stack = {"original": True}
    record.exc = 7
    payload = event(record)
    assert_context(payload)
    assert payload["stack"] == record.stack_info and payload["stack_"] == {"original": True}
    assert payload["exc"] == record.exc_text and payload["exc_"] == 7


def test_existing_redaction_runs_before_enrichment_and_cannot_be_undone():
    class Redacted(logging.Formatter):
        def format(self, record):
            return '{"msg":"safe","count":2}'

    record = logging.LogRecord("uber", logging.ERROR, "", 0, "private-message", (), None)
    record.credential = "private-extra"
    record.exc_text = "private-trace"
    try:
        raise ValueError("private-exception")
    except ValueError:
        record.exc_info = sys.exc_info()
    payload = event(record, Redacted())
    assert_context(payload)
    assert payload["count"] == 2 and "private" not in json.dumps(payload)


@pytest.fixture
def clean_logging(monkeypatch):
    root = logging.getLogger()
    old_loggers = {
        logger: (logger.handlers[:], logger.level, logger.propagate, logger.disabled)
        for logger in [root, *(v for v in logging.Logger.manager.loggerDict.values() if isinstance(v, logging.Logger))]
    }
    fallback = logging.lastResort.formatter
    root.handlers = []
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    yield
    for logger in [root, *(v for v in logging.Logger.manager.loggerDict.values() if isinstance(v, logging.Logger))]:
        handlers, level, propagate, disabled = old_loggers.get(logger, ([], logging.NOTSET, True, False))
        logger.handlers = handlers
        logger.setLevel(level)
        logger.propagate = propagate
        logger.disabled = disabled
    logging.lastResort.setFormatter(fallback)


def test_setup_preserves_preexisting_sinks_filters_levels_and_redaction(clean_logging):
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setLevel(logging.ERROR)
    handler.setFormatter(logging.Formatter("safe %(message)s"))
    handler.addFilter(lambda record: record.msg != "filtered")
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.WARNING)
    logs.setup()
    formatter = handler.formatter
    logs.setup()
    assert root.handlers == [handler] and handler.formatter is formatter
    assert root.level == logging.WARNING and handler.level == logging.ERROR
    root.error("retained")
    root.error("filtered")
    root.warning("below threshold")
    payload = json.loads(sink.getvalue())
    assert payload["msg"] == "safe retained"
    assert_context(payload)


@pytest.mark.parametrize("name", ["asyncio", "uber", "uber_login", "signin_window", "fastmcp", "mcp.server"])
def test_dedicated_handlers_keep_debug_policy_and_propagation(clean_logging, name):
    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setFormatter(logging.Formatter("safe", validate=False))
    logger = logging.getLogger(name)
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.DEBUG)
    logs.setup()
    logger.debug("private-input")
    payload = json.loads(sink.getvalue())
    assert_context(payload)
    assert payload["level"] == "DEBUG" and payload["component"] == name
    assert payload["msg"] == "safe" and "private-input" not in sink.getvalue()
    assert logger.level == logging.DEBUG and logger.propagate is False


def test_default_setup_uses_stderr_keeps_stdout_clear_and_noisy_defaults(clean_logging, monkeypatch, capsys):
    names = ("uvicorn.access", "httpx", "httpcore", "mcp.server.lowlevel.server")
    for name in names:
        logging.getLogger(name).setLevel(logging.NOTSET)
    monkeypatch.setenv("LOG_LEVEL", "")
    logging.getLogger().handlers = []
    logs.setup()
    logging.getLogger("uber").info("retained")
    captured = capsys.readouterr()
    assert not captured.out
    assert_context(json.loads(captured.err))
    assert logging.getLogger().level == logging.INFO
    assert all(logging.getLogger(name).getEffectiveLevel() == logging.WARNING for name in names)


def test_rich_keeps_dynamic_quiet_filters_threshold_and_shared_destination(clean_logging):
    sink = io.StringIO()
    console = Console(file=sink, force_terminal=False)
    handler = RichHandler(console=console, show_time=False, show_level=False, show_path=False)
    handler.setLevel(logging.ERROR)
    handler.setFormatter(logging.Formatter("safe %(message)s"))
    handler.addFilter(lambda record: record.msg != "filtered")
    root = logging.getLogger()
    root.handlers = [handler]
    logger = logging.getLogger("uber.synthetic")
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.DEBUG)
    logs.stamp_handlers()
    replacement = root.handlers[0]
    logs.stamp_handlers()
    assert replacement is root.handlers[0] is logger.handlers[0]
    assert replacement.level == logging.ERROR
    logger.error("retained")
    logger.error("filtered")
    logger.warning("below threshold")
    console.quiet = True
    logger.error("quiet")
    console.quiet = False
    logger.error("resumed")
    payloads = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert [payload["msg"].strip() for payload in payloads] == ["safe retained", "safe resumed"]
    for payload in payloads:
        assert_context(payload)


def test_uvicorn_public_configuration_preserves_existing_handlers_and_filters(clean_logging):
    import uvicorn

    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setLevel(logging.WARNING)
    handler.setFormatter(logging.Formatter("safe %(message)s"))
    handler.addFilter(lambda record: record.msg != "filtered")
    logger = logging.getLogger("uvicorn")
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.handlers = [logging.StreamHandler(io.StringIO())]
    original_root = root.handlers[:]
    uvicorn.Config("synthetic:app", log_config=logs.uvicorn_log_config(), access_log=False)
    assert root.handlers == original_root and logger.handlers == [handler]
    assert logger.level == logging.DEBUG and logger.propagate is False and handler.level == logging.WARNING
    logging.getLogger("uvicorn.error").warning("retained")
    logging.getLogger("uvicorn.error").warning("filtered")
    payload = json.loads(sink.getvalue())
    assert_context(payload)
    assert payload["msg"] == "safe retained" and payload["component"] == "uvicorn.error"


def test_uvicorn_keeps_inherited_sink_filters_and_redaction(clean_logging, capsys):
    import uvicorn

    sink = io.StringIO()
    handler = logging.StreamHandler(sink)
    handler.setFormatter(logging.Formatter("REDACTED", validate=False))
    handler.addFilter(lambda record: record.msg != "filtered-value")
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers, logger.propagate, logger.disabled = [], True, False
        logger.setLevel(logging.NOTSET)
    logs.setup()
    uvicorn.Config("synthetic:app", log_config=logs.uvicorn_log_config(), access_log=False)
    logging.getLogger("uvicorn.error").warning("synthetic-private-value")
    logging.getLogger("uvicorn.error").warning("filtered-value")
    payload = json.loads(sink.getvalue())
    assert_context(payload)
    assert payload["msg"] == "REDACTED"
    assert not capsys.readouterr().err
    assert not logging.getLogger("uvicorn").handlers and logging.getLogger("uvicorn").propagate


def test_fresh_native_uvicorn_http_configuration_keeps_warning_policy_and_streams():
    code = """
import logging
import uvicorn
from uber_mcp import logs
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
config = uvicorn.Config("synthetic:app", log_config=logs.uvicorn_log_config(), log_level="warning")
logging.getLogger("uvicorn.error").info("suppressed")
logging.getLogger("uvicorn.error").warning("native retained")
logging.getLogger("uvicorn.access").info("suppressed access")
logging.getLogger("fastmcp").warning("sdk retained")
"""
    result = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, timeout=20)
    assert not result.stdout
    payloads = [json.loads(line) for line in result.stderr.splitlines()]
    assert len(payloads) == 2
    assert payloads[0]["msg"].endswith("native retained")
    assert payloads[0]["component"] == "uvicorn.error" and payloads[0]["level"] == "WARNING"
    assert payloads[1]["component"] == "fastmcp"
    for payload in payloads:
        assert payload["service"] == "uber" and payload["version"] == identity.build_identity().version
    assert len({payload["instance"] for payload in payloads}) == 1


def test_real_sdk_http_launch_uses_public_log_config_without_serving_network():
    code = """
import asyncio, logging
from fastmcp import FastMCP
import uvicorn
from uber_mcp import logs
logs.setup()
async def serve(self, sockets=None):
    logging.getLogger("uvicorn.error").info("native retained")
    logging.getLogger("uvicorn.access").info("suppressed access")
uvicorn.Server.serve = serve
asyncio.run(FastMCP("synthetic", version="1.2.3").run_http_async(
    show_banner=False,
    uvicorn_config={"log_config": logs.uvicorn_log_config(), "access_log": False},
))
"""
    result = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, timeout=20)
    assert not result.stdout
    payloads = [json.loads(line) for line in result.stderr.splitlines()]
    assert any(
        payload["component"].startswith("fastmcp.") and "Starting MCP server" in payload["msg"] for payload in payloads
    )
    assert any(payload["component"] == "uvicorn.error" and "native retained" in payload["msg"] for payload in payloads)
    assert all("suppressed access" not in payload["msg"] for payload in payloads)
    assert all(
        payload["service"] == "uber" and payload["version"] == identity.build_identity().version for payload in payloads
    )
    assert len({payload["instance"] for payload in payloads}) == 1
