// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SessionContext, sessionCan, type AuthStatus } from "../lib/session";
import { AccessPage, agoText } from "./AccessPage";

const now = Date.now() / 1000;
const listing = {
  store: "ok", store_problem: "",
  rejected: { at: now - 300, ip: "10.0.0.77", hint: "", reason: "ukendt nøgle" },
  keys: [
    { id: "a1", name: "Home Assistant", scope: "control", prefix: "", hint: "K7pQ", created: now - 400 * 86400, created_by: "flyttet fra den gamle nøgle",
      moved: true, moved_from: "webui", lan_only: false, expires: null, last_used: now - 12, last_ip: "10.0.0.20", uses: 9 },
    { id: "b2", name: "Gammel test", scope: "admin", prefix: "dth_", hint: "v0Zs", created: now - 120 * 86400, created_by: "admin",
      moved: false, moved_from: "", lan_only: true, expires: null, last_used: now - 94 * 86400, last_ip: "10.0.0.112", uses: 3 },
  ],
};
const created = "dth_EKSEMPEL0ikke0en0rigtig0noegle0Qm2hT9xHc3T";
const api = vi.hoisted(() => ({ posts: [] as { endpoint: string; body: unknown }[] }));
vi.mock("../lib/api", () => ({
  requestJson: async (endpoint: string) => {
    if (endpoint === "/api/access/keys") return listing;
    if (endpoint === "/api/users") return { users: [{ username: "admin", role: "admin" }, { username: "anna", role: "user" }] };
    return { enabled: true, readable: true, fingerprint: "4F2A-91C0", created_at: now - 86400 * 30, created_by: "admin", peers: ["10.0.0.10"],
      users: ["admin"], last_used: { at: now - 60, ip: "10.0.0.29", user: "anna" }, rejected_at: null, rejected_ip: "", rejected_text: "" };
  },
  postJson: async (endpoint: string, body: unknown) => {
    api.posts.push({ endpoint, body });
    if (endpoint === "/api/access/keys/create") return { ...listing, key: created, id: "c3" };
    return listing;
  },
}));

const admin: AuthStatus = { username: "admin", role: "admin", csrf: "x", permissions: ["configure", "control", "diagnostics", "login_switch", "mail", "system", "users"] };
const page = () => render(<SessionContext.Provider value={{ auth: admin, loading: false, refresh: async () => {}, can: p => sessionCan(admin, p) }}><AccessPage/></SessionContext.Provider>);

afterEach(() => { cleanup(); api.posts.length = 0; });

describe("AccessPage", () => {
  it("lists keys with scope, moved badge and unused warning, and never offers to show a key", async () => {
    page();
    expect(await screen.findByText("Home Assistant")).toBeTruthy();
    expect(screen.getByText("Flyttet")).toBeTruthy();
    expect(screen.getAllByText("Læse og styre").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Administrator").length).toBeGreaterThan(0);
    expect(screen.getByText("Ubrugt i 90 dage")).toBeTruthy();
    expect(screen.getByText(/10\.0\.0\.77/)).toBeTruthy();
    expect(screen.queryByText(/Vis nøgle/i)).toBeNull();
    expect(screen.getByText("Login fra Kontrolrum (SSO)")).toBeTruthy();
    expect(screen.getByText("SSO · 4F2A-91C0")).toBeTruthy();
    expect(screen.getByText("1 af 2")).toBeTruthy();
  });

  it("makes a key, shows it once and forgets it", async () => {
    page();
    fireEvent.click(await screen.findByRole("button", { name: /Ny API-nøgle/ }));
    const dialog = screen.getByRole("dialog");
    fireEvent.change(within(dialog).getByPlaceholderText("Fx Kontrolrum"), { target: { value: "Kontrolrum" } });
    fireEvent.click(within(dialog).getByLabelText(/Læse og styre/));
    fireEvent.click(within(dialog).getByRole("button", { name: /Lav nøglen/ }));
    await waitFor(() => expect(screen.getByTestId("shown-key").textContent).toBe(created));
    expect(api.posts[0]).toEqual({ endpoint: "/api/access/keys/create", body: { name: "Kontrolrum", scope: "control", lan_only: true, expires_days: 0 } });
    expect(screen.getByText(/vises kun én gang/)).toBeTruthy();
    const done = screen.getByRole("button", { name: "Færdig" }) as HTMLButtonElement;
    expect(done.disabled).toBe(true);
    fireEvent.click(screen.getByLabelText(/Jeg har gemt nøglen/));
    expect(done.disabled).toBe(false);
    fireEvent.click(done);
    expect(screen.queryByText(created)).toBeNull();
  });

  it("asks before deleting and says the other keys keep working", async () => {
    page();
    fireEvent.click(await screen.findByRole("button", { name: "Slet Gammel test" }));
    expect(screen.getByText(/De andre nøgler virker som før/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Slet nøglen/ }));
    await waitFor(() => expect(api.posts[0]).toEqual({ endpoint: "/api/access/keys/delete", body: { id: "b2" } }));
  });
});

describe("agoText", () => {
  it("speaks Danish", () => {
    expect(agoText(null)).toBe("Aldrig brugt endnu");
    expect(agoText(1000, 1012)).toBe("for 12 sek.");
    expect(agoText(1000, 1000 + 240)).toBe("for 4 min.");
    expect(agoText(1000, 1000 + 94 * 86400)).toBe("for 94 dage siden");
  });
});
