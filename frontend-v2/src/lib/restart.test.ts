import { describe, expect, it } from "vitest";
import { RESTART_ACTIONS, RESTART_TIMING, restartStep, startRestartWatch } from "./restart";

describe("waiting for the Pi after a restart", () => {
  it("sends only the allowlisted helper actions", () => {
    expect(RESTART_ACTIONS.services).toEqual({ action: "restart_service", target: "all" });
    expect(RESTART_ACTIONS.pi).toEqual({ action: "reboot" });
  });

  it("does not reload while the old services still answer just after the request", () => {
    const watch = startRestartWatch("services", 0);
    expect(restartStep(watch, true, 2_000).outcome).toBe("wait");
    expect(restartStep(watch, true, RESTART_TIMING.services.minWaitMs - 1).outcome).toBe("wait");
  });

  it("reloads when the services answer again after they went away", () => {
    let watch = startRestartWatch("services", 0);
    const down = restartStep(watch, false, 2_000);
    expect(down.outcome).toBe("wait");
    expect(down.watch.sawDown).toBe(true);
    watch = down.watch;
    expect(restartStep(watch, true, 4_000).outcome).toBe("reload");
  });

  it("reloads once the minimum wait is over even if no poll ever failed", () => {
    const watch = startRestartWatch("services", 0);
    expect(restartStep(watch, true, RESTART_TIMING.services.minWaitMs).outcome).toBe("reload");
  });

  it("does not take a glitch for a finished reboot", () => {
    const down = restartStep(startRestartWatch("pi", 0), false, 2_000).watch;
    expect(restartStep(down, true, 4_000).outcome).toBe("wait");
    expect(restartStep(down, true, RESTART_TIMING.pi.earliestMs).outcome).toBe("reload");
  });

  it("keeps waiting while the Pi is down and gives up after the limit", () => {
    const down = restartStep(startRestartWatch("pi", 0), false, 5_000).watch;
    expect(restartStep(down, false, 120_000).outcome).toBe("wait");
    expect(restartStep(down, false, RESTART_TIMING.pi.giveUpMs).outcome).toBe("timeout");
  });
});
