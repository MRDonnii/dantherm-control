// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { UpdatesPage, aheadOfReleaseText, lastAttemptText } from "./UpdatesPage";

const api = vi.hoisted(() => ({ requestJson: vi.fn(), postJson: vi.fn() }));
vi.mock("../lib/api", () => ({
  requestJson: api.requestJson,
  postJson: api.postJson,
  ApiError: class ApiError extends Error { constructor(message: string, public status?: number) { super(message); } },
}));
afterEach(() => { cleanup(); api.requestJson.mockReset(); api.postJson.mockReset(); });

const ahead = { ok: true, channel: "stable" as const, current_version: "1.6.1", available_version: "1.6.0", update_available: false, ahead_of_release: true };

describe("Opdateringer", () => {
  it("siger at installationen er nyere end seneste udgivelse", async () => {
    api.requestJson.mockResolvedValue({ csrf: "csrf-test" });
    api.postJson.mockResolvedValue(ahead);
    render(<UpdatesPage />);
    expect((await screen.findAllByText("Du kører 1.6.1 – nyere end seneste udgivelse (1.6.0)")).length).toBeGreaterThan(0);
    expect(screen.getByText("Seneste udgivelse")).toBeTruthy();
    expect(screen.queryByText("Tilgængelig")).toBeNull();
    expect(screen.queryByText("Denne kanal er opdateret.")).toBeNull();
  });

  it("viser den almindelige tekst, når versionen er den samme eller ældre", () => {
    expect(aheadOfReleaseText({ ...ahead, current_version: "1.6.0", ahead_of_release: false })).toBeNull();
    expect(aheadOfReleaseText({ ...ahead, current_version: "1.5.0", available_version: "1.6.0", ahead_of_release: false, update_available: true })).toBeNull();
    expect(aheadOfReleaseText(null)).toBeNull();
    expect(aheadOfReleaseText(ahead)).toBe("Du kører 1.6.1 – nyere end seneste udgivelse (1.6.0)");
  });

  it("viser sidste forsøg med tidspunkt, versioner og udfald", () => {
    const at = new Date(2026, 10, 19, 18, 57).getTime() / 1000;
    expect(lastAttemptText({ started_at: at, from_version: "1.6.1", target_version: "1.7.0", channel: "beta", result: "failed" }))
      .toBe("Sidste forsøg 19.11.2026 kl. 18.57 · 1.6.1 → 1.7.0 · Beta · fejlede");
    expect(lastAttemptText({ started_at: at, target_version: "1.7.5", from_version: "1.7.4", channel: "stable", result: "complete" })).toContain("1.7.4 → 1.7.5 · Stable · lykkedes");
    expect(lastAttemptText({})).toBeNull();
  });

  it("viser årsagen fra update.sh og kan skjule fejlen", async () => {
    const failed = { ok: true, channel: "beta" as const, current_version: "1.6.1", available_version: "1.6.1", update_available: false,
      update: { running: false, phase: "failed", progress: 58, detail: "Opdateringen fejlede", last_error: "update.sh stoppede med kode 2",
        started_at: 1_790_000_000, from_version: "1.6.1", target_version: "1.7.0", channel: "beta" as const, result: "failed" as const,
        log_tail: "Saved RS485 device is currently unavailable" } };
    api.requestJson.mockResolvedValue({ csrf: "csrf-test" });
    api.postJson.mockImplementation(async (_endpoint: string, body: { action: string }) => body.action === "dismiss_update_error"
      ? { ok: true, update: { ...failed.update, phase: "dismissed", last_error: null, log_tail: null, detail: "Fejlen er skjult." } }
      : failed);
    render(<UpdatesPage />);
    expect(await screen.findByText("update.sh stoppede med kode 2")).toBeTruthy();
    expect(screen.getByText("Saved RS485 device is currently unavailable")).toBeTruthy();
    expect(screen.getByText(/1\.6\.1 → 1\.7\.0 · Beta · fejlede/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Skjul" }));
    await waitFor(() => expect(screen.queryByText("update.sh stoppede med kode 2")).toBeNull());
    expect(api.postJson).toHaveBeenCalledWith("/api/admin/action", { action: "dismiss_update_error", target: undefined }, "csrf-test");
  });
  it("spørger, før den opdaterer, mens det fryser", async () => {
    const { ApiError } = await import("../lib/api");
    const offer = { ok: true, channel: "beta" as const, current_version: "1.6.1", available_version: "1.7.0", update_available: true };
    api.requestJson.mockResolvedValue({ csrf: "csrf-test" });
    api.postJson.mockImplementation(async (_endpoint: string, body: { action: string; force?: boolean }) => {
      if (body.action === "install_update" && !body.force) throw new ApiError("Det fryser.", 423);
      return offer;
    });
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<UpdatesPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Opdater nu" }));
    await waitFor(() => expect(api.postJson).toHaveBeenCalledWith("/api/admin/action",
      { action: "install_update", target: "beta", force: true }, "csrf-test"));
    expect(confirm).toHaveBeenCalled();
    confirm.mockRestore();
  });
});
