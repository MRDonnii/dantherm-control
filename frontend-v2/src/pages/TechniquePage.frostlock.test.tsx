// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TechniquePage } from "./TechniquePage";

const api = vi.hoisted(() => ({ requestJson: vi.fn() }));
vi.mock("../lib/api", () => ({ requestJson: api.requestJson, postJson: vi.fn() }));

afterEach(() => { cleanup(); api.requestJson.mockReset(); });

describe("TechniquePage frost lock row", () => {
  it("shows the frost lock state, floor and reason", async () => {
    api.requestJson.mockImplementation(async (endpoint: string) => {
      if (endpoint.startsWith("/api/controller/state")) return { afterheat_frost_lock: true, afterheat_frost_lock_reason: "Frost: loft 0,4 °C (grænse 3 °C)", afterheat_frost_lock_floor: 18, afterheat_frost_lock_enforced: true };
      return {};
    });
    render(<TechniquePage/>);
    expect(await screen.findByText("Frostlås")).toBeTruthy();
    expect(await screen.findByText("Aktiv · min. 18 °C · Frost: loft 0,4 °C (grænse 3 °C)")).toBeTruthy();
  });
});
