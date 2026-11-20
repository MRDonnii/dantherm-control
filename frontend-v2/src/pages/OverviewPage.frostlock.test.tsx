// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { OverviewPage } from "./OverviewPage";

const api = vi.hoisted(() => ({ requestJson: vi.fn(), postJson: vi.fn() }));
vi.mock("../lib/api", () => ({ requestJson: api.requestJson, postJson: api.postJson }));

class ResizeObserverStub { observe() {} disconnect() {} }
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

afterEach(() => { cleanup(); api.requestJson.mockReset(); api.postJson.mockReset(); });

function serve(controller: Record<string, unknown>) {
  api.requestJson.mockImplementation(async (endpoint: string) => {
    if (endpoint === "/state.json") return { outdoor_temp: -1.6, extract_temp: 21.5, exhaust_temp: 3.1 };
    if (endpoint === "/api/controller/state?compact=1") return { afterheat_coil: "water", ...controller };
    if (endpoint === "/api/auth/status") return { csrf: "test" };
    throw new Error(endpoint);
  });
}
const LOCKED = {
  afterheat_frost_lock: true, afterheat_frost_lock_reason: "Frost: ude -1,6 °C (grænse 3 °C)", afterheat_frost_lock_floor: 18,
  afterheat_frost_lock_enforced: true, frost_lock_enabled: true, frost_lock_outdoor_c: 3, frost_lock_coil_c: 8, frost_lock_min_setpoint_c: 18,
};

describe("afterheat frost lock on the overview", () => {
  it("shows the afterheat as on at the floor even when the stored choice is OFF", async () => {
    serve({ ...LOCKED, afterheat_enabled: false, afterheat_setpoint: 15 });
    render(<OverviewPage/>);
    await waitFor(() => expect(screen.getAllByRole("slider")[0].getAttribute("aria-valuenow")).toBe("18"));
    const power = screen.getAllByRole("button", { name: /Sluk/ })[0] as HTMLButtonElement;
    expect(power.disabled).toBe(true);
    fireEvent.click(power);
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(api.postJson).not.toHaveBeenCalled();
    expect(screen.getAllByText(/Frostsikring: eftervarmen kan ikke slukkes i frostvejr/).length).toBeGreaterThan(0);
  });

  it("keeps today's OFF behaviour on firmware without the frost lock", async () => {
    serve({ afterheat_enabled: false, afterheat_setpoint: 15 });
    render(<OverviewPage/>);
    await waitFor(() => expect(screen.getAllByRole("slider")[0].getAttribute("aria-valuetext")).toBe("Slukket"));
    expect((screen.getAllByRole("button", { name: /Tænd/ })[0] as HTMLButtonElement).disabled).toBe(false);
  });

  it("blocks Køl while it freezes but keeps Stop available", async () => {
    serve({ ...LOCKED, afterheat_setpoint: 20, cool_boost_remaining_seconds: 600, cool_boost_minutes: 30 });
    render(<OverviewPage/>);
    const kol = await screen.findByRole("button", { name: "Køl" }) as HTMLButtonElement;
    await waitFor(() => expect(kol.disabled).toBe(true));
    expect(kol.title).toBe("Spærret: frostsikring");
    const stop = screen.getByRole("button", { name: "Stop" }) as HTMLButtonElement;
    expect(stop.disabled).toBe(false);
  });

  it("shows the blocked hint on the Køl tile", async () => {
    serve({ ...LOCKED, afterheat_setpoint: 20 });
    render(<OverviewPage/>);
    expect(await screen.findByText("Spærret: frostsikring")).toBeTruthy();
  });

  it("lets Køl open normally without the lock", async () => {
    serve({ afterheat_setpoint: 20, afterheat_frost_lock: false });
    render(<OverviewPage/>);
    const kol = await screen.findByRole("button", { name: "Køl" }) as HTMLButtonElement;
    await waitFor(() => expect(kol.disabled).toBe(false));
    fireEvent.click(kol);
    const dialog = await screen.findByRole("dialog", { name: "Køl huset" });
    const thirty = within(dialog).getByRole("button", { name: /30 min/ });
    expect((thirty as HTMLButtonElement).disabled).toBe(false);
  });
});
