// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { effectiveTierLevel, levelAirflow } from "./dryAir";
import { CONTROLLER_KEYS, SettingsPage } from "../pages/SettingsPage";

describe("dry air", () => {
  it("reads the estimated airflow per level", () => {
    const controller = { airflow_plan: { levels: { "2": { supply_m3h: 150 } } } };
    expect(levelAirflow(controller, 2)).toBe(150);
    expect(levelAirflow(controller, 3)).toBeNull();
  });

  it("never caps under the house minimum", () => {
    expect(effectiveTierLevel({ dry_min_level: 1, local_min_level: 2 }, {}, 1)).toEqual({ level: 2, floor: 2, raised: true });
    expect(effectiveTierLevel({ dry_min_level: 2 }, { dry_floor_level: 2 }, 3)).toEqual({ level: 3, floor: 2, raised: false });
  });

  it("saves the new keys and shows one dry-air card in the humidity section", () => {
    for (const key of ["dry_dynamic_enabled", "dry_tier1_rh", "dry_co2_soft_enabled", "dry_co2_release_ppm", "winter_dry_enabled"]) {
      expect(CONTROLLER_KEYS).toContain(key);
    }
    window.location.hash = "#/settings?section=humidity";
    const markup = renderToStaticMarkup(createElement(SettingsPage));
    expect(markup).toContain("Fugt og tør luft");
    expect(markup.match(/<h2>Tør luft<\/h2>/g)?.length).toBe(1);
    expect(markup).not.toContain("Vinter: tør luft</h2>");
    expect(markup).not.toContain("Fugt efter absolut vandindhold");
    window.location.hash = "";
  });
});
