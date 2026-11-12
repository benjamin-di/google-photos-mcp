"""Rich retains authority over traceback disclosure, filtering and output routing."""

import importlib.util
import io
import json
import logging
import sys

import pytest
from rich.console import Console
from rich.logging import RichHandler

from photos_mcp import logs


@pytest.fixture
def isolated_logging(monkeypatch):
    primary = logging.getLogger("photos_mcp.synthetic_policy")
    secondary = logging.getLogger("photos_mcp.synthetic_other")
    registered = [
        logging.getLogger(),
        *(item for item in logging.Logger.manager.loggerDict.copy().values() if isinstance(item, logging.Logger)),
    ]
    states = {logger: (logger.handlers[:], logger.level, logger.propagate, logger.disabled) for logger in registered}
    formatters = {handler: handler.formatter for logger in registered for handler in logger.handlers}
    fallback = logging.lastResort
    if fallback is not None:
        formatters[fallback] = fallback.formatter
    logging.getLogger().handlers = [logging.NullHandler()]
    for logger in (primary, secondary):
        logger.handlers, logger.propagate, logger.disabled = [], False, False
        logger.setLevel(logging.DEBUG)
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    yield primary, secondary
    for logger, (handlers, level, propagate, disabled) in states.items():
        logger.handlers, logger.propagate, logger.disabled = handlers, propagate, disabled
        logger.setLevel(level)
    for handler, formatter in formatters.items():
        handler.setFormatter(formatter)
    logging.lastResort = fallback


@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("suppress", [False, True])
@pytest.mark.parametrize("rich_tracebacks", [False, True])
@pytest.mark.parametrize("with_formatter", [False, True])
def test_rich_traceback_rendering_survives_identity(
    tmp_path, isolated_logging, fallback, suppress, rich_tracebacks, with_formatter
):
    source = tmp_path / "synthetic_failure.py"
    source.write_text(
        "\n".join(
            [f"def frame_{index}():\n    return frame_{index + 1}()" for index in range(8)]
            + ["def frame_8():\n    raise ValueError('safe-error')  # private-source-sentinel"]
        )
        + "\n"
    )
    spec = importlib.util.spec_from_file_location("synthetic_failure", source)
    synthetic = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(synthetic)
    try:
        synthetic.frame_0()
    except ValueError:
        exc_info = sys.exc_info()
    logger, _ = isolated_logging
    stream = io.StringIO()
    console = Console(file=stream, width=160, color_system=None)
    original = RichHandler(
        console=console,
        show_time=False,
        show_level=False,
        show_path=False,
        rich_tracebacks=rich_tracebacks,
        tracebacks_suppress=[str(tmp_path)] if suppress else [],
        tracebacks_max_frames=3,
    )
    if with_formatter:
        original.setFormatter(logging.Formatter("safe:%(message)s"))

    def record():
        event = logging.LogRecord(logger.name, logging.ERROR, "", 0, "retained", (), exc_info)
        event.private_extra = "private-extra-sentinel"
        return event

    with console.capture() as captured:
        original.handle(record())
    expected = captured.get().rstrip("\n")
    assert "ValueError: safe-error" in expected
    if rich_tracebacks:
        assert "frame_4" not in expected
        assert ("private-source-sentinel" not in expected) is suppress
    if fallback:
        logger.handlers = []
        logging.lastResort = original
    else:
        logger.handlers = [original]
    logs.setup()
    logs.setup()
    if fallback:
        assert not logger.handlers and not logger.propagate
    logger.handle(record())
    retained = json.loads(stream.getvalue())
    if rich_tracebacks:
        if suppress:
            assert "private-source-sentinel" not in stream.getvalue()
        assert "frame_4" not in stream.getvalue()
    assert retained["msg"] == expected
    assert retained["component"] == logger.name and retained["source_revision"]
    assert "private-extra-sentinel" not in stream.getvalue()


def test_rich_filters_shared_identity_quiet_and_dynamic_stream_survive(isolated_logging):
    logger, sibling = isolated_logging
    stream = io.StringIO()
    console = Console(file=stream, width=120)
    original = RichHandler(console=console, show_time=False, show_level=False, show_path=False)
    original.setLevel(logging.ERROR)
    filtered = []

    def keep(record):
        filtered.append(record.msg)
        return record.msg != "filtered"

    original.addFilter(keep)
    original.setFormatter(logging.Formatter("safe:%(message)s"))
    logger.handlers = [original]
    sibling.handlers = [original]
    logs.setup()
    adopted = logger.handlers[0]
    logs.setup()
    assert logger.handlers == sibling.handlers == [adopted]
    assert adopted.level == logging.ERROR and adopted.filters == [keep]
    logger.warning("below threshold")
    logger.error("filtered")
    console.quiet = True
    logger.error("quiet")
    assert not stream.getvalue()
    console.quiet = False
    logger.error("first")
    first = stream.getvalue()
    assert json.loads(first)["msg"].strip() == "safe:first"
    replacement = io.StringIO()
    console.file = replacement
    sibling.error("second")
    assert stream.getvalue() == first
    assert json.loads(replacement.getvalue())["msg"].strip() == "safe:second"
    assert filtered == ["filtered", "quiet", "first", "second"]


@pytest.mark.parametrize("failure", [OSError, RecursionError])
def test_rich_sink_failure_follows_logging_policy(isolated_logging, monkeypatch, failure):
    class BrokenStream(io.StringIO):
        def write(self, text):
            raise failure("synthetic sink failure")

    logger, _ = isolated_logging
    original = RichHandler(console=Console(file=BrokenStream()))
    failures = []
    monkeypatch.setattr(original, "handleError", failures.append)
    baseline = logging.LogRecord("synthetic.native", logging.ERROR, __file__, 1, "retained", (), None)
    original.handle(baseline)
    assert failures == [baseline]
    failures.clear()
    logger.handlers = [original]
    monkeypatch.setattr(logging, "raiseExceptions", False)
    logs.setup()
    logger.error("retained")
    assert len(failures) == 1
    assert failures[0].getMessage() == "retained"
