"""Bounded, optional raw observations for offline demand replay.

Called by dashboard-history, never the serial owner or regulator thread.
"""
from __future__ import annotations
from contextlib import closing
import json
import logging
import math
import sqlite3
import threading
import time
from pathlib import Path

LOG = logging.getLogger(__name__)
MAX_RECORDS = 3 * 24 * 60  # three days at one observation per minute
MAX_PAYLOAD_BYTES = 128 * 1024
# After a failed write the next attempt waits this long, doubling up to the maximum.
RETRY_FIRST_SECONDS = 60
RETRY_MAX_SECONDS = 30 * 60

class ControlHistory:
    """Optional recording. A failure pauses it; it is tried again with growing pauses
    (RETRY_FIRST_SECONDS doubling up to RETRY_MAX_SECONDS) and resumes by itself."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.available, self.error, self.last_recorded_at = False, None, None
        self.ready = False  # the table has been created at least once
        self.failures = 0
        self.failed_at: float | None = None
        self.retry_at: float | None = None
        self.recovered_at: float | None = None
        try:
            self._prepare()
            self.available = True
        except (OSError, sqlite3.Error) as error:
            self._fail(error, time.time())

    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS observations (ts INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
        self.ready = True

    def _fail(self, error: Exception, now: float) -> None:
        """Pause recording until the next retry; only the first failure in a row is logged."""
        self.failures += 1
        self.available, self.error, self.failed_at = False, str(error), now
        self.retry_at = now + min(RETRY_MAX_SECONDS, RETRY_FIRST_SECONDS * 2 ** min(self.failures - 1, 16))
        if self.failures == 1:
            LOG.warning('Control replay recording paused, retrying automatically: %s', error)
        else:
            LOG.debug('Control replay recording still failing (%d): %s', self.failures, error)

    def _retry_due(self, now: float) -> bool:
        # A clock set back never postpones the retry.
        return self.retry_at is None or now >= self.retry_at or (self.failed_at is not None and now < self.failed_at)

    def record(self, observation: dict, now: float | None = None):
        now = time.time() if now is None else now
        if not math.isfinite(now):
            return
        with self.lock:
            if not self.available and not self._retry_due(now):
                return
            if self.last_recorded_at is not None and 0 <= now - self.last_recorded_at < 60:
                return
        payload = json.dumps(observation, separators=(',', ':'), allow_nan=False)
        if len(payload.encode()) > MAX_PAYLOAD_BYTES:
            LOG.warning('Control replay observation too large; skipped')
            return
        with self.lock:
            if not self.available and not self._retry_due(now):
                return
            try:
                if not self.ready:
                    self._prepare()
                with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
                    db.execute('INSERT OR REPLACE INTO observations VALUES (?, ?)', (int(now), payload))
                    db.execute('DELETE FROM observations WHERE ts < ?', (int(now) - 3*86400,))
                    db.execute('DELETE FROM observations WHERE ts NOT IN (SELECT ts FROM observations ORDER BY ts DESC LIMIT ?)', (MAX_RECORDS,))
                self.last_recorded_at = now
                if not self.available:
                    LOG.warning('Control replay recording resumed after %d failed attempt(s)', self.failures)
                    self.recovered_at = now
                self.available, self.error, self.failures, self.retry_at = True, None, 0, None
            except (OSError, sqlite3.Error) as error:
                self._fail(error, now)

    def query(self, span: int) -> list[dict]:
        # Stored rows stay readable while recording is paused.
        if not self.ready:
            return []
        try:
            with self.lock, closing(sqlite3.connect(self.path, timeout=1)) as db:
                rows = db.execute('SELECT ts, payload FROM observations WHERE ts >= ? ORDER BY ts DESC LIMIT 1441',
                                  (int(time.time())-span,)).fetchall()
            result = []
            for ts, payload in reversed(rows):
                try:
                    value = json.loads(payload)
                    if isinstance(value, dict):
                        result.append({**value, 'ts': ts})
                except (TypeError, ValueError):
                    continue
            return result
        except sqlite3.Error as error:
            LOG.warning('Control replay history unavailable: %s', error)
            return []

    def status(self) -> dict:
        return {'available': self.available, 'last_recorded_at': self.last_recorded_at,
                'error': self.error, 'sample_seconds': 60, 'retention_days': 3,
                'state': 'recording' if self.available else 'retrying',
                'failures': self.failures, 'last_failure_at': self.failed_at,
                'next_retry_at': None if self.available else self.retry_at, 'recovered_at': self.recovered_at}
