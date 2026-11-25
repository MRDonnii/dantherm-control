import { describe, expect, it } from "vitest";
import { roomAirRows, roomAirSentence } from "./roomAir";

const state = {
  co2_setpoint: 800, rh_setpoint: 55, bathroom_rh_setpoint: 65, pm25_setpoint: 25, pm25_enabled: true,
  effective_level: 4, effective_normal_level: 4, smart_controlling_room: "Soveværelse",
  smart_rooms: {
    Soveværelse: { co2: 715, humidity: 44, pm25: 2, control: true },
    Badeværelse: { humidity: 70, control: true },
    Køkken: { co2: 500, humidity: 40, pm25: 3, control: false },
  },
};

describe("room air overview", () => {
  it("lists rooms against their limits, deciding room first, and uses the bathroom limit", () => {
    const rooms = roomAirRows(state);
    expect(rooms[0].name).toBe("Soveværelse");
    expect(rooms[0].decides).toBe(true);
    const bath = rooms.find(room => room.name === "Badeværelse")!;
    expect(bath.rows[0].limit).toBe(65);
    expect(bath.over).toBe(true);
    expect(rooms.find(room => room.name === "Køkken")!.decides).toBe(false);
  });

  it("says plainly when everything is fine or a room is over", () => {
    const calm = { ...state, smart_rooms: { Soveværelse: state.smart_rooms.Soveværelse } };
    expect(roomAirSentence(calm, roomAirRows(calm))).toBe("Blæseren kører på normaltrin 4. Luften er god i alle rum. Tættest på grænsen er Soveværelse (CO₂ 715 ppm af 800).");
    expect(roomAirSentence(state, roomAirRows(state))).toContain("Badeværelse ligger over grænsen");
    expect(roomAirRows({})).toEqual([]);
  });
});
