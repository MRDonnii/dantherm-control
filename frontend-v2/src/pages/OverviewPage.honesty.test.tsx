// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OverviewPage } from "./OverviewPage";

const api = vi.hoisted(() => ({ requestJson: vi.fn(), postJson: vi.fn() }));
vi.mock("../lib/api", () => ({ requestJson: api.requestJson, postJson: api.postJson }));

class ResizeObserverStub { observe() {} disconnect() {} }
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

afterEach(() => { cleanup(); vi.useRealTimers(); api.requestJson.mockReset(); api.postJson.mockReset(); });

const serve = (unit: object, controller: () => Promise<object> | object) => api.requestJson.mockImplementation(async (endpoint: string) => {
  if (endpoint === "/state.json") return unit;
  if (endpoint === "/api/controller/state?compact=1") return controller();
  if (endpoint === "/api/auth/status") return { csrf: "test" };
  throw new Error(endpoint);
});

describe("Overview tells the truth", () => {
  it("names no room when the fan sits on the normal step, and says the air is good", async () => {
    serve({ available: true, bus_traffic: true, bus_last_frame_age: 1 }, () => ({
      rs485_healthy: true, effective_level: 4, effective_normal_level: 4, effective_source: "ha_smart", effective_reason: "CO2 Soveværelse 729 (auto)",
      co2_setpoint: 800, smart_max_co2: 729, smart_max_co2_room: "Soveværelse",
    }));
    render(<OverviewPage/>);
    expect((await screen.findAllByText(/luften er god i alle rum \(højest CO₂ 729 ppm i Soveværelse, grænsen er 800\)/)).length).toBeGreaterThan(0);
  });

  it("never shows the T2 correction as a temperature", async () => {
    serve({ available: true, bus_traffic: true, bus_last_frame_age: 1, outdoor_temp: 12, extract_temp: 23, exhaust_temp: 14 },
      () => ({ rs485_healthy: true, actual_supply_before_heater_temperature: 18.1, corrected_t2_temperature: 18.5, t2_offset_k: 0.4, afterheat_lift: 0.6, supply_recovery_percent: 81 }));
    render(<OverviewPage/>);
    expect(await screen.findByText(/Føleren før varmefladen er rettet 0,4 grader op/)).toBeTruthy();
    expect(screen.queryByText(/T2 0,4/)).toBeNull();
  });

  it("says the numbers are old and lights no step when the bus is lost", async () => {
    serve({ available: false, bus_traffic: false, bus_last_frame_age: 300, fan_supply_rpm: 1200 }, () => ({ rs485_healthy: false }));
    render(<OverviewPage/>);
    expect(await screen.findByText(/Tallene fra anlægget er gamle/)).toBeTruthy();
    expect(screen.getByText("Ingen kontakt til anlægget")).toBeTruthy();
    expect(document.querySelectorAll(".pro-levels button.active").length).toBe(0);
  });

  it("keeps a pressed step when a poll that started earlier answers late", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    let late: (value: object) => void = () => {};
    let held = false;
    const state = { rs485_healthy: true, mode: "manual", effective_level: 4, manual_level: 4 };
    serve({ available: true, bus_traffic: true, bus_last_frame_age: 1 }, () => held ? new Promise(resolve => { late = resolve; }) : state);
    api.postJson.mockResolvedValue({ ...state, effective_level: 5, manual_level: 5 });
    render(<OverviewPage/>);
    const five = await screen.findByRole("button", { name: "5" });
    await waitFor(() => expect(screen.getByRole("button", { name: "4" }).className).toContain("active"));
    held = true;
    await act(async () => { vi.advanceTimersByTime(2000); });
    fireEvent.click(five);
    await waitFor(() => expect(five.className).toContain("active"));
    await act(async () => { late({ ...state }); });
    expect(screen.getByRole("button", { name: "5" }).className).toContain("active");
  });
});
