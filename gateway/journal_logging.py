"""Logging setup that keeps Python log levels in journald.

journald stores every plain stderr line at priority 6 (info), so a filter
such as ``journalctl -p warning`` never showed the gateway's warnings and
errors. With ``SyslogLevelPrefix=`` on (the systemd default) journald reads
a leading ``<N>`` as the syslog priority and strips it from the message.
The prefix is only added when stderr really goes to the journal, so running
a script by hand prints clean lines.
"""
from __future__ import annotations

import logging
import os
import sys

_PRIORITY = (
    (logging.CRITICAL, 2),
    (logging.ERROR, 3),
    (logging.WARNING, 4),
    (logging.INFO, 6),
)

FORMAT = "%(asctime)s %(levelname)s %(message)s"


def syslog_priority(levelno: int) -> int:
    for threshold, priority in _PRIORITY:
        if levelno >= threshold:
            return priority
    return 7


class JournalPriorityFormatter(logging.Formatter):
    """Prefix each line (also continuation lines of a traceback) with ``<N>``."""

    def format(self, record: logging.LogRecord) -> str:
        prefix = f"<{syslog_priority(record.levelno)}>"
        return "\n".join(prefix + line for line in super().format(record).split("\n"))


def stderr_is_journal(stream=None) -> bool:
    """True when stderr is the journald stream systemd handed this process."""
    value = os.environ.get("JOURNAL_STREAM", "")
    try:
        device, inode = (int(part) for part in value.split(":", 1))
        stat = os.fstat((stream or sys.stderr).fileno())
    except (ValueError, OSError, AttributeError):
        return False
    return stat.st_dev == device and stat.st_ino == inode


def configure_logging(level: int | str = logging.INFO) -> None:
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    handler = logging.StreamHandler()
    if stderr_is_journal():
        handler.setFormatter(JournalPriorityFormatter(FORMAT))
    else:
        handler.setFormatter(logging.Formatter(FORMAT))
    logging.basicConfig(level=level, handlers=[handler])
