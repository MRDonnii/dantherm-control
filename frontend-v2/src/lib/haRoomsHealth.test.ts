import { expect, it } from "vitest";
import { haRoomsStatus } from "./haRoomsHealth";

it("does not call an old measurement over a fresh connection fresh", () => {
  expect(haRoomsStatus({ state: "stale", fresh: false, connection_online: true, fresh_count: 0, stale_count: 1, unknown_age_count: 0 }))
    .toEqual({ value: "1 for gammel", tone: "warn" });
});

it("shows a mix of fresh and old measurements with counts", () => {
  expect(haRoomsStatus({ state: "mixed", fresh: false, connection_online: true, fresh_count: 5, stale_count: 2, unknown_age_count: 1 }))
    .toEqual({ value: "5 friske · 2 for gamle · 1 ukendt alder", tone: "warn" });
});

it("shows unknown age as unknown, not as fresh or old", () => {
  expect(haRoomsStatus({ state: "unknown", fresh: null, connection_online: true, fresh_count: 3, stale_count: 0, unknown_age_count: 2 }))
    .toEqual({ value: "3 friske · 2 ukendt alder", tone: "neutral" });
});

it("shows fresh measurements, no rooms, no measurements and a lost connection", () => {
  expect(haRoomsStatus({ state: "fresh", fresh: true, fresh_count: 1, stale_count: 0, unknown_age_count: 0 })).toEqual({ value: "1 frisk", tone: "ok" });
  expect(haRoomsStatus({ state: "no_rooms", fresh: null })).toEqual({ value: "Ingen HA-rum", tone: "neutral" });
  expect(haRoomsStatus({ state: "no_measurements", fresh: null })).toEqual({ value: "Ingen målinger", tone: "neutral" });
  expect(haRoomsStatus({ state: "offline", fresh: false })).toEqual({ value: "Ingen forbindelse", tone: "warn" });
});

it("still reads an older gateway that only sends fresh", () => {
  expect(haRoomsStatus({ fresh: true })).toEqual({ value: "Frisk", tone: "ok" });
  expect(haRoomsStatus({ fresh: false })).toEqual({ value: "Mangler eller for gammel", tone: "warn" });
  expect(haRoomsStatus(undefined)).toEqual({ value: "Mangler eller for gammel", tone: "warn" });
});

it("shows a selected HA sensor without data as missing and never as fresh", () => {
  expect(haRoomsStatus({ state: "mixed", fresh: false, connection_online: true, fresh_count: 4, stale_count: 0, unknown_age_count: 0, missing_count: 1 }))
    .toEqual({ value: "4 friske · 1 mangler", tone: "warn" });
  expect(haRoomsStatus({ state: "missing", fresh: false, connection_online: true, fresh_count: 0, stale_count: 0, unknown_age_count: 0, missing_count: 2 }))
    .toEqual({ value: "2 mangler", tone: "warn" });
  // No sensor selected stays neutral, and an older gateway without the count is unchanged.
  expect(haRoomsStatus({ state: "no_measurements", fresh: null, missing_count: 0 })).toEqual({ value: "Ingen målinger", tone: "neutral" });
  expect(haRoomsStatus({ state: "fresh", fresh: true, fresh_count: 2, stale_count: 0, unknown_age_count: 0 })).toEqual({ value: "2 friske", tone: "ok" });
});
