import { describe, expect, it } from "vitest";
import { describeControl } from "./control";

describe("describeControl", () => {
  it("explains the room automation driven by the unit's own CO2 sensor", () => {
    const c = describeControl({ active_master: "pi", effective_source: "ha_smart", effective_level: 4, effective_reason: "CO2 HCH5 / lokale sensorer 722 (auto)" });
    expect(c.title).toBe("Automatik med rummene");
    expect(c.level).toBe(4);
    expect(c.reason).toBe("CO₂ 722 ppm ved anlæggets egen føler");
    expect(c.footer).toBe("Pi styrer");
  });

  it("says fine dust and the calm-fan holds in plain words", () => {
    expect(describeControl({ active_master: "pi", effective_source: "ha_smart", effective_level: 5, effective_reason: "PM2.5 Køkken 108 µg/m³ (auto); Upshift delay" }).reason)
      .toBe("Fint støv 108 µg/m³ i Køkken; venter før optrapning");
    expect(describeControl({ active_master: "pi", effective_source: "ha_smart", effective_level: 4, effective_reason: "Luften er fin · normal ventilation" }).reason)
      .toBe("Luften er fin · normal ventilation");
  });

  it("translates room humidity, rise and hold reasons", () => {
    expect(describeControl({ active_master: "pi", effective_source: "ha_smart", effective_level: 5, effective_reason: "Badeværelse RH Badeværelse 71.5% / 65% (high); Boost hold" }).reason)
      .toBe("Fugt 71,5 % i Badeværelse (grænse 65 %); holder boost");
    expect(describeControl({ active_master: "pi", effective_source: "ha_smart", effective_level: 6, effective_reason: "RH rise Badeværelse +8.2%/10m (high)" }).reason)
      .toBe("Fugten stiger 8,2 % på 10 min i Badeværelse");
    const night = describeControl({ active_master: "pi", effective_source: "night_air_quality", effective_level: 3, effective_reason: "CO2 Soveværelse 1040 (high); nat: luftkvalitet begrænset til trin 4; Downshift delay" });
    expect(night.title).toBe("Nat · luftkvalitet");
    expect(night.tone).toBe("reduced");
    expect(night.reason).toBe("CO₂ 1040 ppm i Soveværelse; nat: luftkvalitet begrænset til trin 4; venter før nedgang");
  });

  it("says the air is good instead of naming a room when the fan sits on the normal step", () => {
    const calm = describeControl({ active_master: "pi", effective_source: "ha_smart", effective_level: 4, effective_normal_level: 4, effective_reason: "CO2 Soveværelse 729 (auto)", co2_setpoint: 800, smart_max_co2: 715, smart_max_co2_room: "Soveværelse" });
    expect(calm.reason).toBe("Normaltrin 4 · luften er god i alle rum (højest CO₂ 715 ppm i Soveværelse, grænsen er 800)");
    const raised = describeControl({ active_master: "pi", effective_source: "ha_smart", effective_level: 5, effective_normal_level: 4, effective_reason: "CO2 Soveværelse 889 (auto)", co2_setpoint: 800, smart_max_co2: 889, smart_max_co2_room: "Soveværelse" });
    expect(raised.reason).toBe("CO₂ 889 ppm i Soveværelse");
    expect(describeControl({ active_master: "pi", effective_source: "ha_smart", effective_level: 4, effective_reason: "PM2.5 Køkken 2 µg/m³ (auto)" }).reason).toBe("Støv 2 µg/m³ i Køkken");
  });

  it("says when HCP4 or the fireplace takes over", () => {
    expect(describeControl({ active_master: "hcp4", effective_source: "ha_smart", effective_level: 3 }).title).toBe("HCP4-panelet styrer");
    const fire = describeControl({ active_master: "pi", fireplace: true, fireplace_auto_active: true, effective_level: 3 });
    expect(fire.title).toBe("Pejsefunktion");
    expect(fire.tone).toBe("boost");
  });
});
