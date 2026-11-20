// @vitest-environment jsdom
import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AfterheatLevelPanel } from "./AfterheatLevel";
import { readAfterheatControl } from "../lib/afterheatControl";

afterEach(() => { cleanup(); vi.useRealTimers(); });

const PI = {
  afterheat_control_mode: "pi", afterheat_control_active: "pi", afterheat_level: 3, afterheat_level_wanted: 3.4, afterheat_level_target: 4,
  afterheat_level_max: 10, afterheat_next_action: "up", afterheat_next_tick_seconds: 12, afterheat_target: 21.5, afterheat_hac1_setpoint: 22,
  afterheat_control_error: -0.4, afterheat_control_reason: "Går et trin op: 21,1 °C under mål",
};

describe("AfterheatLevelPanel", () => {
  it("shows the level, the target marker, who controls, the target and the reason", () => {
    const { container } = render(<AfterheatLevelPanel control={readAfterheatControl(PI)}/>);
    expect(screen.getByText("Trin 3/10")).toBeTruthy();
    expect(screen.getByText("Pi-styring")).toBeTruthy();
    expect(screen.getByText("21,5 °C")).toBeTruthy();
    expect(screen.getByText("3,4")).toBeTruthy();
    expect(screen.getByText("Går et trin op: 21,1 °C under mål")).toBeTruthy();
    const meter = screen.getByRole("meter", { name: "HAC1-trin" });
    expect(meter.getAttribute("aria-valuenow")).toBe("3");
    expect(container.querySelectorAll(".afterheat-level-bar i")).toHaveLength(10);
    expect(container.querySelectorAll(".afterheat-level-bar i.is-lit")).toHaveLength(3);
    const target = container.querySelectorAll(".afterheat-level-bar i")[3];
    expect(target.className).toContain("is-target");
    const marker = container.querySelector(".afterheat-level-marker") as HTMLElement;
    expect(marker.style.getPropertyValue("--at")).toBe("34%");
  });

  it("counts down to the next tick between polls", () => {
    vi.useFakeTimers();
    render(<AfterheatLevelPanel control={readAfterheatControl(PI)}/>);
    expect(screen.getByText("Et trin op til 4 om 12 s")).toBeTruthy();
    act(() => { vi.advanceTimersByTime(5000); });
    expect(screen.getByText("Et trin op til 4 om 7 s")).toBeTruthy();
    act(() => { vi.advanceTimersByTime(9000); });
    expect(screen.getByText("Et trin op til 4 nu")).toBeTruthy();
  });

  it("shows only the level and the reason while HAC1 controls", () => {
    const control = readAfterheatControl({ ...PI, afterheat_control_active: "fallback", afterheat_control_reason: "HAC1 styrer selv (forældet måling)" });
    const { container } = render(<AfterheatLevelPanel control={control}/>);
    expect(screen.getByText("HAC1 midlertidigt")).toBeTruthy();
    expect(screen.getByText("HAC1 styrer selv (forældet måling)")).toBeTruthy();
    expect(screen.queryByText(/Et trin op/)).toBeNull();
    expect(container.querySelector(".afterheat-level-marker")).toBeNull();
    expect(container.querySelector(".is-target")).toBeNull();
  });

  it("copes with an unknown level", () => {
    render(<AfterheatLevelPanel control={readAfterheatControl({ afterheat_control_mode: "native", afterheat_control_active: "native", afterheat_level: null })}/>);
    expect(screen.getByText("Trin —/10")).toBeTruthy();
    expect(screen.getByRole("meter").getAttribute("aria-valuetext")).toBe("Ukendt");
  });
});
