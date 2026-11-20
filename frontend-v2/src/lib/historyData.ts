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
