import { describe, expect, it } from "vitest";
import { automationLines, nightCoolText, roomTargetText } from "./automation";

describe("automation status lines", () => {
  it("shows the target from the rooms with and without the duct offset", () => {
    expect(roomTargetText({ afterheat_room_enabled: false })).toBeNull();
    expect(roomTargetText({ afterheat_room_enabled: true, afterheat_room_temperature: 22.4, afterheat_room_rooms_target: 21.6, afterheat_room_supply_target: 21.6 }))
      .toBe("Mål fra rummene: 22,4 °C i rummene → indblæsning 21,6 °C");
    expect(roomTargetText({ afterheat_room_enabled: true, afterheat_room_temperature: 22.4, afterheat_room_rooms_target: 21.6, afterheat_room_supply_target: 22.1, afterheat_room_offset_k: 0.5, afterheat_room_state: "saturated" }))
      .toBe("Mål fra rummene: 22,4 °C i rummene → indblæsning 21,6 °C (+0,5 K kanaltab = 22,1 °C ved anlægget) · eftervarmen kan ikke følge med");
    expect(roomTargetText({ afterheat_room_enabled: true })).toContain("venter på rumtemperatur");
  });

  it("shows night cooling only in or around its window", () => {
    expect(nightCoolText({ night_cool_state: "outside_window", night_cool_reason: "Uden for natvinduet" })).toBeNull();
    expect(nightCoolText({ night_cool_state: "active", night_cool_reason: "Natkøling: varm dag i morgen (26 °C), ude 14 °C, inde 24,0 °C" }))
      .toBe("Natkøling: varm dag i morgen (26 °C), ude 14 °C, inde 24,0 °C");
  });

  it("collects only the room target and night cooling lines", () => {
    expect(automationLines({ presence_state: "home", occupied_count: 3, home_state: "home" })).toEqual([]);
    expect(automationLines({ night_cool_state: "active", night_cool_reason: "Natkøling: kører" })).toEqual(["Natkøling: kører"]);
  });
});
