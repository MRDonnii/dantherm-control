// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SENSOR_HISTORY, SensorHistoryDialog } from "./SensorHistoryDialog";

const api = vi.hoisted(() => ({ requestJson: vi.fn() }));
vi.mock("../lib/api", () => ({ requestJson: api.requestJson, postJson: vi.fn() }));

class ResizeObserverStub { observe() {} disconnect() {} }
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

afterEach(() => { cleanup(); api.requestJson.mockReset(); });

const now = Math.floor(Date.now() / 1000);
const times = Array.from({ length: 6 }, (_, index) => now - 3000 + index * 600);
function serve(extra: (index: number) => Record<string, number | null>) {
  const samples = times.map((ts, index) => ({
    ts, heating_coil_after_temperature: 22 + index * 0.5, afterheat_setpoint: 21, ...extra(index),
  }));
  api.requestJson.mockResolvedValue({ range: "24h", samples });
}
const legendValue = (label: string) =>
  [...document.querySelectorAll(".hx-label")].find(node => node.textContent === label)?.nextElementSibling?.textContent;

describe("T2AH graph (afterheat)", () => {
  it("draws target, sent setpoint, valve opening and decision inside one plot", async () => {
    serve(index => ({ afterheat_target: 21, afterheat_setpoint: 25 + (index > 3 ? 1 : 0), afterheat_level: index < 3 ? 3 : 5, afterheat_level_target: 4 }));
    render(<SensorHistoryDialog sensor={SENSOR_HISTORY.afterHeater} onClose={() => {}}/>);
    const chart = await screen.findByRole("img", { name: /^T2AH efter eftervarme: .*Ventilåbning \(faktisk\), Styringens beslutning/ });
    // One plot: no separate valve chart.
    expect(screen.getAllByRole("img")).toHaveLength(1);
    expect(legendValue("Mål")).toBe("21,0°C");
    expect(legendValue("Sendt til HAC1")).toBe("26,0°C");
    expect(legendValue("Ventilåbning (faktisk)")).toBe("50 %");
    expect(legendValue("Styringens beslutning")).toBe("40 %");
    expect(chart.querySelector("path.hx-ref")).toBeTruthy();
    expect(chart.querySelector("path.hx-cmd")).toBeTruthy();
    expect(chart.querySelector("path.hx-valve-area")?.getAttribute("d")).toMatch(/Z$/);
    expect(chart.querySelector("path.hx-decision")?.getAttribute("d")).toMatch(/^M/);
    // Own 0..100 % scale on the right edge.
    expect([...chart.querySelectorAll("text.hx-tick-valve")].map(node => node.textContent)).toEqual(["0 %", "50 %", "100 %"]);
    expect(screen.queryByText("Ventil åben")).toBeNull();
    expect(screen.getByText(/HAC1's trin × 10 %/)).toBeTruthy();
  });

  it("reads every value at the hovered moment", async () => {
    serve(index => ({ afterheat_target: 21, afterheat_setpoint: 25 + index, afterheat_level: index, afterheat_level_target: index + 1 }));
    render(<SensorHistoryDialog sensor={SENSOR_HISTORY.afterHeater} onClose={() => {}}/>);
    const chart = await screen.findByRole("img", { name: /^T2AH efter eftervarme/ });
    fireEvent.keyDown(chart, { key: "End" });
    fireEvent.keyDown(chart, { key: "ArrowLeft" });
    fireEvent.keyDown(chart, { key: "ArrowLeft" });
    // Sample 3: T2AH 23,5, sent 28, level 3 = 30 %, decision 4 = 40 %.
    expect(legendValue("T2AH efter eftervarme")).toBe("23,5°C");
    expect(legendValue("Mål")).toBe("21,0°C");
    expect(legendValue("Sendt til HAC1")).toBe("28,0°C");
    expect(legendValue("Ventilåbning (faktisk)")).toBe("30 %");
    expect(legendValue("Styringens beslutning")).toBe("40 %");
    expect(chart.querySelector("circle.hx-valve-dot")).toBeTruthy();
  });

  it("says HAC1 steers itself where there is no decision", async () => {
    serve(() => ({ afterheat_target: 21, afterheat_setpoint: 21, afterheat_level: 2, afterheat_level_target: null }));
    render(<SensorHistoryDialog sensor={SENSOR_HISTORY.afterHeater} onClose={() => {}}/>);
    const chart = await screen.findByRole("img", { name: /^T2AH efter eftervarme/ });
    expect(legendValue("Ventilåbning (faktisk)")).toBe("20 %");
    expect(legendValue("Styringens beslutning")).toBe("HAC1 styrer selv");
    expect(chart.querySelector("path.hx-decision")?.getAttribute("d")).toBe("");
    // HAC1 gets the target itself: no extra line on top of it.
    expect(screen.queryByText("Sendt til HAC1")).toBeNull();
  });

  it("falls back to the setpoint and the open-valve strip for older history", async () => {
    serve(() => ({ afterheat_active: 0.5 }));
    render(<SensorHistoryDialog sensor={SENSOR_HISTORY.afterHeater} onClose={() => {}}/>);
    expect(await screen.findByText("Ventil åben")).toBeTruthy();
    expect(legendValue("Mål")).toBe("21,0°C");
    expect(screen.queryByText("Sendt til HAC1")).toBeNull();
    expect(screen.queryByText("Ventilåbning (faktisk)")).toBeNull();
    expect(document.querySelector("text.hx-tick-valve")).toBeNull();
  });
});
