import { Wind } from "lucide-react";
import { fmt, roomAirRows, roomAirSentence } from "../lib/roomAir";

/** Read-only: which room is closest to raising the fan, and by how much. */
export function RoomAirOverview({ state }: { state: Record<string, unknown> }) {
  const rooms = roomAirRows(state);
  return (
    <article className="surface panel-card room-air" aria-label="Luften i rummene">
      <div className="pro-card-head compact"><div><h2>Luften i rummene</h2><p>{roomAirSentence(state, rooms)}</p></div><Wind size={20}/></div>
      {rooms.length > 0 && <ul className="room-air-list">
        {rooms.map(room => (
          <li key={room.name} className={`${room.over ? "over" : ""}${room.decides ? " decides" : ""}`}>
            <div className="room-air-name"><strong>{room.name}</strong>{room.decides && <span className="room-air-tag">Bestemmer lige nu</span>}</div>
            {room.rows.map(row => (
              <div key={row.metric} className="room-air-metric" role="img" aria-label={`${row.label} ${fmt(row.value)}${row.unit}, grænsen er ${fmt(row.limit)}`}>
                <span>{row.label}</span>
                <b>{fmt(row.value)}{row.unit}</b>
                <i className={row.share >= 1 ? "over" : row.share >= 0.85 ? "near" : ""}><u style={{ width: `${Math.min(100, Math.round(row.share * 100))}%` }}/></i>
                <small>grænse {fmt(row.limit)}</small>
              </div>
            ))}
          </li>
        ))}
      </ul>}
    </article>
  );
}
