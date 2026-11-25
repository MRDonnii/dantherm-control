// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { CONTROLLER_KEYS, SettingsPage, settingsPatch } from "./SettingsPage";

describe("SettingsPage", () => {
  afterEach(() => localStorage.clear());

  it("sends only the settings changed on the page, not a stale copy of the rest", () => {
    const loaded = { afterheat_setpoint: 21, co2_setpoint: 800, local_normal_level: 4 };
    // HA has since set the afterheat to 22; the page still shows 21 and only the CO2 limit was changed.
    const form = { ...loaded, co2_setpoint: 850 };
    expect(settingsPatch(form, loaded)).toEqual({ co2_setpoint: 850 });
    expect(CONTROLLER_KEYS).toContain("upshift_delay_seconds");
    expect(CONTROLLER_KEYS).toContain("humidity_rise_min_rh");
    expect(CONTROLLER_KEYS).toContain("fireplace_cold_guard_enabled");
  });

  it("saves every advanced controller feature", () => {
    for (const key of ["sizing_enabled", "house_area_m2", "ceiling_height_m", "airflow_measured", "afterheat_room_enabled",
      "fireplace_auto_enabled", "fireplace_auto_source", "humidity_smart_enabled", "dry_protection_enabled", "cooling_min_on_seconds", "onewire_roles",
      "balance_enabled", "balance_extract_excess_percent", "balance_ratio_mode", "balance_duct_ratio"]) {
      expect(CONTROLLER_KEYS).toContain(key);
    }
  });

  it("groups settings into named sections with Danish help by default", () => {
    const markup = renderToStaticMarkup(<SettingsPage/>);
    for (const label of ["Følere og målerum", "Trin og luftmængde", "Hus og luftmængde", "Fugt og CO₂", "Badeværelse og tør luft", "Eftervarme og frost", "Frikøling og bypass", "Pejs og brændeovn", "Brugerflade", "Sikkerhed"]) {
      expect(markup).toContain(label);
    }
    expect(markup).toContain("Grundtrin");
    expect(markup).toContain("Luftbalance");
    expect(markup).toContain("Udsugning over indblæsning");
    expect(markup).toContain(">Manuel<");
    expect(markup).toContain('aria-current="page"');
  });

  it("groups the side menu under clear headings and links to the page Rum", () => {
    const markup = renderToStaticMarkup(<SettingsPage/>);
    const groups = [...markup.matchAll(/class="settings-nav-group">([^<]+)</g)].map(match => match[1]);
    expect(groups).toEqual(["Ventilation", "Luftkvalitet", "Varme og køl", "Følere", "Konto og system"]);
    expect(markup).toContain('href="#/rooms"');
  });

  it("has the automatic limits and response times that used to live in Home Assistant", () => {
    for (const key of ["local_min_level", "local_max_level", "downshift_delay_seconds", "boost_hold_seconds", "auto_step_rh", "auto_step_co2"]) {
      expect(CONTROLLER_KEYS).toContain(key);
    }
    const markup = renderToStaticMarkup(<SettingsPage/>);
    for (const label of ["Grænser og reaktionstid", "Laveste trin", "Højeste trin", "Vent før der skrues ned", "Hold et højt trin mindst"]) expect(markup).toContain(label);
    window.location.hash = "#/settings?section=air";
    const air = renderToStaticMarkup(<SettingsPage/>);
    expect(air).toContain("Fugt pr. trin");
    expect(air).toContain("CO₂ pr. trin");
    window.location.hash = "";
  });

  it("follows the chosen language", () => {
    localStorage.setItem("hch5-v2-lang", "en");
    const markup = renderToStaticMarkup(<SettingsPage/>);
    expect(markup).toContain("House and airflow");
    expect(markup).toContain(">Manual<");
    expect(markup).not.toContain("Hus og luftmængde");
  });

  it("offers a separate air quality ceiling in the controller form", () => {
    window.location.hash = "#/settings?section=air";
    expect(CONTROLLER_KEYS).toContain("air_quality_max_level");
    const markup = renderToStaticMarkup(<SettingsPage/>);
    expect(markup).toContain("Maks. trin ved CO₂ og fugt");
    expect(markup).toContain("Badeværelsesudtørring og PM2.5 har egne grænser.");
    window.location.hash = "";
  });
});
