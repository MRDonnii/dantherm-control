// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { HealthReportPage } from "./HealthReportPage";
import { recordingText } from "./DiagnosticsPage";
const api = vi.hoisted(() => ({ requestJson: vi.fn(), postJson: vi.fn() }));
vi.mock("../lib/api", () => api);
afterEach(() => { cleanup(); vi.resetAllMocks(); });

const period = { samples: 1440, gap_minutes: 45, supply_minutes: 1200, target_mean: 22, air_min: 19.5, air_max: 26.1, error_mean: 0.4,
  error_sd: 1.25, max_over: 4.1, max_under: 2.5, in_band_percent: 31, level_changes: 120, level_changes_per_hour: 5, level_changes_max_hour: 17,
  fan_changes: 8, stale_minutes: { supply_temp: 3, co2: 0 } };

it("shows the week's swings, changes, errors, restarts and stale measurements", async () => {
  api.requestJson.mockResolvedValue({ generated_at: 1795540000, since: 1794935200, days: 7, total: period,
    per_day: [{ date: "2026-11-24", ...period }],
    command_errors: { alarms: 2, recent: [{ time: 1795500000, text: "En styrekommando er ikke gennemført" }], pending_now: ["fan_level"] },
    watchdog_restarts: { count: 1, recent: [{ at: 1795400000, reason: "regulator stalled" }] },
    stale: { sensors: { supply_temp: "Indblæsning (T2AH)", co2: "CO₂" }, alarms: 0, now: [] }, note: "Beregnet på gemte målinger." });
  render(<HealthReportPage/>);
  const supply = await screen.findByRole("article", { name: "Indblæsning mod målet" });
  expect(api.requestJson).toHaveBeenCalledWith("/api/health/weekly", { timeoutMs: 20000 });
  expect(within(supply).getByText("1,25 K")).toBeTruthy();
  expect(within(supply).getByText("19,5 °C / 26,1 °C")).toBeTruthy();
  const changes = screen.getByRole("article", { name: "Trinskift" });
  expect(within(changes).getByText("17")).toBeTruthy();
  const errors = screen.getByRole("article", { name: "Fejl og genstarter" });
  expect(within(errors).getByText("fan_level")).toBeTruthy();
  expect(within(errors).getByText("regulator stalled")).toBeTruthy();
  const stale = screen.getByRole("article", { name: "For gamle målinger" });
  expect(within(stale).getByText("3 min")).toBeTruthy();
  expect(screen.getAllByText("45 min")).toHaveLength(2);
});

it("shows HA rooms with old measurements by name and with counts", async () => {
  api.requestJson.mockResolvedValue({ generated_at: 1795540000, since: 1794935200, days: 7, total: period, per_day: [],
    command_errors: { alarms: 0, recent: [], pending_now: [] }, watchdog_restarts: { count: 0, recent: [] },
    stale: { sensors: { co2: "CO₂" }, alarms: 0, now: ["ha_rooms"],
      ha_rooms: { state: "mixed", fresh: false, connection_online: true, fresh_count: 4, stale_count: 1, unknown_age_count: 0 } }, note: "" });
  render(<HealthReportPage/>);
  const stale = await screen.findByRole("article", { name: "For gamle målinger" });
  expect(within(stale).getByText("HA-rum")).toBeTruthy();
  expect(within(stale).getByText("4 friske · 1 for gammel")).toBeTruthy();
});

it("shows an error when the report cannot be loaded", async () => {
  api.requestJson.mockRejectedValue(new Error("Din brugerrolle har ikke adgang til denne funktion"));
  render(<HealthReportPage/>);
  expect((await screen.findByRole("alert")).textContent).toContain("brugerrolle");
});

it("describes the room recording, including a pause after a failure", () => {
  expect(recordingText({ available: true, state: "recording" })).toBe("Optager");
  expect(recordingText({ available: false, state: "retrying", failures: 2, next_retry_at: 1795540000 })).toMatch(/^Pause efter fejl · prøver igen kl\. .+ \(2 fejl i træk\)$/);
  expect(recordingText({ available: true, state: "recording", recovered_at: 1795540000 })).toMatch(/^Optager · genoptaget kl\./);
  expect(recordingText({})).toBe("Afventer");
});

const report = (extra: Record<string, unknown> = {}) => ({ generated_at: 1795540000, since: 1794935200, days: 7, total: period, per_day: [],
  command_errors: { alarms: 0, recent: [], pending_now: [] }, watchdog_restarts: { count: 0, recent: [] },
  stale: { sensors: { co2: "CO₂" }, alarms: 0, now: [] }, note: "", ...extra });

it("says the data is missing instead of showing 0 gaps when recording is paused", async () => {
  api.requestJson.mockResolvedValue(report({ total: { ...period, samples: 0, gap_minutes: 0 },
    data_basis: { state: "paused", ok: false, gaps_known: false, text: "Historikken optager ikke – tallene mangler data", recording: "retrying", next_retry_at: 1795540000, error: "disk I/O error" } }));
  render(<HealthReportPage/>);
  expect((await screen.findByRole("alert")).textContent).toContain("Historikken optager ikke – tallene mangler data");
  const changes = screen.getByRole("article", { name: "Trinskift" });
  expect(within(changes).getByText("Mangler data")).toBeTruthy();
  expect(within(changes).queryByText("0 min")).toBeNull();
});

it("filters the precision by operating mode through the API", async () => {
  api.requestJson.mockResolvedValue(report({ context: "all", data_basis: { state: "ok", ok: true, gaps_known: true, text: "" } }));
  render(<HealthReportPage/>);
  await screen.findByRole("article", { name: "Indblæsning mod målet" });
  expect(screen.queryByRole("alert")).toBeNull();
  const select = screen.getByLabelText("Vurder driftstilstand") as HTMLSelectElement;
  expect(select.value).toBe("all");
  expect([...select.options].map(option => option.textContent)).toContain("Frostbeskyttelse");
  expect([...select.options].map(option => option.value)).not.toContain("4");
  fireEvent.change(select, { target: { value: "1" } });
  await waitFor(() => expect(api.requestJson).toHaveBeenLastCalledWith("/api/health/weekly?context=1", { timeoutMs: 20000 }));
});

it("shows a selected HA sensor without data as missing", async () => {
  api.requestJson.mockResolvedValue(report({ stale: { sensors: { co2: "CO₂" }, alarms: 0, now: [],
    ha_rooms: { state: "mixed", fresh: false, connection_online: true, fresh_count: 4, stale_count: 0, unknown_age_count: 0, missing_count: 1 } } }));
  render(<HealthReportPage/>);
  const stale = await screen.findByRole("article", { name: "For gamle målinger" });
  expect(within(stale).getByText("4 friske · 1 mangler")).toBeTruthy();
});
