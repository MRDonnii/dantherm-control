import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { NightCoolCard, RoomTargetCard } from "./AutomationCards";
import { CONTROLLER_KEYS } from "../pages/SettingsPage";
import { NIGHT_COOL_SETTINGS, ROOM_TARGET_SETTINGS } from "../lib/automation";

describe("automation setting cards", () => {
  it("saves every new setting with the controller", () => {
    for (const key of [...ROOM_TARGET_SETTINGS, ...NIGHT_COOL_SETTINGS]) expect(CONTROLLER_KEYS).toContain(key);
  });

  it("room target card has its toggle, dead band, duct offset and the reason", () => {
    const c = { afterheat_room_enabled: true, afterheat_room_band_k: 0.3, afterheat_room_offset_k: 0.5, afterheat_room_temperature: 22.4,
      afterheat_room_rooms_target: 21.6, afterheat_room_supply_target: 22.1, afterheat_room_state: "in_band", afterheat_room_rooms: 6,
      afterheat_room_reason: "Mål fra rummene: 22,4 °C i rummene → indblæsning 21,6 °C" };
    const markup = renderToStaticMarkup(<RoomTargetCard lang="da" form={c} controller={c} set={() => {}} roomOptions={() => null}/>);
    for (const text of ["Styr indblæsningen efter rummene", "Dødbånd om rummålet", "Kanaltab / forskydning", "Korrektion udsugningsrum",
      "Inden for dødbåndet", "22,4 °C · 6 rum", "22,1 °C", "Mål fra rummene: 22,4 °C i rummene → indblæsning 21,6 °C"]) expect(markup).toContain(text);
    expect(markup).toContain('min="0" max="1.5"');
    expect(markup).toContain('min="-3" max="3"');
  });

  it("night cooling card shows window, thresholds and the reason", () => {
    const c = { night_cool_enabled: true, night_cool_active: true, night_cool_state: "active", night_cool_season_mean: 16.2,
      night_cool_reason: "Natkøling: varm dag i morgen (26 °C), ude 14 °C, inde 24,0 °C" };
    const markup = renderToStaticMarkup(<NightCoolCard lang="da" form={c} controller={c} set={() => {}} maxLevel={4}/>);
    for (const text of ["Natkøling efter vejrudsigt", "Varm dag i morgen fra", "Kølesæson over", "Køler", "16,2 °C", "varm dag i morgen (26 °C)"]) expect(markup).toContain(text);
    expect(markup).toContain('value="23:00"');
  });
});
