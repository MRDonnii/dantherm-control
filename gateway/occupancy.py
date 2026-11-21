"""Occupancy of the rooms from Home Assistant's presence sensors, with hold times.

mmWave sensors flap: a room only counts as occupied once presence has been
seen continuously for ``on_minutes`` and is released after ``off_minutes``
without presence. Rooms whose presence is unknown (no sensor, sensor
unavailable or the HA lease expired) never count.

Kept free of the controller so the rules are easy to read and test.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class _Room:
    present: bool | None = None
    on_since: float | None = None
    off_since: float | None = None
    occupied: bool = False
    seen_at: float = 0.0
    room_kind: str = "normal"


class OccupancyTracker:
    """Per-room presence with on/off hold times."""

    # A room not reported for this long is unknown (HA sends every minute).
    STALE_SECONDS = 600.0
    # Radar drop-outs this short do not break a presence streak.
    GAP_SECONDS = 90.0

    def __init__(self) -> None:
        self.rooms: dict[str, _Room] = {}

    def observe(self, name: str, present: bool | None, now: float, room_kind: str = "normal") -> None:
        room = self.rooms.setdefault(name, _Room())
        room.seen_at, room.room_kind, room.present = now, room_kind, present
        if present is None:
            room.on_since = room.off_since = None
        elif present:
            if room.on_since is None or (room.off_since is not None and now - room.off_since > self.GAP_SECONDS):
                room.on_since = now
            room.off_since = None
        elif room.off_since is None:
            room.off_since = now

    def forget_missing(self, names: set[str]) -> None:
        for name in list(self.rooms):
            if name not in names:
                del self.rooms[name]

    def update(self, now: float, on_minutes: float, off_minutes: float) -> dict[str, dict[str, object]]:
        """Each room's held state: occupied (bool), present (raw) and kind."""
        result: dict[str, dict[str, object]] = {}
        for name, room in self.rooms.items():
            if now - room.seen_at > self.STALE_SECONDS:
                room.present, room.on_since, room.off_since = None, None, None
            if room.present is None:
                room.occupied = False
            elif room.off_since is None:
                if room.on_since is not None and now - room.on_since >= on_minutes * 60:
                    room.occupied = True
            else:
                if now - room.off_since > self.GAP_SECONDS:
                    room.on_since = None
                if room.occupied and now - room.off_since >= off_minutes * 60:
                    room.occupied = False
            result[name] = {"occupied": room.occupied, "present": room.present, "room_kind": room.room_kind}
        return result


__all__ = ["OccupancyTracker"]
