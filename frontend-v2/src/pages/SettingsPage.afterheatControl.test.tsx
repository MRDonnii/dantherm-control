// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AfterheatControlCard, CONTROLLER_KEYS, SettingsPage } from "./SettingsPage";

const api = vi.hoisted(() => ({ requestJson: vi.fn(), postJson: vi.fn() }));
vi.mock("../lib/api", () => ({ requestJson: api.requestJson, postJson: api.postJson }));

afterEach(() => { cleanup(); api.requestJson.mockReset(); api.postJson.mockReset(); window.location.hash = ""; });

describe("afterheat control setting", () => {
  it("is saved with the controller", () => {
    expect(CONTROLLER_KEYS).toContain("afterheat_control_mode");
  });

  it("switches between Pi and HAC1 through the form setter", () => {
    const set = vi.fn();
    const controller = { afterheat_control_mode: "native", afterheat_control_active: "native", afterheat_control_reason: "HAC1 styrer selv (valgt)" };
    render(<AfterheatControlCard lang="da" form={controller} controller={controller} set={set}/>);
    const native = screen.getByRole("radio", { name: "HAC1 styrer selv" });
    expect(native.getAttribute("aria-checked")).toBe("true");
    fireEvent.click(screen.getByRole("radio", { name: "Pi-styring" }));
    expect(set).toHaveBeenCalledWith("afterheat_control_mode", "pi");
    expect(screen.getByText(/flytter højst ét trin i minuttet/)).toBeTruthy();
    expect(screen.getByText("HAC1 styrer selv (valgt)")).toBeTruthy();
    expect(screen.queryByLabelText(/Forstærkning/)).toBeNull();
  });

  it("shows the tuning only with Pi control on firmware that reports it", () => {
    const controller = { afterheat_control_mode: "pi", afterheat_pi_gain: 1.5, afterheat_pi_integral_minutes: 8 };
    const set = vi.fn();
    render(<AfterheatControlCard lang="da" form={controller} controller={controller} set={set}/>);
    fireEvent.change(screen.getByLabelText(/Integraltid/), { target: { value: "12" } });
    expect(set).toHaveBeenCalledWith("afterheat_pi_integral_minutes", 12);
    expect((screen.getByLabelText(/Forstærkning/) as HTMLInputElement).value).toBe("1.5");
    expect(screen.queryByLabelText(/Komfortbånd/)).toBeNull();  // older firmware without the setting
  });

  it("shows the comfort band and the minimum time per level when the Pi reports them", () => {
    const controller = { afterheat_control_mode: "pi", afterheat_pi_gain: 1, afterheat_pi_integral_minutes: 2, afterheat_pi_band: 0.5, afterheat_pi_min_dwell_ticks: 3 };
    const set = vi.fn();
    render(<AfterheatControlCard lang="da" form={controller} controller={controller} set={set}/>);
    const band = screen.getByLabelText(/Komfortbånd/) as HTMLInputElement;
    expect(band.value).toBe("0.5");
    expect(band.min).toBe("0.1");
    expect(band.max).toBe("1.5");
    fireEvent.change(band, { target: { value: "0.8" } });
    expect(set).toHaveBeenCalledWith("afterheat_pi_band", 0.8);
    const dwell = screen.getByLabelText(/Mindste tid pr. trin/) as HTMLInputElement;
    expect(dwell.value).toBe("3");
    expect(dwell.max).toBe("10");
    fireEvent.change(dwell, { target: { value: "5" } });
    expect(set).toHaveBeenCalledWith("afterheat_pi_min_dwell_ticks", 5);
    // Whole ticks only: a fraction is rounded, an empty field is not sent as 0.
    set.mockClear();
    fireEvent.change(dwell, { target: { value: "2.5" } });
    expect(set).toHaveBeenLastCalledWith("afterheat_pi_min_dwell_ticks", 3);
    set.mockClear();
    fireEvent.change(dwell, { target: { value: "" } });
    expect(set).not.toHaveBeenCalled();
    expect(screen.getByText(/skiftes der mellem de to nabotrin/)).toBeTruthy();
    expect(screen.getByText(/lader Pi'en trinnet være i fred/)).toBeTruthy();
  });

  it("only appears on firmware with the setting and saves the choice", async () => {
    window.location.hash = "#/settings?section=afterheat";
    api.requestJson.mockImplementation(async (endpoint: string) => endpoint === "/api/auth/status" ? { csrf: "x" } : { afterheat_setpoint: 20 });
    render(<SettingsPage/>);
    await screen.findByText("Mål fra rummene");
    expect(screen.queryByRole("radiogroup", { name: "Regulering af eftervarmen" })).toBeNull();
    cleanup();
    api.requestJson.mockImplementation(async (endpoint: string) => endpoint === "/api/auth/status" ? { csrf: "x" } : { afterheat_setpoint: 20, afterheat_control_mode: "native" });
    api.postJson.mockImplementation(async (_endpoint: string, patch: Record<string, unknown>) => ({ afterheat_setpoint: 20, ...patch }));
    render(<SettingsPage/>);
    fireEvent.click(await screen.findByRole("radio", { name: "Pi-styring" }));
    fireEvent.click(screen.getByRole("button", { name: /Gem controller/ }));
    await waitFor(() => expect(api.postJson).toHaveBeenCalled());
    const patch = api.postJson.mock.calls[0][1] as Record<string, unknown>;
    expect(patch.afterheat_control_mode).toBe("pi");
    expect("afterheat_pi_gain" in patch).toBe(false);
    expect("afterheat_pi_band" in patch).toBe(false);
  });
});
