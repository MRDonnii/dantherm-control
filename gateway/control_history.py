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

class ControlHistory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.available, self.error, self.last_recorded_at = False, None, None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS observations (ts INTEGER PRIMARY KEY, payload TEXT NOT NULL)')
            self.available = True
        except (OSError, sqlite3.Error) as error:
            self.error = str(error)
            LOG.warning('Control replay recording unavailable: %s', error)

    def record(self, observation: dict, now: float | None = None):
        if not self.available:
            return
        now = time.time() if now is None else now
        if not math.isfinite(now):
            return
        payload = json.dumps(observation, separators=(',', ':'), allow_nan=False)
        if len(payload.encode()) > MAX_PAYLOAD_BYTES:
            LOG.warning('Control replay observation too large; skipped')
            return
        with self.lock:
            if self.last_recorded_at is not None and now - self.last_recorded_at < 60:
                return
            try:
                with closing(sqlite3.connect(self.path, timeout=1)) as db, db:
                    db.execute('INSERT OR REPLACE INTO observations VALUES (?, ?)', (int(now), payload))
                    db.execute('DELETE FROM observations WHERE ts < ?', (int(now) - 3*86400,))
                    db.execute('DELETE FROM observations WHERE ts NOT IN (SELECT ts FROM observations ORDER BY ts DESC LIMIT ?)', (MAX_RECORDS,))
                self.last_recorded_at = now
            except sqlite3.Error as error:
                self.available, self.error = False, str(error)
                LOG.warning('Control replay recording disabled: %s', error)

    def query(self, span: int) -> list[dict]:
        if not self.available:
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
                'error': self.error, 'sample_seconds': 60, 'retention_days': 3}
