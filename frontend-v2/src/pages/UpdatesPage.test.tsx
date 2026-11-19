// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { UpdatesPage, aheadOfReleaseText } from "./UpdatesPage";

const api = vi.hoisted(() => ({ requestJson: vi.fn(), postJson: vi.fn() }));
vi.mock("../lib/api", () => ({
  requestJson: api.requestJson,
  postJson: api.postJson,
  ApiError: class ApiError extends Error {},
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
});
