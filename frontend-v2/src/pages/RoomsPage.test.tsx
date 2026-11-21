// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RoomsPage } from "./RoomsPage";

const api = vi.hoisted(() => ({ view: {} as Record<string, unknown>, posts: [] as unknown[] }));
vi.mock("../lib/api", () => ({
  requestJson: async (endpoint: string) => {
    if (endpoint === "/api/auth/status") return { csrf: "t" };
    if (endpoint === "/api/controller/rooms") return api.view;
    return { smart_rooms: { Køkkenet: { temperature: 21.5, humidity: 48, co2: 640 } }, max_level: 4 };
  },
  postJson: async (_endpoint: string, body: { room_setup: unknown[] }) => {
    api.posts.push(body);
    api.view = { ...api.view, rooms: body.room_setup };
    return {};
  },
}));

const CATALOG = [
  { area_id: "garagen", area_name: "Garagen", sensors: [{ entity_id: "sensor.garage_t", name: "Garage temperatur", kind: "temperature" }] },
  { area_id: "kokkenet", area_name: "Køkkenet", sensors: [
    { entity_id: "sensor.kok_t", name: "Køkken temperatur", kind: "temperature" },
    { entity_id: "sensor.kok_rh", name: "Køkken fugt", kind: "humidity" },
    { entity_id: "sensor.kok_co2", name: "Køkken CO2", kind: "co2" }] },
];

beforeEach(() => { api.posts = []; });
afterEach(() => cleanup());

describe("RoomsPage", () => {
  it("explains what to do when Home Assistant has not sent its areas", async () => {
    api.view = { rooms: [], catalog: [], catalog_age_seconds: null };
    render(<RoomsPage/>);
    expect(await screen.findByText("Home Assistant har ikke sendt sine områder endnu")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Tilføj alle rum fra HA" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("adds every room from HA, shows live values and saves the list", async () => {
    api.view = { rooms: [], catalog: CATALOG, catalog_age_seconds: 12 };
    render(<RoomsPage/>);
    fireEvent.click(await screen.findByRole("button", { name: "Tilføj alle rum fra HA" }));
    expect((screen.getByLabelText("Køkkenet rumtype") as HTMLSelectElement).value).toBe("kitchen");
    expect((screen.getByLabelText("Garagen rumtype") as HTMLSelectElement).value).toBe("excluded");
    expect((screen.getByLabelText("Køkkenet CO₂") as HTMLSelectElement).value).toBe("sensor.kok_co2");
    await waitFor(() => expect(screen.getByText("640 ppm")).toBeTruthy());
    expect(screen.getByText("21,5 °C")).toBeTruthy();
    expect(screen.getByText(/1 styrer · 0 kun måling · 1 ignoreres/)).toBeTruthy();
    fireEvent.click(screen.getByRole("switch", { name: "Køkkenet styrer ventilationen" }));
    fireEvent.click(screen.getByRole("button", { name: "Gem rum" }));
    await waitFor(() => expect(api.posts).toHaveLength(1));
    const saved = (api.posts[0] as { room_setup: { name: string; control: boolean; type: string }[] }).room_setup;
    expect(saved.map(room => [room.name, room.type, room.control])).toEqual([["Garagen", "excluded", false], ["Køkkenet", "kitchen", false]]);
    expect(await screen.findByText(/Rummene er gemt/)).toBeTruthy();
  });

  it("blocks saving duplicate names and shows bathroom limits only for bathrooms", async () => {
    api.view = { rooms: [], catalog: CATALOG, catalog_age_seconds: 12 };
    render(<RoomsPage/>);
    fireEvent.click(await screen.findByRole("button", { name: "Tilføj rum" }));
    fireEvent.click(screen.getByRole("button", { name: "Tilføj rum" }));
    const names = screen.getAllByLabelText("Rummets navn") as HTMLInputElement[];
    fireEvent.change(names[1], { target: { value: "Nyt rum" } });
    expect(screen.getByText('Navnet "Nyt rum" bruges af flere rum.')).toBeTruthy();
    expect((screen.getByRole("button", { name: "Gem rum" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByLabelText("Nyt rum fugt start")).toBeNull();
    fireEvent.change(screen.getAllByLabelText("Nyt rum rumtype")[0], { target: { value: "bathroom" } });
    expect(screen.getByLabelText("Nyt rum fugt start")).toBeTruthy();
  });
});
