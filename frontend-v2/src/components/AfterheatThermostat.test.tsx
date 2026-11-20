// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AfterheatThermostat, FROST_POWER_NOTE, type AfterheatThermostatProps } from "./AfterheatThermostat";

afterEach(cleanup);

function setup(props: Partial<AfterheatThermostatProps> = {}) {
  const onChange = vi.fn();
  const onConfirm = vi.fn();
  const base: AfterheatThermostatProps = {
    value: 20, current: 20, onChange, onConfirm, onCancel: () => {}, heating: true, lockout: false, cutoff: 15,
    outdoor: -2, airBefore: 14, airAfter: 20, registered: "20 °C", ...props,
  };
  render(<AfterheatThermostat {...base}/>);
  return { onChange, onConfirm };
}
const power = () => screen.getByRole("button", { name: /Sluk|Tænd/ });
const minus = () => screen.getAllByRole("button", { name: "Sænk eftervarme" })[0];

describe("AfterheatThermostat", () => {
  it("can be switched off without the frost lock", () => {
    const { onChange } = setup({ value: 10, current: 10 });
    expect((power() as HTMLButtonElement).disabled).toBe(false);
    fireEvent.click(power());
    expect(onChange).toHaveBeenLastCalledWith("off");
    fireEvent.click(minus());
    expect(onChange).toHaveBeenLastCalledWith("off");
    expect(screen.queryByText(new RegExp(FROST_POWER_NOTE))).toBeNull();
  });

  it("disables the power button and explains why while it freezes", () => {
    const { onChange } = setup({ frostLock: true, floor: 18 });
    const button = power() as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.title).toBe(FROST_POWER_NOTE);
    fireEvent.click(button);
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByText(/Frostsikring: eftervarmen kan ikke slukkes i frostvejr/)).toBeTruthy();
  });

  it("stops the minus button and the keyboard at the floor", () => {
    const { onChange } = setup({ value: 19, current: 19, frostLock: true, floor: 18 });
    fireEvent.click(minus());
    expect(onChange).toHaveBeenLastCalledWith(18);
    cleanup();
    const atFloor = setup({ value: 18, current: 18, frostLock: true, floor: 18 });
    expect((minus() as HTMLButtonElement).disabled).toBe(true);
    const slider = screen.getByRole("slider");
    expect(slider.getAttribute("aria-valuemin")).toBe("18");
    fireEvent.keyDown(slider, { key: "ArrowDown" });
    fireEvent.keyDown(slider, { key: "ArrowLeft" });
    expect(atFloor.onChange).not.toHaveBeenCalled();
  });

  it("turns an OFF value back on at the floor or above, never lower", () => {
    const { onChange } = setup({ value: "off", current: "off", frostLock: true, floor: 18, lastOn: 12 });
    fireEvent.click(power());
    expect(onChange).toHaveBeenLastCalledWith(18);
    fireEvent.click(screen.getAllByRole("button", { name: "Hæv eftervarme" })[0]);
    expect(onChange).toHaveBeenLastCalledWith(18);
  });

  it("clamps the dial at the floor", () => {
    const { onChange } = setup({ value: 22, current: 22, frostLock: true, floor: 18 });
    const slider = screen.getByRole("slider");
    slider.getBoundingClientRect = () => ({ left: 0, top: 0, width: 200, height: 200, right: 200, bottom: 200, x: 0, y: 0, toJSON: () => ({}) });
    (slider as unknown as { setPointerCapture: () => void }).setPointerCapture = () => {};
    // Lower-left end of the scale is 10 °C.
    fireEvent.pointerDown(slider, { clientX: 45, clientY: 155, pointerId: 1 });
    expect(onChange).toHaveBeenLastCalledWith(18);
  });

  it("refuses to confirm OFF or a value under the floor", () => {
    const { onConfirm } = setup({ value: "off", current: 20, frostLock: true, floor: 18 });
    const ok = screen.getByRole("button", { name: "Bekræft" }) as HTMLButtonElement;
    expect(ok.disabled).toBe(true);
    fireEvent.click(ok);
    expect(onConfirm).not.toHaveBeenCalled();
    cleanup();
    const allowed = setup({ value: 21, current: 20, frostLock: true, floor: 18 });
    fireEvent.click(screen.getByRole("button", { name: "Bekræft" }));
    expect(allowed.onConfirm).toHaveBeenCalledTimes(1);
  });

  it("explains a draft under the floor with the floor text", () => {
    setup({ value: 16, current: 20, frostLock: true, floor: 18 });
    expect(screen.getByText("Frostsikring: eftervarmen kan ikke sættes under 18 °C, mens det fryser.")).toBeTruthy();
  });

  it("steps up to whole degrees from a fractional floor", () => {
    const { onChange } = setup({ value: 18.5, current: 18.5, frostLock: true, floor: 18.5 });
    fireEvent.click(screen.getAllByRole("button", { name: "Hæv eftervarme" })[0]);
    expect(onChange).toHaveBeenLastCalledWith(19);
    expect((minus() as HTMLButtonElement).disabled).toBe(true);
  });
});
