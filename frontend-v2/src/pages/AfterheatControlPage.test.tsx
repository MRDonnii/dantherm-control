// @vitest-environment jsdom
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { AfterheatControlPage } from "./AfterheatControlPage";
import sample from "./AfterheatControlPage.sample.json";

const api = vi.hoisted(() => ({ requestJson: vi.fn() }));
vi.mock("../lib/api", () => api);
class NoopObserver { observe() {} disconnect() {} }
vi.stubGlobal("ResizeObserver", NoopObserver);
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it("shows the decision, the safety sentence and what the model has learned", async () => {
  api.requestJson.mockImplementation(async (url: string) => url.startsWith("/history.json") ? { samples: [] } : sample);
  render(<AfterheatControlPage/>);
  const now = await screen.findByRole("article", { name: "Nu: beslutningen" });
  expect(within(now).getByText("Forudseende (MPC)")).toBeTruthy();
  expect(within(now).getByText("Sikkerheden greb ind: for mange skift de sidste 10 minutter")).toBeTruthy();
  expect(within(now).getByRole("img", { name: /Forventet luft de næste 12 minutter/ })).toBeTruthy();
  const learned = screen.getByRole("article", { name: "Hvad den har lært" });
  expect(within(learned).getByText("Lærer stadig")).toBeTruthy();
  expect(within(learned).getByText("ca. 4 min")).toBeTruthy();
  const perf = screen.getByRole("article", { name: "Hvor godt går det" });
  expect(within(perf).getByText("For lidt data til at sammenligne")).toBeTruthy();
  expect(within(perf).getAllByText("+1,64 K").length).toBeGreaterThan(0);
  expect(api.requestJson).toHaveBeenCalledWith("/api/afterheat/algorithm", { timeoutMs: 20000 });
  await waitFor(() => expect(api.requestJson).toHaveBeenCalledWith("/history.json?range=24h", { timeoutMs: 8000 }));
});

it("shows dashes and the HAC1 text when the fields are missing", async () => {
  api.requestJson.mockImplementation(async (url: string) => url.startsWith("/history.json") ? { samples: [] } : { now: { afterheat_algorithm_active: null, afterheat_safety_override: 0 }, performance: {} });
  render(<AfterheatControlPage/>);
  const now = await screen.findByRole("article", { name: "Nu: beslutningen" });
  expect(within(now).getByText("HAC1 styrer selv")).toBeTruthy();
  expect(within(now).getByText("Sikkerheden greb ikke ind")).toBeTruthy();
  expect(within(now).getAllByText("–").length).toBeGreaterThan(3);
});
