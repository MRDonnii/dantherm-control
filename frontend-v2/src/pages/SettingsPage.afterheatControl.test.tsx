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
  });

  it("only appears on firmware with the setting and saves the choice", async () => {
    window.location.hash = "#/settings?section=afterheat";
    api.requestJson.mockImplementation(async (endpoint: string) => endpoint === "/api/auth/status" ? { csrf: "x" } : { afterheat_setpoint: 20 });
    render(<SettingsPage/>);
    await screen.findByText("Styr efter rumtemperatur");
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
  });
});
