// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TechniquePage, afterheatControlRows } from "./TechniquePage";
import { readAfterheatControl } from "../lib/afterheatControl";

const api = vi.hoisted(() => ({ requestJson: vi.fn() }));
vi.mock("../lib/api", () => ({ requestJson: api.requestJson, postJson: vi.fn() }));

afterEach(() => { cleanup(); api.requestJson.mockReset(); });

const PI = {
  afterheat_control_mode: "pi", afterheat_control_active: "pi", afterheat_level: 3, afterheat_level_wanted: 3.6, afterheat_level_target: 4,
  afterheat_next_action: "up", afterheat_next_tick_seconds: 14, afterheat_target: 21.5, afterheat_hac1_setpoint: 22,
  afterheat_control_error: -0.42, afterheat_control_reason: "Går et trin op: 21,1 °C under mål",
};

describe("TechniquePage afterheat level rows", () => {
  it("shows the level, the control and the next action", async () => {
    api.requestJson.mockImplementation(async () => PI);
    render(<TechniquePage/>);
    expect(await screen.findByText("3/10 · mål 4 (3,6)")).toBeTruthy();
    // Register 209 is only on/off; the step is HAC1's level, not an opening.
    expect(screen.getByText("Eftervarmens trin").nextElementSibling?.textContent).toBe("Trin 3 af 10");
    expect(screen.getByText("Pi-styring · HAC1-setpunkt 22 °C")).toBeTruthy();
    expect(screen.getByText("Et trin op til 4 om 14 s")).toBeTruthy();
    expect(screen.getByText("21,5 °C · −0,42 K")).toBeTruthy();
  });

  it("says when the Pi has handed control back to HAC1", () => {
    const rows = afterheatControlRows(readAfterheatControl({ ...PI, afterheat_control_active: "fallback", afterheat_control_reason: "HAC1 styrer selv (forældet måling)" }));
    const byLabel = Object.fromEntries(rows.map(row => [row.label, row]));
    expect(byLabel["HAC1 trin"].value).toBe("3/10");
    expect(byLabel.Styring.value).toBe("HAC1 midlertidigt · valgt: Pi-styring · HAC1-setpunkt 22 °C");
    expect(byLabel.Styring.tone).toBe("warn");
    expect(byLabel["Næste handling"].value).toBe("—");
    expect(byLabel["Styringens begrundelse"].value).toBe("HAC1 styrer selv (forældet måling)");
  });

  it("prefers the gateway's valve percent and shows no opening without a level", () => {
    const reported = afterheatControlRows(readAfterheatControl({ ...PI, actual_afterheat_valve_percent: 70, afterheat_level: 7 }));
    expect(reported[0].value).toBe("7/10 · mål 4 (3,6)");
    const unknown = afterheatControlRows(readAfterheatControl({ ...PI, afterheat_level: null }));
    expect(unknown[0].value).toBe("— · mål 4 (3,6)");
  });

  it("adds no rows on older firmware", () => {
    expect(afterheatControlRows(readAfterheatControl({ afterheat_setpoint: 20 }))).toEqual([]);
  });
});
