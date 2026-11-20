// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OverviewPage } from "./OverviewPage";

const api = vi.hoisted(() => ({ requestJson: vi.fn(), postJson: vi.fn() }));
vi.mock("../lib/api", () => ({ requestJson: api.requestJson, postJson: api.postJson }));

class ResizeObserverStub { observe() {} disconnect() {} }
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

afterEach(() => { cleanup(); api.requestJson.mockReset(); api.postJson.mockReset(); });

function serve(controller: Record<string, unknown>) {
  api.requestJson.mockImplementation(async (endpoint: string) => {
    if (endpoint === "/state.json") return { outdoor_temp: 4.2, extract_temp: 21.5 };
    if (endpoint === "/api/controller/state?compact=1") return { afterheat_coil: "water", afterheat_setpoint: 21, ...controller };
    if (endpoint === "/api/auth/status") return { csrf: "test" };
    throw new Error(endpoint);
  });
}

describe("afterheat level on the overview", () => {
  it("shows HAC1's level, who controls and the reason under the thermostat", async () => {
    serve({
      afterheat_control_mode: "pi", afterheat_control_active: "pi", afterheat_level: 3, afterheat_level_wanted: 3.2, afterheat_level_target: 3,
      afterheat_level_max: 10, afterheat_next_action: "hold", afterheat_next_tick_seconds: 30, afterheat_target: 21,
      afterheat_hac1_setpoint: 21, afterheat_control_error: 0.2, afterheat_control_reason: "Holder trin 3 · luft 21,2 °C mod mål 21,0 °C",
    });
    render(<OverviewPage/>);
    expect(await screen.findByText("Holder trin 3 · luft 21,2 °C mod mål 21,0 °C")).toBeTruthy();
    expect(screen.getByText("Trin 3/10")).toBeTruthy();
    expect(screen.getByText("Pi-styring")).toBeTruthy();
    expect(screen.getByRole("meter", { name: "HAC1-trin" }).getAttribute("aria-valuenow")).toBe("3");
    expect(screen.getByText(/Holder trinnet om \d+ s/)).toBeTruthy();
  });

  it("keeps the old card on firmware without the level control", async () => {
    serve({});
    render(<OverviewPage/>);
    await screen.findAllByRole("slider");
    expect(screen.queryByRole("meter", { name: "HAC1-trin" })).toBeNull();
    expect(screen.queryByText(/Trin \d+\/10/)).toBeNull();
  });
});
