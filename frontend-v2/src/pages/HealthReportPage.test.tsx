// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
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
