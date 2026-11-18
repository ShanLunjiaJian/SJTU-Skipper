from __future__ import annotations

import faulthandler
import logging
import signal
import sys
from enum import StrEnum


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def parse_log_level(value: LogLevel | str) -> LogLevel:
    """Accept ``debug``/``DEBUG``/``LogLevel.DEBUG`` and reject anything else."""

    if isinstance(value, LogLevel):
        return value
    try:
        return LogLevel(str(value).strip().upper())
    except ValueError as exc:
        names = ", ".join(level.value.lower() for level in LogLevel)
        raise ValueError(
            f"unknown log level {value!r}; expected one of {names}"
        ) from exc


_CONFIGURED = False


def setup_logging(level: LogLevel = LogLevel.INFO) -> None:
    global _CONFIGURED
    # SIGUSR1 dumps all thread stacks for blocked-worker diagnosis.
    try:
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except Exception:  # noqa: BLE001 - not supported on every platform
        pass
    root = logging.getLogger("lecture_agent")
    root.setLevel(level.value)
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(handler)
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str = "lecture_agent") -> logging.Logger:
    return logging.getLogger(name)
