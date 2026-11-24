// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { RoomSensorQuality } from "./RoomSensorQuality";
afterEach(cleanup);
it("shows rejected stale data and distinguishes legacy unknown ages",()=>{
 const {rerender}=render(<RoomSensorQuality name="Stue" state={{smart_room_quality:{stue:{
   co2:{state:"stale",age_seconds:10860,max_age_seconds:10800,used:false},
   humidity:{state:"fresh",age_seconds:2,max_age_seconds:10800,used:true},
 }}}}/>);
 expect(screen.getByLabelText("Stue målingernes kvalitet").textContent).toContain("For gammel");
 expect(screen.queryByText(/2 min gammel/)).toBeNull();
 rerender(<RoomSensorQuality name="Stue" state={{smart_room_quality:{Stue:{
   co2:{state:"unknown_age",age_seconds:null,max_age_seconds:10800,used:true},
   humidity:{state:"unknown_age",age_seconds:null,max_age_seconds:10800,used:false},
 }}}}/>);
 expect(screen.getByText(/ældre HA-klient/)).toBeTruthy();
 expect(screen.getByText(/bruges ikke/)).toBeTruthy();
});
