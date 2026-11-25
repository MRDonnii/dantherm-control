/** Plain-Danish text for the errors the Pi sends back; unknown messages pass through unchanged. */
const RANGES: Record<string, [label: string, low: number, high: number, unit: string]> = {
  rh_setpoint: ["Fugtgrænsen", 25, 80, "%"], rh_hysteresis: ["Fugtgrænsens margen", 1, 10, "%"],
  bathroom_rh_setpoint: ["Fugtgrænsen for badeværelser", 35, 90, "%"], bathroom_rh_hysteresis: ["Margen for badeværelsets fugtgrænse", 1, 20, "%"],
  auto_step_rh: ["Fugttrinnet", 2, 20, "%"], cooling_room_setpoint: ["Rumtemperaturen for frikøling", 18, 30, "°C"],
  cooling_hysteresis: ["Margen for frikøling", 0.2, 3, "grader"], cooling_outdoor_min: ["Laveste udetemperatur for frikøling", -10, 25, "°C"],
  cooling_min_delta: ["Mindste forskel for frikøling", 0.5, 10, "grader"],
  co2_setpoint: ["CO₂-grænsen", 500, 2000, "ppm"], co2_hysteresis: ["CO₂-grænsens margen", 25, 500, "ppm"], co2_offset: ["CO₂-rettelsen", -1000, 1000, "ppm"],
  auto_step_co2: ["CO₂-trinnet", 50, 1000, "ppm"], ha_timeout_seconds: ["Ventetiden på rumdata", 60, 3600, "sekunder"],
  downshift_delay_seconds: ["Ventetiden før nedgang", 30, 3600, "sekunder"], boost_hold_seconds: ["Tiden boost holdes", 60, 3600, "sekunder"],
  cooling_start_delay_seconds: ["Ventetiden før frikøling starter", 0, 1800, "sekunder"], cooling_min_on_seconds: ["Mindste køretid for frikøling", 0, 3600, "sekunder"],
  cooling_min_off_seconds: ["Mindste pause for frikøling", 0, 3600, "sekunder"], cooling_transition_timeout_seconds: ["Tiden bypass må bruge", 30, 300, "sekunder"],
};

export function friendlyError(message: string): string {
  const range = /^(\w+) udenfor gyldigt område$/.exec(message.trim());
  if (range) {
    const entry = RANGES[range[1]];
    if (entry) return `${entry[0]} skal være mellem ${entry[1].toLocaleString("da-DK")} og ${entry[2].toLocaleString("da-DK")} ${entry[3]}.`;
    return "Et af tallene ligger uden for det tilladte område. Tjek værdierne og prøv igen.";
  }
  if (message === "Timeout") return "Pi'en svarede ikke i tide. Prøv igen om lidt.";
  return message;
}
