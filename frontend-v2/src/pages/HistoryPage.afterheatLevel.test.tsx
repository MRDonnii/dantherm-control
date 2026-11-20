// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
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

describe("HAC1 level chart in the history", () => {
  it("draws the level and target level on a fixed 0..10 axis under the afterheat chart", async () => {
    serve(index => ({ afterheat_level: index < 3 ? 2 : 3, afterheat_level_target: 3, afterheat_target: 21.5 }));
    render(<HistoryPage/>);
    expect(await screen.findByText("HAC1-trin")).toBeTruthy();
    const chart = screen.getByRole("img", { name: /^HAC1-trin: Trin, Mål-trin/ });
    const ticks = [...chart.querySelectorAll("text.hx-tick")].map(node => node.textContent).filter(text => /^\d+$/.test(text ?? ""));
    expect(ticks.slice(0, 6)).toEqual(["0", "2", "4", "6", "8", "10"]);
    // Steps, not slopes.
    expect(chart.querySelector("path.hx-line")?.getAttribute("d")).toMatch(/H[\d.]+V/);
    // The afterheat chart's reference follows the Pi's target with decimals.
    expect(screen.getByText("Mål").nextElementSibling?.textContent).toBe("21,5°C");
  });

  it("keeps the target line over a period that starts before the update", async () => {
    serve((index): Record<string, number | null> => index < 3 ? {} : { afterheat_level: 2, afterheat_target: 21.5 });
    render(<HistoryPage/>);
    expect(await screen.findByText("HAC1-trin")).toBeTruthy();
    const chart = screen.getByRole("img", { name: /^Indblæsning og eftervarme/ });
    // One stepped line through both the old whole setpoint and the new target.
    const path = chart.querySelector("path.hx-ref")?.getAttribute("d") ?? "";
    expect(path.match(/M/g)?.length).toBe(1);
    expect(path.match(/H/g)?.length).toBe(5);
  });

  it("leaves the chart out for history without levels", async () => {
    serve(() => ({}));
    render(<HistoryPage/>);
    expect(await screen.findByText("Setpunkt")).toBeTruthy();
    expect(screen.queryByText("HAC1-trin")).toBeNull();
  });
});
