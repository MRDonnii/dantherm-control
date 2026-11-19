// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RestartPanel } from "./RestartPanel";

const api = vi.hoisted(() => ({ calls: [] as { endpoint: string; init?: RequestInit }[], up: true, fail: "" }));
vi.mock("../lib/api", () => ({
  requestJson: async (endpoint: string, init?: RequestInit) => {
    api.calls.push({ endpoint, init });
    if (endpoint === "/api/admin/action") {
      if (api.fail) throw new Error(api.fail);
      return { ok: true };
    }
    if (!api.up) throw new Error("offline");
    return { csrf: "t" };
  },
}));

const posts = () => api.calls.filter(call => call.endpoint === "/api/admin/action");
const reload = vi.fn();

beforeEach(() => {
  api.calls = []; api.up = true; api.fail = ""; reload.mockClear();
  Object.defineProperty(window, "location", { configurable: true, value: { ...window.location, reload } });
});
afterEach(() => { cleanup(); vi.useRealTimers(); });

describe("RestartPanel", () => {
  it("asks first and sends nothing when the dialog is cancelled", () => {
    render(<RestartPanel csrf="csrf-1"/>);
    fireEvent.click(screen.getByRole("button", { name: /Genstart Pi’en/ }));
    expect(screen.getByRole("alertdialog", { name: "Genstart Raspberry Pi’en?" })).toBeTruthy();
    expect(document.activeElement?.textContent).toBe("Annullér");
    fireEvent.click(screen.getByRole("button", { name: "Annullér" }));
    expect(screen.queryByRole("alertdialog")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Genstart Dantherm-styringen/ }));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("alertdialog")).toBeNull();
    expect(posts()).toHaveLength(0);
  });

  it("is disabled without a CSRF token", () => {
    render(<RestartPanel csrf=""/>);
    expect((screen.getByRole("button", { name: /Genstart Pi’en/ }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: /Genstart Dantherm-styringen/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("restarts the services with the CSRF token, waits and reloads when they are back", async () => {
    vi.useFakeTimers({ now: 0 });
    render(<RestartPanel csrf="csrf-1"/>);
    fireEvent.click(screen.getByRole("button", { name: /Genstart Dantherm-styringen/ }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Genstart styringen" })); });
    expect(posts()).toHaveLength(1);
    const init = posts()[0].init!;
    expect(init.method).toBe("POST");
    expect((init.headers as Record<string, string>)["X-CSRF-Token"]).toBe("csrf-1");
    expect(JSON.parse(String(init.body))).toEqual({ action: "restart_service", target: "all" });
    expect(screen.getByText("Genstarter Dantherm-styringen …")).toBeTruthy();

    api.up = false;
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(screen.getByText("Styringen starter op igen …")).toBeTruthy();
    expect(reload).not.toHaveBeenCalled();
    api.up = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("reboots the Pi only after confirmation and says so when the request is refused", async () => {
    api.fail = "Kun teknikere og administratorer kan udføre denne handling";
    render(<RestartPanel csrf="csrf-1"/>);
    fireEvent.click(screen.getByRole("button", { name: /Genstart Pi’en/ }));
    await act(async () => { fireEvent.click(within(screen.getByRole("alertdialog")).getByRole("button", { name: "Genstart Pi’en" })); });
    expect(JSON.parse(String(posts()[0].init!.body))).toEqual({ action: "reboot" });
    expect(screen.getByRole("alert").textContent).toContain("Kun teknikere og administratorer");
    expect(screen.queryByRole("status")).toBeNull();
  });
});
