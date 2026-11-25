import { describe, expect, it } from "vitest";
import { friendlyError } from "./errorText";

describe("friendlyError", () => {
  it("names the setting and the allowed range", () => {
    expect(friendlyError("co2_setpoint udenfor gyldigt område")).toBe("CO₂-grænsen skal være mellem 500 og 2.000 ppm.");
    expect(friendlyError("Timeout")).toBe("Pi'en svarede ikke i tide. Prøv igen om lidt.");
    expect(friendlyError("noget andet")).toBe("noget andet");
  });
});
