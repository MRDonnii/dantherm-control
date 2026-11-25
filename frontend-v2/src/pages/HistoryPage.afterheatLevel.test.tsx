// @vitest-environment jsdom
import { cleanup, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HistoryPage } from "./HistoryPage";

const api = vi.hoisted(() => ({ requestJson: vi.fn() }));
vi.mock("../lib/api", () => ({ requestJson: api.requestJson, postJson: vi.fn() }));
vi.mock("../components/AlarmHistory", () => ({ AlarmHistory: () => null }));

class ResizeObserverStub { observe() {} disconnect() {} }
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

afterEach(() => { cleanup(); api.requestJson.mockReset(); });

const now = Math.floor(Date.now() / 1000);
function serve(extra: (index: number) => Record<string, number | null>) {
  const samples = Array.from({ length: 6 }, (_, index) => ({ ts: now - 3000 + index * 600, heating_coil_after_temperature: 21 + index * 0.1, afterheat_setpoint: 21, ...extra(index) }));
  api.requestJson.mockImplementation(async (endpoint: string) => {
    if (endpoint.startsWith("/history.json")) return { range: "24h", samples };
    return {};
  });
}

describe("Afterheat chart and valve opening in the history", () => {
  const afterheatChart = () => screen.findByRole("img", { name: /^Indblæsning og eftervarme/ });
  const legendValue = (chart: Element, label: string) => within(chart.closest(".hx-chart") as HTMLElement).getByText(label).nextElementSibling?.textContent;

  it("draws the step and the decision as steps inside the afterheat plot", async () => {
    serve(index => ({ afterheat_level: index < 3 ? 2 : 3, afterheat_level_target: 3, afterheat_target: 21.5 }));
    render(<HistoryPage/>);
    const chart = await afterheatChart();
    expect(chart.getAttribute("aria-label")).toMatch(/Trin \(0-10\), Styringens beslutning/);
    expect([...chart.querySelectorAll("text.hx-tick-valve")].map(node => node.textContent)).toEqual(["0", "5", "10"]);
    // Steps, not slopes.
    expect(chart.querySelector("path.hx-valve-edge")?.getAttribute("d")).toMatch(/H[\d.]+V/);
    expect(chart.querySelector("path.hx-decision")?.getAttribute("d")).toMatch(/^M[\d.]+ [\d.]+H/);
    // Level 3 is 30 %; the old on/off strip and the separate level chart are gone.
    expect(legendValue(chart, "Trin (0-10)")).toBe("3");
    expect(legendValue(chart, "Styringens beslutning")).toBe("3");
    expect(screen.queryByText("Ventil åben")).toBeNull();
    expect(screen.queryByRole("img", { name: /^Trin \(0-10\)/ })).toBeNull();
    // The reference is the user's target with decimals.
    expect(legendValue(chart, "Mål")).toBe("21,5°C");
  });

  it("keeps the target line over a period that starts before the update", async () => {
    serve((index): Record<string, number | null> => index < 3 ? {} : { afterheat_level: 2, afterheat_target: 21.5 });
    render(<HistoryPage/>);
    const chart = await afterheatChart();
    // One stepped line through both the old whole setpoint and the new target.
    const path = chart.querySelector("path.hx-ref")?.getAttribute("d") ?? "";
    expect(path.match(/M/g)?.length).toBe(1);
    expect(path.match(/H/g)?.length).toBe(5);
  });

  it("keeps the target flat while the setpoint sent to HAC1 moves", async () => {
    serve(index => ({ afterheat_level: 3, afterheat_target: 21, afterheat_setpoint: index < 3 ? 25 : 27, afterheat_level_target: 4 }));
    render(<HistoryPage/>);
    const chart = await afterheatChart();
    expect(chart.querySelector("path.hx-cmd")?.getAttribute("d")).toMatch(/H[\d.]+V/);
    expect(legendValue(chart, "Sendt til HAC1")).toBe("27,0°C");
    // The reference stays at 21: one height all the way.
    expect(legendValue(chart, "Mål")).toBe("21,0°C");
    const ys = new Set((chart.querySelector("path.hx-ref")?.getAttribute("d") ?? "").match(/V[\d.]+/g) ?? []);
    expect(ys.size).toBe(1);
  });

  it("leaves the sent line out while HAC1 is given the target itself", async () => {
    serve(() => ({ afterheat_level: 3, afterheat_target: 21, afterheat_setpoint: 21 }));
    render(<HistoryPage/>);
    const chart = await afterheatChart();
    expect(screen.queryByText("Sendt til HAC1")).toBeNull();
    expect(legendValue(chart, "Styringens beslutning")).toBe("HAC1 styrer selv");
  });

  it("falls back to the setpoint as target and the open-valve strip for history without levels", async () => {
    serve(() => ({ afterheat_active: 1 }));
    render(<HistoryPage/>);
    const chart = await afterheatChart();
    expect(legendValue(chart, "Mål")).toBe("21,0°C");
    expect(screen.getByText("Ventil åben")).toBeTruthy();
    expect(chart.querySelector("text.hx-tick-valve")).toBeNull();
  });
});
