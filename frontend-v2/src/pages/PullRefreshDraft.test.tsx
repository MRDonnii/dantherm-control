// @vitest-environment jsdom
// Pull to refresh never discards unsaved edits (Change by Claude AI): a page with a draft
// reports `dirty`, and the pull then refreshes only what is not the draft.
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PullToRefresh } from "../components/PullToRefresh";
import { changedKeys, runPullRefresh, usePullRefresh } from "../lib/pullRefresh";
import { newRoom } from "../lib/balancing";
import { BalancingPage } from "./BalancingPage";
import { RoomsPage } from "./RoomsPage";
import { SchedulePage } from "./SchedulePage";
import { SettingsPage } from "./SettingsPage";

const api = vi.hoisted(() => ({ calls: [] as string[], responses: {} as Record<string, unknown> }));
vi.mock("../lib/api", () => ({
  requestJson: async (endpoint: string) => {
    api.calls.push(endpoint);
    const key = Object.keys(api.responses).find(prefix => endpoint.startsWith(prefix));
    return key ? structuredClone(api.responses[key]) : {};
  },
  postJson: async () => ({}),
}));

const pull = async () => { let result = ""; await act(async () => { result = await runPullRefresh(() => {}); }); return result; };
const callsTo = (prefix: string) => api.calls.filter(call => call.startsWith(prefix)).length;

beforeEach(() => { api.calls = []; api.responses = { "/api/auth/status": { csrf: "t" } }; });
afterEach(() => { cleanup(); localStorage.clear(); window.location.hash = ""; });

describe("the pull-refresh hook", () => {
  function Page({ dirty, refresh }: { dirty: boolean; refresh: (context: { keepDraft: boolean }) => unknown }) {
    usePullRefresh(refresh, { dirty });
    return <p>Side</p>;
  }

  it("asks a dirty page to keep its draft and a clean page to reload everything", async () => {
    const refresh = vi.fn();
    const { rerender } = render(<Page dirty={false} refresh={refresh}/>);
    expect(await pull()).toBe("data");
    expect(refresh).toHaveBeenLastCalledWith({ keepDraft: false });
    rerender(<Page dirty refresh={refresh}/>);
    expect(await pull()).toBe("kept");
    expect(refresh).toHaveBeenLastCalledWith({ keepDraft: true });
  });

  it("tells the user that the unsaved changes were kept", async () => {
    render(<><PullToRefresh reload={vi.fn()}/><Page dirty refresh={vi.fn()}/></>);
    const content = screen.getByText("Side");
    const touch = (type: string, y: number) => {
      const event = new Event(type, { bubbles: true, cancelable: true }) as Event & { touches: unknown[] };
      Object.defineProperty(event, "touches", { value: type === "touchend" ? [] : [{ clientX: 100, clientY: y }] });
      act(() => { content.dispatchEvent(event); });
    };
    touch("touchstart", 100);
    for (let y = 115; y <= 280; y += 15) touch("touchmove", y);
    touch("touchend", 280);
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Ugemte ændringer er bevaret"), { timeout: 2000 });
  });

  it("finds the changed keys of a draft", () => {
    expect(changedKeys({ a: 1, b: [1, 2], c: 3 }, { a: 1, b: [1], c: 4 }, ["a", "b", "d"])).toEqual({ b: [1, 2] });
  });
});

describe("pages keep their draft on pull to refresh", () => {
  it("Rum keeps an unsaved room name, and reloads it when there is nothing unsaved", async () => {
    api.responses["/api/controller/rooms"] = { rooms: [{ id: "stue", name: "Stue", type: "normal", priority: "auto", control: true,
      enabled: true, unit_sensor: false, sensors: {} }], catalog: [], catalog_age_seconds: 5 };
    render(<RoomsPage/>);
    const name = await screen.findByDisplayValue("Stue") as HTMLInputElement;
    fireEvent.change(name, { target: { value: "Stuen" } });
    name.focus();
    expect(await pull()).toBe("kept");
    expect(callsTo("/api/controller/rooms")).toBe(2);
    expect((screen.getByLabelText("Rummets navn") as HTMLInputElement).value).toBe("Stuen");
    expect(document.activeElement).toBe(screen.getByLabelText("Rummets navn"));
    // Undone: the next pull reloads the rooms from the Pi again.
    fireEvent.change(screen.getByLabelText("Rummets navn"), { target: { value: "Stue" } });
    expect(await pull()).toBe("data");
  });

  it("Ugeplan keeps an unsaved holiday form", async () => {
    api.responses["/api/controller/state"] = { schedule_periods: {}, schedule_enabled: false, vacation_enabled: false, max_level: 4 };
    const { container } = render(<SchedulePage/>);
    await waitFor(() => expect(callsTo("/api/controller/state")).toBe(1));
    const from = container.querySelector('input[type="datetime-local"]') as HTMLInputElement;
    fireEvent.change(from, { target: { value: "2026-12-20T08:00" } });
    expect(await pull()).toBe("kept");
    expect(callsTo("/api/controller/state")).toBe(2);
    expect((container.querySelector('input[type="datetime-local"]') as HTMLInputElement).value).toBe("2026-12-20T08:00");
  });

  it("Indstillinger keeps an unsaved field and refreshes the others", async () => {
    window.location.hash = "#/settings?section=humidity";
    api.responses["/api/controller/state"] = { bathroom_max_minutes: 120, rh_setpoint: 60 };
    const { container } = render(<SettingsPage/>);
    const field = () => container.querySelector('input[max="720"]') as HTMLInputElement;
    await waitFor(() => expect(field().value).toBe("120"));
    fireEvent.change(field(), { target: { value: "90" } });
    api.responses["/api/controller/state"] = { bathroom_max_minutes: 120, rh_setpoint: 62 };
    expect(await pull()).toBe("kept");
    expect(field().value).toBe("90");
  });

  it("Indregulering keeps an unsaved room name", async () => {
    api.responses["/api/balancing"] = { project: { rooms: [{ ...newRoom("living"), name: "Stue" }], meta: {}, measure_level: null }, reports: [] };
    api.responses["/api/controller/state"] = {};
    render(<BalancingPage/>);
    const name = await screen.findByDisplayValue("Stue");
    fireEvent.change(name, { target: { value: "Stuen" } });
    expect(await pull()).toBe("kept");
    expect(callsTo("/api/balancing")).toBe(2);
    expect(screen.getByDisplayValue("Stuen")).toBeTruthy();
  });
});
