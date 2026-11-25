// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { HistoryPage } from "./HistoryPage";

const api = vi.hoisted(() => ({ requestJson: vi.fn() }));
vi.mock("../lib/api", () => ({ requestJson: api.requestJson, postJson: vi.fn() }));
vi.mock("../components/AlarmHistory", () => ({ AlarmHistory: () => null }));

class ResizeObserverStub { observe() {} disconnect() {} }
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

afterEach(() => { cleanup(); api.requestJson.mockReset(); });

const now = Math.floor(Date.now() / 1000);
const samples = (outdoor: number) => [{ ts: now - 120, outdoor_temp: outdoor }, { ts: now - 60, outdoor_temp: outdoor }, { ts: now, outdoor_temp: outdoor }];

describe("History page", () => {
  it("ignores a slow answer for an older period after the period was switched", async () => {
    let slow: (value: object) => void = () => {};
    api.requestJson.mockImplementation((endpoint: string) => {
      if (endpoint === "/history.json?range=24h") return new Promise(resolve => { slow = resolve; });
      if (endpoint === "/history.json?range=7d") return Promise.resolve({ range: "7d", samples: samples(7) });
      return Promise.resolve({
        decision_log: [
          { timestamp: now - 7200, master: "pi", source: "ha_smart", level: 4, reason: "CO2 Soveværelse 700 (auto)", controlling_room: "Soveværelse" },
          { timestamp: now - 3600, master: "pi", source: "ha_smart", level: 5, reason: "Badeværelse RH Badeværelse 71.5% / 65% (high)", controlling_room: "Badeværelse" },
        ],
      });
    });
    render(<HistoryPage/>);
    fireEvent.click(screen.getByRole("button", { name: "7 dage" }));
    await waitFor(() => expect(screen.getAllByText("7,0°C").length).toBeGreaterThan(0));
    await act(async () => { slow({ range: "24h", samples: samples(24) }); });
    expect(screen.queryByText("24,0°C")).toBeNull();
    expect(screen.getAllByText("7,0°C").length).toBeGreaterThan(0);
    // The decision log is shown as a plain "why did the fan change" list.
    expect(await screen.findByText("Fugt 71,5 % i Badeværelse (grænse 65 %)")).toBeTruthy();
    expect(screen.getByText("Trin 4 → 5")).toBeTruthy();
  });
});
