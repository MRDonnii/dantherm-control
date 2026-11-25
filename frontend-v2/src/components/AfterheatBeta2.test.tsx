// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ControlReplay } from "./ControlReplay";
import { AfterheatScale } from "./AfterheatScale";
const api = vi.hoisted(() => ({ postJson: vi.fn(), requestJson: vi.fn() }));
vi.mock("../lib/api", () => api);
vi.mock("./HistoryChart", () => ({ HistoryChart: ({ title }: { title?: string }) => <div data-testid="chart">{title}</div> }));
afterEach(() => { cleanup(); vi.resetAllMocks(); });

const metrics = (changes: number, error: number) => ({ level_changes: changes, level_changes_per_hour: changes / 6, mean_abs_error: error,
  in_band_percent: 50, max_over: 1, max_under: 1 });

it("replays the afterheat target, band and dwell without saving anything", async () => {
  api.postJson.mockResolvedValue({ scope: "afterheat", covered_minutes: 359, frost_minutes: 0, live_mode: "native",
    points: [{ ts: 1, target: 22, proposed_target: 22.5, actual_air: 22, actual_level: 2, current_air: 22, current_level: 2, proposed_air: 22, proposed_level: 2 }],
    summary: { actual: metrics(6, 0.8), current: metrics(30, 0.4), proposed: metrics(12, 0.6) },
    model: { steps_k: [1.9, 4, 1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 1.5, 1.5], step_sources: ["history", "learned", "prior", "prior", "prior", "prior", "prior", "prior", "prior", "prior"], reference_fan_percent: 27 },
    note: "Vejledende." });
  render(<ControlReplay co2Default={1000} rhDefault={55} csrf="csrf" afterheat={{ band: 0.5, dwell: 3, mode: "native" }}/>);
  fireEvent.change(screen.getByLabelText("Prøvetilstand"), { target: { value: "afterheat" } });
  expect(screen.queryByLabelText("CO₂-grænse (ppm)")).toBeNull();
  expect((screen.getByLabelText("Komfortbånd (± K)") as HTMLInputElement).value).toBe("0.5");
  fireEvent.change(screen.getByLabelText("Ændring af målet (K)"), { target: { value: "0.5" } });
  fireEvent.change(screen.getByLabelText("Komfortbånd (± K)"), { target: { value: "1" } });
  fireEvent.change(screen.getByLabelText("Mindste tid pr. trin (min)"), { target: { value: "5" } });
  fireEvent.click(screen.getByRole("button", { name: "Sammenlign uden at ændre" }));
  await waitFor(() => expect(api.postJson).toHaveBeenCalledOnce());
  expect(api.postJson).toHaveBeenCalledWith("/api/controller/replay", { mode: "afterheat", range: "6h",
    settings: { afterheat_target_offset: 0.5, afterheat_pi_band: 1, afterheat_pi_min_dwell_ticks: 5 } }, "csrf", 120000);
  const table = await screen.findByRole("table", { name: "Eftervarme: faktisk, nuværende og forslag" });
  const row = within(table).getByText("Trinskift pr. time").closest("tr")!;
  expect(row.textContent).toContain("5,0");
  expect(row.textContent).toContain("2,0");
  expect(screen.getByText(/ikke slået til nu/)).toBeTruthy();
  expect(screen.getByText(/1→2 4,0 K \(lært\)/)).toBeTruthy();
  expect(screen.getAllByTestId("chart")).toHaveLength(2);
});

it("does not allow a replay outside the limits", () => {
  render(<ControlReplay co2Default={1000} rhDefault={55} csrf="csrf" afterheat={{ band: 0.5, dwell: 3, mode: "pi" }}/>);
  fireEvent.change(screen.getByLabelText("Prøvetilstand"), { target: { value: "afterheat" } });
  fireEvent.change(screen.getByLabelText("Komfortbånd (± K)"), { target: { value: "3" } });
  expect((screen.getByRole("button", { name: "Sammenlign uden at ændre" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.change(screen.getByLabelText("Komfortbånd (± K)"), { target: { value: "1" } });
  fireEvent.change(screen.getByLabelText("Mindste tid pr. trin (min)"), { target: { value: "2.5" } });
  expect((screen.getByRole("button", { name: "Sammenlign uden at ændre" }) as HTMLButtonElement).disabled).toBe(true);
});

const level = (n: number, extra: object = {}) => ({ level: n, valve_percent: n * 10, minutes: 0, air: null, lift: null, step_k: null,
  return_temperature: null, flow_temperature: null, ...extra });

it("shows the measured step size and return water per level for the chosen fan", async () => {
  api.requestJson.mockImplementation(async (endpoint: string) => ({ hours: endpoint.includes("hours=72") ? 72 : 168, settle_minutes: 3, min_points: 3, before_source: "estimated",
    fans: [{ fan_percent: 36, minutes: 900, levels: [level(0), level(1, { minutes: 800, air: 22.9, lift: 3.2, return_temperature: 27.7, flow_temperature: 44 }),
      level(2, { minutes: 100, air: 25.4, lift: 5.6, step_k: 2.4, return_temperature: 25.1, flow_temperature: 44 })] },
    { fan_percent: 50, minutes: 50, levels: [level(0), level(1, { minutes: 50, air: 21, lift: 2.1, return_temperature: 26, flow_temperature: 44 })] }] }));
  render(<AfterheatScale/>);
  const table = await screen.findByRole("table", { name: "Trinstørrelse pr. trin" });
  expect(api.requestJson).toHaveBeenCalledWith("/api/afterheat/scale?hours=168", { timeoutMs: 20000 });
  const rows = within(table).getAllByRole("row");
  expect(rows).toHaveLength(3);
  expect(rows[2].textContent).toContain("2");
  expect(rows[2].textContent).toContain("+2,4 K/trin");
  expect(rows[2].textContent).toContain("25,1 °C");
  expect(screen.getByText(/anslået ud fra ude-/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Blæserhastighed"), { target: { value: "50" } });
  expect(within(screen.getByRole("table", { name: "Trinstørrelse pr. trin" })).getAllByRole("row")).toHaveLength(2);
  fireEvent.change(screen.getByLabelText("Periode"), { target: { value: "72" } });
  await waitFor(() => expect(api.requestJson).toHaveBeenLastCalledWith("/api/afterheat/scale?hours=72", { timeoutMs: 20000 }));
  expect(screen.getByText("Reference: RLV-skala målt i november 2026")).toBeTruthy();
});
