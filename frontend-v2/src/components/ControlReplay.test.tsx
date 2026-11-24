// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ControlReplay } from "./ControlReplay";
const api=vi.hoisted(()=>({postJson:vi.fn(),requestJson:vi.fn()}));
vi.mock("../lib/api",()=>api);
vi.mock("./HistoryChart",()=>({HistoryChart:()=>null}));
afterEach(()=>{cleanup();vi.resetAllMocks();});
it("keeps the same focused input and unsaved value when live defaults refresh",()=>{
 const {rerender}=render(<ControlReplay co2Default={1000} rhDefault={55} csrf="csrf"/>);
 const input=screen.getByLabelText("CO₂-grænse (ppm)") as HTMLInputElement;
 input.focus();fireEvent.change(input,{target:{value:"1200"}});
 rerender(<ControlReplay co2Default={900} rhDefault={50} csrf="csrf"/>);
 expect(screen.getByLabelText("CO₂-grænse (ppm)")).toBe(input);
 expect(document.activeElement).toBe(input);expect(input.value).toBe("1200");
});
it("loads priorities once, keeps drafts across polling and only submits them to replay",async()=>{
 api.requestJson.mockResolvedValue({rooms:[{name:"Stue",priority:"normal",control:true},{name:"Ude",priority:"low",control:false}]});
 api.postJson.mockResolvedValue({points:[],covered_minutes:0,different_minutes:0,humidity_available:true,note:"Smart Auto-behov"});
 const {rerender}=render(<ControlReplay co2Default={1000} rhDefault={55} csrf="csrf"/>);
 fireEvent.change(screen.getByLabelText("Prøvetilstand"),{target:{value:"smart_auto"}});
 const priority=await screen.findByLabelText("Stue prøveprioritet");
 expect(screen.queryByLabelText("Ude prøveprioritet")).toBeNull();
 priority.focus();fireEvent.change(priority,{target:{value:"critical"}});
 rerender(<ControlReplay co2Default={1000} rhDefault={55} csrf="csrf"/>);
 expect(screen.getByLabelText("Stue prøveprioritet")).toBe(priority);
 expect(document.activeElement).toBe(priority);
 expect(api.requestJson).toHaveBeenCalledOnce();
 fireEvent.click(screen.getByRole("button",{name:"Sammenlign uden at ændre"}));
 await waitFor(()=>expect(api.postJson).toHaveBeenCalledOnce());
 expect(api.postJson).toHaveBeenCalledWith("/api/controller/replay",{range:"24h",mode:"smart_auto",settings:{co2_setpoint:1000,rh_setpoint:55,room_priorities:{Stue:"critical"}}},"csrf");
});
it("sends only a replay request, preserves settings and shows its limited scope",async()=>{
 api.postJson.mockResolvedValue({points:[],covered_minutes:0,different_minutes:0,humidity_available:false,note:"Local Auto på gemte målinger."});
 render(<ControlReplay co2Default={1000} rhDefault={55} csrf="csrf"/>);
 fireEvent.click(screen.getByRole("button",{name:"Sammenlign uden at ændre"}));
 await waitFor(()=>expect(api.postJson).toHaveBeenCalledOnce());
 expect(api.postJson).toHaveBeenCalledWith("/api/controller/replay",{range:"24h",settings:{co2_setpoint:1000,rh_setpoint:55}},"csrf");
 expect(await screen.findByText(/mangler gemt fugtighed/)).toBeTruthy();
});
