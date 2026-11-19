// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { AppShell, navigation, searchPages } from "./AppShell";
import { useTopbarNotice } from "../lib/topbar-notice";

const api = vi.hoisted(() => ({ available: false, checks: vi.fn() }));
vi.mock("../lib/api", () => ({
  requestJson: async (endpoint: string) => endpoint === "/api/auth/status"
    ? { csrf: "test-token" }
    : { available: true, bus_traffic: true, version: "1.2.0-beta.36" },
  postJson: async () => {
    api.checks();
    return { update_available: api.available, available_version: "1.2.0-beta.37" };
  },
}));

function ChangeButton() {
  const { setNotice } = useTopbarNotice();
  return <button onClick={() => setNotice("Manuel valgt.")}>Gem ændring</button>;
}

afterEach(() => { cleanup(); api.checks.mockClear(); });

describe("AppShell update tab and control feedback", () => {
  it("keeps update navigation hidden when the beta channel is current", async () => {
    api.available = false;
    render(<MemoryRouter><AppShell><ChangeButton/></AppShell></MemoryRouter>);
    await waitFor(() => expect(api.checks).toHaveBeenCalled());
    expect(screen.queryByRole("link", { name: /Opdatering klar/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Gem ændring" }));
    expect(screen.getByRole("status").textContent).toContain("Manuel valgt.");
    const notice = document.querySelector(".topbar-control-notice");
    expect(notice?.parentElement?.classList.contains("topbar-actions")).toBe(true);
    expect(notice?.nextElementSibling?.classList.contains("status-chip")).toBe(true);
  });

  it("shows an update tab in the top banner only when a new beta exists", async () => {
    api.available = true;
    render(<MemoryRouter><AppShell><ChangeButton/></AppShell></MemoryRouter>);
    const tab = await screen.findByRole("link", { name: /Opdatering klar/ });
    expect(tab.getAttribute("href")).toBe("/updates");
    expect(tab.textContent).toContain("1.2.0-beta.37");
    expect(document.querySelector(".sidebar-nav a[href='/updates']")).not.toBeNull();
  });
});

describe("AppShell search (Ctrl K) in both menu placements", () => {
  afterEach(() => { localStorage.clear(); });

  it("finds every page in the menu by its name, and Min konto", () => {
    for (const item of navigation) expect(searchPages(navigation, item[1]).map(hit => hit[0])).toContain(item[0]);
    expect(searchPages(navigation, "eftervarme").map(hit => hit[0])).toEqual(["/overview", "/settings"]);
    expect(searchPages(navigation, "")).toEqual([]);
    expect(searchPages([["/settings?section=security", "Min konto", "user", null, null]], "adgangskode")).toHaveLength(1);
  });

  it("shows the search field with the menu at the top and opens a page with Enter", async () => {
    localStorage.setItem("hch5-v2-nav", "top");
    render(<MemoryRouter initialEntries={["/overview"]}><AppShell><ChangeButton/></AppShell></MemoryRouter>);
    expect(document.querySelector(".fd-topnav")).not.toBeNull();
    const input = screen.getByRole("combobox", { name: "Søg" });
    expect(input.closest(".fd-search")?.previousElementSibling?.classList.contains("fd-topnav")).toBe(true);
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });
    expect(document.activeElement).toBe(input);
    fireEvent.change(input, { target: { value: "ugepl" } });
    expect(screen.getByRole("option", { name: /Ugeplan/ }).getAttribute("aria-selected")).toBe("true");
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.queryByRole("listbox")).toBeNull());
    expect(document.querySelector(".fd-top[data-active]")?.textContent).toBe("Ugeplan");
  });

  it("has a search button for the phone that opens the field", () => {
    render(<MemoryRouter><AppShell><ChangeButton/></AppShell></MemoryRouter>);
    const button = screen.getByRole("button", { name: "Søg" });
    fireEvent.click(button);
    expect(document.querySelector(".fd-search[data-open]")).not.toBeNull();
    expect(document.activeElement).toBe(screen.getByRole("combobox", { name: "Søg" }));
  });
});
