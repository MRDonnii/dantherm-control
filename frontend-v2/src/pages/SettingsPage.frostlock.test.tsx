// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CONTROLLER_KEYS, FrostLockCard, SettingsPage } from "./SettingsPage";

const api = vi.hoisted(() => ({ requestJson: vi.fn(), postJson: vi.fn() }));
vi.mock("../lib/api", () => ({ requestJson: api.requestJson, postJson: api.postJson }));

afterEach(() => { cleanup(); api.requestJson.mockReset(); api.postJson.mockReset(); window.location.hash = ""; });

const SETTINGS = { frost_lock_enabled: true, frost_lock_outdoor_c: 3, frost_lock_coil_c: 8, frost_lock_min_setpoint_c: 18 };

describe("frost lock settings", () => {
  it("saves the four frost lock settings with the controller", () => {
    for (const key of Object.keys(SETTINGS)) expect(CONTROLLER_KEYS).toContain(key);
  });

  it("explains the lock and shows its state", () => {
    const controller = { ...SETTINGS, afterheat_coil: "water", afterheat_frost_lock: true, afterheat_frost_lock_reason: "Frost: ude -1,6 °C (grænse 3 °C)", afterheat_frost_lock_floor: 18, afterheat_frost_lock_enforced: false };
    const markup = renderToStaticMarkup(<FrostLockCard lang="da" form={controller} controller={controller} set={() => {}}/>);
    for (const text of ["Frostlås", "Frostsikring af eftervarmen", "Frost ude eller på loftet", "Frost ved vandfladen", "Mindste setpunkt i frost",
      "Aktiv · min. 18 °C", "Frost: ude -1,6 °C (grænse 3 °C)", "Nej · Pi er ikke busmaster", "HCP4 styrer bussen", "2 °C over grænserne i 30 minutter"]) {
      expect(markup).toContain(text);
    }
    expect(markup).toContain('min="-10" max="10"');
    expect(markup).toContain('min="3" max="15"');
    expect(markup).toContain('min="15" max="25"');
    expect(markup).not.toContain("elvarmeflade, så frostlåsen");
  });

  it("notes that an electric coil does not use the lock", () => {
    const controller = { ...SETTINGS, afterheat_coil: "electric", afterheat_frost_lock: false };
    const markup = renderToStaticMarkup(<FrostLockCard lang="en" form={controller} controller={controller} set={() => {}}/>);
    expect(markup).toContain("Frost lock");
    expect(markup).toContain("Not active");
    expect(markup).toContain("The coil is set to electric");
  });

  it("edits a limit through the form setter", () => {
    const set = vi.fn();
    render(<FrostLockCard lang="da" form={SETTINGS} controller={SETTINGS} set={set}/>);
    fireEvent.change(screen.getByLabelText(/Mindste setpunkt i frost/), { target: { value: "19" } });
    expect(set).toHaveBeenCalledWith("frost_lock_min_setpoint_c", 19);
    fireEvent.click(screen.getByLabelText(/Frostsikring af eftervarmen/));
    expect(set).toHaveBeenCalledWith("frost_lock_enabled", false);
  });

  it("only appears in the afterheat section on firmware that reports the lock", async () => {
    window.location.hash = "#/settings?section=afterheat";
    api.requestJson.mockImplementation(async (endpoint: string) => endpoint === "/api/auth/status" ? { csrf: "x" } : { afterheat_setpoint: 20 });
    render(<SettingsPage/>);
    await waitFor(() => expect(api.requestJson).toHaveBeenCalled());
    await screen.findByText("Styr efter rumtemperatur");
    expect(screen.queryByText("Mindste setpunkt i frost")).toBeNull();
    cleanup();
    api.requestJson.mockImplementation(async (endpoint: string) => endpoint === "/api/auth/status" ? { csrf: "x" } : { afterheat_setpoint: 20, ...SETTINGS, afterheat_frost_lock: false });
    render(<SettingsPage/>);
    expect(await screen.findByText("Mindste setpunkt i frost")).toBeTruthy();
  });
});
