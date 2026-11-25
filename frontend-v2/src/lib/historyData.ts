// Shared clean-up of /history.json samples for every chart.
export type Sample = Record<string, number | null>;

const AFTER = "heating_coil_after_temperature";

// Plausible ranges. Older history can hold decode errors (e.g. 655.35 °C from
// negative temperatures before 1.7.1); they must not set a chart's scale.
const PLAUSIBLE: Record<string, [number, number]> = {
  outdoor_temp: [-40, 45], supply_temp: [-10, 50], extract_temp: [-10, 50], exhaust_temp: [-40, 45],
  [AFTER]: [-10, 60], [`${AFTER}_min`]: [-10, 60], [`${AFTER}_max`]: [-10, 60],
  heating_coil_frost_temperature: [-40, 80], flow_temperature: [-10, 95], return_temperature: [-10, 95],
  co2: [250, 5000], fan_supply_percent: [0, 100], fan_extract_percent: [0, 100],
  heat_recovery_efficiency: [0, 100], afterheat_active: [0, 1], afterheat_setpoint: [5, 40],
  afterheat_level: [0, 10], afterheat_level_target: [0, 10], afterheat_target: [5, 40],
};

export function clean(samples: Sample[]): Sample[] {
  return samples.map(sample => {
    const copy: Sample = { ...sample };
    for (const [key, [low, high]] of Object.entries(PLAUSIBLE)) {
      const value = copy[key];
      if (typeof value === "number" && (value < low || value > high)) copy[key] = null;
    }
    return copy;
  });
}


/** Flow minus return, the water's cooling across the afterheat coil. */
export function withWaterDelta(samples: Sample[]): Sample[] {
  return samples.map(sample => ({
    ...sample,
    water_delta: typeof sample.flow_temperature === "number" && typeof sample.return_temperature === "number"
      ? Math.round((sample.flow_temperature - sample.return_temperature) * 100) / 100 : null,
  }));
}

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);

/** HAC1 register 101 is 0..10 (0-10 V to the valve): each level is 10 % opening. */
export const VALVE_PERCENT_PER_LEVEL = 10;

export function valvePercent(level: unknown): number | null {
  return finite(level) ? Math.max(0, Math.min(100, Math.round(level * VALVE_PERCENT_PER_LEVEL * 10) / 10)) : null;
}

/**
 * Derived afterheat lines for the charts:
 * - afterheat_goal: the user's target ("Mål"), flat while the user leaves it alone. Older
 *   history (before gateway 1.9.0) has neither the target nor HAC1's level; its
 *   whole-degree setpoint was the target then.
 * - afterheat_sent: the setpoint actually given to HAC1 ("Sendt til HAC1"); with Pi
 *   control it moves a few degrees to make HAC1 step.
 * - afterheat_valve_percent: what really happens, HAC1's level as valve opening in %.
 * - afterheat_decision_percent: what the Pi decided, its target level (else its
 *   continuous wanted level) in %; null while HAC1 steers itself.
 */
export function withAfterheatLines(samples: Sample[]): Sample[] {
  return samples.map(sample => {
    const target = sample.afterheat_target;
    const setpoint = finite(sample.afterheat_setpoint) ? sample.afterheat_setpoint : null;
    const legacy = !finite(target) && !finite(sample.afterheat_level);
    return {
      ...sample,
      afterheat_goal: finite(target) ? target : legacy ? setpoint : null,
      afterheat_sent: finite(target) ? setpoint : null,
      afterheat_valve_percent: valvePercent(sample.afterheat_level),
      afterheat_decision_percent: valvePercent(sample.afterheat_level_target) ?? valvePercent(sample.afterheat_level_wanted),
    };
  });
}

/** Which afterheat layers the period holds data for (samples from withAfterheatLines). */
export function afterheatLayers(samples: Sample[]) {
  return {
    hasValve: samples.some(sample => finite(sample.afterheat_valve_percent)),
    hasDecision: samples.some(sample => finite(sample.afterheat_decision_percent)),
    // The setpoint sent to HAC1 gets its own line once it leaves the target: with Pi
    // control it moves several degrees, natively HAC1 gets the target in whole degrees.
    showSent: samples.some(sample => finite(sample.afterheat_sent) && finite(sample.afterheat_goal)
      && Math.abs(sample.afterheat_sent - sample.afterheat_goal) >= 0.05),
  };
}

/** The afterheat chart's labels, shared by the history page and the T2AH dialog. */
export const AFTERHEAT_CHART = {
  goal: { key: "afterheat_goal", label: "Mål", decimals: 1 },
  sent: { key: "afterheat_sent", label: "Sendt til HAC1", decimals: 1 },
  valve: {
    key: "afterheat_valve_percent", label: "Trin (0-10)", steps: true,
    decisionKey: "afterheat_decision_percent", decisionLabel: "Styringens beslutning", decisionIdle: "HAC1 styrer selv",
  },
  strip: { key: "afterheat_active", label: "Ventil åben" },
  note: "Mål er den temperatur, du har valgt. Sendt til HAC1 er det setpunkt, styringen giver HAC1; med Pi-styring "
    + "flyttes det nogle grader for at få HAC1 til at skifte trin, så luften rammer målet. Nederst i grafen (skala "
    + "0-10 til højre) viser det fyldte felt, hvilket trin HAC1 kører på, "
    + "og den stiplede linje, hvilket trin styringen vil have. Trinnet er ikke det samme som, hvor meget ventilen er åben: ventilen åbner hurtigt, så allerede de første trin giver meget varme. "
    + " Uden Pi-styring styrer HAC1 selv, og der er ingen beslutningslinje.",
} as const;
