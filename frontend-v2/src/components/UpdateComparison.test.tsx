// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { UpdateComparison, type ComparisonResult } from "./UpdateComparison";
const api = vi.hoisted(() => ({ requestJson: vi.fn() }));
vi.mock("../lib/api", () => api);
afterEach(() => { cleanup(); vi.resetAllMocks(); });

const update = { id: "1800000000", version: "1.13.0-beta.7", previous_version: "1.13.0-beta.6", at: 1800000000 };
const side = { start: 1799913600, end: 1800000000, window_minutes: 1440, context_minutes: 1200, supply_minutes: 900,
  gap_minutes: 15, coverage_percent: 83.3, outdoor_min: -2, outdoor_mean: 1, outdoor_max: 5, target_mean: 22 };
const result = (summary = "1 bedre · 0 dårligere · 0 uændret"): ComparisonResult => ({ updates: [update],
  comparison: { update, enough: true, before: side, after: { ...side, ongoing: true }, summary,
    note: "Vejr og brug er ikke ens.", settle_minutes: 30, min_minutes: 360,
    metrics: [{ key: "error_sd", label: "Spredning", unit: "K", before: 1.2, after: 0.5, delta: -0.7,
      verdict: "better", verdict_text: "Bedre" }] }, history: { available: true } });

it("loads only when opened and explains when no previous version is known", async () => {
  api.requestJson.mockResolvedValue({ updates: [], comparison: null });
  render(<UpdateComparison context="all"/>);
  expect(api.requestJson).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Vis sammenligning" }));
  expect(await screen.findByText(/endnu ingen registreret opdatering/)).toBeTruthy();
  expect(api.requestJson).toHaveBeenCalledWith("/api/health/update-comparison?window=24h&context=all", { timeoutMs: 20000 });
});

it("shows the comparison, conditions and missing-data warning without a settings action", async () => {
  api.requestJson.mockResolvedValue({ ...result(), history: { available: false } });
  render(<UpdateComparison context="1"/>);
  fireEvent.click(screen.getByRole("button", { name: "Vis sammenligning" }));
  expect(await screen.findByRole("table", { name: "Sammenligning af reguleringskvalitet" })).toBeTruthy();
  expect(screen.getByText("Bedre")).toBeTruthy();
  expect(screen.getByRole("alert").textContent).toContain("Historikken optager ikke");
  expect(screen.getAllByText("83,3 %")).toHaveLength(2);
  expect(screen.queryByRole("button", { name: /Gem|Anvend/ })).toBeNull();
});

it("keeps the selectors and focus while new data arrives and ignores an older answer", async () => {
  api.requestJson.mockResolvedValueOnce(result());
  render(<UpdateComparison context="all"/>);
  fireEvent.click(screen.getByRole("button", { name: "Vis sammenligning" }));
  await screen.findByRole("table");
  const select = screen.getByLabelText("Sammenlign periode");
  let oldAnswer: (value: ComparisonResult) => void = () => {};
  api.requestJson.mockImplementationOnce(() => new Promise<ComparisonResult>(resolve => { oldAnswer = resolve; }));
  fireEvent.change(select, { target: { value: "3d" } });
  await waitFor(() => expect(api.requestJson).toHaveBeenCalledTimes(2));
  api.requestJson.mockResolvedValueOnce(result("Nyeste resultat"));
  select.focus();
  fireEvent.change(select, { target: { value: "7d" } });
  await screen.findByText("Nyeste resultat");
  await act(async () => { oldAnswer(result("Forældet resultat")); });
  expect(screen.queryByText("Forældet resultat")).toBeNull();
  expect(screen.getByLabelText("Sammenlign periode")).toBe(select);
  expect(document.activeElement).toBe(select);
});
