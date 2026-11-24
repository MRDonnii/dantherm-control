// @vitest-environment jsdom
import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { PullToRefresh } from "./PullToRefresh";
import { usePullRefresh } from "../lib/pullRefresh";
import { syncThemeColor } from "./AppShell";

function touch(type: string, target: Element, y: number, x = 100) {
  const event = new Event(type, { bubbles: true, cancelable: true }) as Event & { touches: unknown[] };
  Object.defineProperty(event, "touches", { value: type === "touchend" ? [] : [{ clientX: x, clientY: y }] });
  act(() => { target.dispatchEvent(event); });
  return event;
}

/** A finger drag from y=100 down by `distance` px, released at the end unless `release` is false. */
function drag(target: Element, distance: number, release = true) {
  touch("touchstart", target, 100);
  let last: Event | null = null;
  for (let y = 110; y <= 100 + distance; y += 15) last = touch("touchmove", target, y);
  last = touch("touchmove", target, 100 + distance);
  if (release) touch("touchend", target, 100 + distance);
  return last;
}

function Page({ onRefresh }: { onRefresh: () => unknown }) {
  usePullRefresh(onRefresh);
  return <main><p>Indhold</p><input aria-label="felt"/><div className="hx-chart"><svg role="img" aria-label="graf"/></div></main>;
}

afterEach(() => { cleanup(); window.scrollTo = () => {}; Object.defineProperty(window, "scrollY", { value: 0, configurable: true }); });

describe("Pull to refresh", () => {
  it("shows the Danish hints while pulling and refetches the page's data on release past the threshold", async () => {
    const refresh = vi.fn(async () => {});
    const reload = vi.fn();
    render(<><PullToRefresh reload={reload}/><Page onRefresh={refresh}/></>);
    const content = screen.getByText("Indhold");
    const move = drag(content, 60, false);
    expect(screen.getByRole("status").textContent).toContain("Træk for at opdatere");
    expect(move?.defaultPrevented).toBe(true);
    touch("touchmove", content, 100 + 160);
    expect(screen.getByRole("status").textContent).toContain("Slip for at opdatere");
    touch("touchend", content, 260);
    expect(screen.getByRole("status").textContent).toContain("Opdaterer…");
    await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    expect(reload).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.queryByRole("status")).toBeNull(), { timeout: 2000 });
  });

  it("does nothing for a short pull, a scrolled page, a field or a chart", () => {
    const refresh = vi.fn();
    render(<><PullToRefresh reload={vi.fn()}/><Page onRefresh={refresh}/></>);
    drag(screen.getByText("Indhold"), 50);
    drag(screen.getByLabelText("felt"), 200);
    drag(screen.getByLabelText("graf"), 200);
    Object.defineProperty(window, "scrollY", { value: 120, configurable: true });
    drag(screen.getByText("Indhold"), 200);
    expect(refresh).not.toHaveBeenCalled();
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("does nothing while a dialog is open", () => {
    const refresh = vi.fn();
    render(<><PullToRefresh reload={vi.fn()}/><Page onRefresh={refresh}/><section role="dialog" aria-modal="true">Dialog</section></>);
    drag(screen.getByText("Indhold"), 200);
    expect(refresh).not.toHaveBeenCalled();
  });

  it("reloads the whole page when the shown page has no data refetch", async () => {
    const reload = vi.fn();
    render(<><PullToRefresh reload={reload}/><p>Uden data</p></>);
    drag(screen.getByText("Uden data"), 200);
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1));
  });
});

describe("Status bar colour", () => {
  it("follows the active theme's background", () => {
    document.head.innerHTML = '<meta name="theme-color" media="(prefers-color-scheme: light)" content="#fff"><meta name="theme-color" media="(prefers-color-scheme: dark)" content="#fff">';
    document.documentElement.style.setProperty("--body", "#0b0b0d");
    syncThemeColor();
    const metas = [...document.querySelectorAll<HTMLMetaElement>('meta[name="theme-color"]')].map(meta => meta.content);
    expect(metas).toEqual(["#0b0b0d", "#0b0b0d"]);
    document.documentElement.style.setProperty("--body", "#f7f7f8");
    syncThemeColor();
    expect(document.querySelector<HTMLMetaElement>('meta[name="theme-color"]')?.content).toBe("#f7f7f8");
  });
});
