// Tabler Icons (MIT), samme ikonsæt som Fleetdeck. Stierne er kopieret fra tabler-icons (outline, 24 px).
import type { CSSProperties } from "react";

const PATHS = {
  "home": "<path d=\"M5 12l-2 0l9 -9l9 9l-2 0\" /> <path d=\"M5 12v7a2 2 0 0 0 2 2h10a2 2 0 0 0 2 -2v-7\" /> <path d=\"M9 21v-6a2 2 0 0 1 2 -2h2a2 2 0 0 1 2 2v6\" />",
  "history": "<path d=\"M12 8l0 4l2 2\" /> <path d=\"M3.05 11a9 9 0 1 1 .5 4m-.5 5v-5h5\" />",
  "calendar-time": "<path d=\"M11.795 21h-6.795a2 2 0 0 1 -2 -2v-12a2 2 0 0 1 2 -2h12a2 2 0 0 1 2 2v4\" /> <path d=\"M14 18a4 4 0 1 0 8 0a4 4 0 1 0 -8 0\" /> <path d=\"M15 3v4\" /> <path d=\"M7 3v4\" /> <path d=\"M3 11h16\" /> <path d=\"M18 16.496v1.504l1 1\" />",
  "gauge": "<path d=\"M3 12a9 9 0 1 0 18 0a9 9 0 1 0 -18 0\" /> <path d=\"M11 12a1 1 0 1 0 2 0a1 1 0 1 0 -2 0\" /> <path d=\"M13.41 10.59l2.59 -2.59\" /> <path d=\"M7 12a5 5 0 0 1 5 -5\" />",
  "box": "<path d=\"M12 3l8 4.5l0 9l-8 4.5l-8 -4.5l0 -9l8 -4.5\" /> <path d=\"M12 12l8 -4.5\" /> <path d=\"M12 12l0 9\" /> <path d=\"M12 12l-8 -4.5\" />",
  "bolt": "<path d=\"M13 3l0 7l6 0l-8 11l0 -7l-6 0l8 -11\" />",
  "tool": "<path d=\"M7 10h3v-3l-3.5 -3.5a6 6 0 0 1 8 8l6 6a2 2 0 0 1 -3 3l-6 -6a6 6 0 0 1 -8 -8l3.5 3.5\" />",
  "ruler": "<path d=\"M5 4h14a1 1 0 0 1 1 1v5a1 1 0 0 1 -1 1h-7a1 1 0 0 0 -1 1v7a1 1 0 0 1 -1 1h-5a1 1 0 0 1 -1 -1v-14a1 1 0 0 1 1 -1\" /> <path d=\"M4 8l2 0\" /> <path d=\"M4 12l3 0\" /> <path d=\"M4 16l2 0\" /> <path d=\"M8 4l0 2\" /> <path d=\"M12 4l0 3\" /> <path d=\"M16 4l0 2\" />",
  "refresh": "<path d=\"M20 11a8.1 8.1 0 0 0 -15.5 -2m-.5 -4v4h4\" /> <path d=\"M4 13a8.1 8.1 0 0 0 15.5 2m.5 4v-4h-4\" />",
  "settings": "<path d=\"M10.325 4.317c.426 -1.756 2.924 -1.756 3.35 0a1.724 1.724 0 0 0 2.573 1.066c1.543 -.94 3.31 .826 2.37 2.37a1.724 1.724 0 0 0 1.065 2.572c1.756 .426 1.756 2.924 0 3.35a1.724 1.724 0 0 0 -1.066 2.573c.94 1.543 -.826 3.31 -2.37 2.37a1.724 1.724 0 0 0 -2.572 1.065c-.426 1.756 -2.924 1.756 -3.35 0a1.724 1.724 0 0 0 -2.573 -1.066c-1.543 .94 -3.31 -.826 -2.37 -2.37a1.724 1.724 0 0 0 -1.065 -2.572c-1.756 -.426 -1.756 -2.924 0 -3.35a1.724 1.724 0 0 0 1.066 -2.573c-.94 -1.543 .826 -3.31 2.37 -2.37c1 .608 2.296 .07 2.572 -1.065\" /> <path d=\"M9 12a3 3 0 1 0 6 0a3 3 0 0 0 -6 0\" />",
  "search": "<path d=\"M3 10a7 7 0 1 0 14 0a7 7 0 1 0 -14 0\" /> <path d=\"M21 21l-6 -6\" />",
  "sun": "<path d=\"M8 12a4 4 0 1 0 8 0a4 4 0 1 0 -8 0\" /> <path d=\"M3 12h1m8 -9v1m8 8h1m-9 8v1m-6.4 -15.4l.7 .7m12.1 -.7l-.7 .7m0 11.4l.7 .7m-12.1 -.7l-.7 .7\" />",
  "moon": "<path d=\"M12 3c.132 0 .263 0 .393 0a7.5 7.5 0 0 0 7.92 12.446a9 9 0 1 1 -8.313 -12.454l0 .008\" />",
  "dots": "<path d=\"M4 12a1 1 0 1 0 2 0a1 1 0 1 0 -2 0\" /> <path d=\"M11 12a1 1 0 1 0 2 0a1 1 0 1 0 -2 0\" /> <path d=\"M18 12a1 1 0 1 0 2 0a1 1 0 1 0 -2 0\" />",
  "logout": "<path d=\"M14 8v-2a2 2 0 0 0 -2 -2h-7a2 2 0 0 0 -2 2v12a2 2 0 0 0 2 2h7a2 2 0 0 0 2 -2v-2\" /> <path d=\"M9 12h12l-3 -3\" /> <path d=\"M18 15l3 -3\" />",
  "palette": "<path d=\"M12 21a9 9 0 0 1 0 -18c4.97 0 9 3.582 9 8c0 1.06 -.474 2.078 -1.318 2.828c-.844 .75 -1.989 1.172 -3.182 1.172h-2.5a2 2 0 0 0 -1 3.75a1.3 1.3 0 0 1 -1 2.25\" /> <path d=\"M7.5 10.5a1 1 0 1 0 2 0a1 1 0 1 0 -2 0\" /> <path d=\"M11.5 7.5a1 1 0 1 0 2 0a1 1 0 1 0 -2 0\" /> <path d=\"M15.5 10.5a1 1 0 1 0 2 0a1 1 0 1 0 -2 0\" />",
  "user": "<path d=\"M8 7a4 4 0 1 0 8 0a4 4 0 0 0 -8 0\" /> <path d=\"M6 21v-2a4 4 0 0 1 4 -4h4a4 4 0 0 1 4 4v2\" />",
  "layout-sidebar": "<path d=\"M4 6a2 2 0 0 1 2 -2h12a2 2 0 0 1 2 2v12a2 2 0 0 1 -2 2h-12a2 2 0 0 1 -2 -2l0 -12\" /> <path d=\"M9 4l0 16\" />",
  "layout-navbar": "<path d=\"M4 6a2 2 0 0 1 2 -2h12a2 2 0 0 1 2 2v12a2 2 0 0 1 -2 2h-12a2 2 0 0 1 -2 -2l0 -12\" /> <path d=\"M4 9l16 0\" />",
  "check": "<path d=\"M5 12l5 5l10 -10\" />",
  "chevron-down": "<path d=\"M6 9l6 6l6 -6\" />",
  "arrow-right": "<path d=\"M5 12l14 0\" /> <path d=\"M13 18l6 -6\" /> <path d=\"M13 6l6 6\" />",
  "alert-triangle": "<path d=\"M12 9v4\" /> <path d=\"M10.363 3.591l-8.106 13.534a1.914 1.914 0 0 0 1.636 2.871h16.214a1.914 1.914 0 0 0 1.636 -2.87l-8.106 -13.536a1.914 1.914 0 0 0 -3.274 0\" /> <path d=\"M12 16h.01\" />",
  "filter": "<path d=\"M4 4h16v2.172a2 2 0 0 1 -.586 1.414l-4.414 4.414v7l-6 2v-8.5l-4.48 -4.928a2 2 0 0 1 -.52 -1.345v-2.227\" />",
  "wind": "<path d=\"M5 8h8.5a2.5 2.5 0 1 0 -2.34 -3.24\" /> <path d=\"M3 12h15.5a2.5 2.5 0 1 1 -2.34 3.24\" /> <path d=\"M4 16h5.5a2.5 2.5 0 1 1 -2.34 3.24\" />",
  "clock": "<path d=\"M3 12a9 9 0 1 0 18 0a9 9 0 0 0 -18 0\" /> <path d=\"M12 7v5l3 3\" />",
  "info-circle": "<path d=\"M3 12a9 9 0 1 0 18 0a9 9 0 0 0 -18 0\" /> <path d=\"M12 9h.01\" /> <path d=\"M11 12h1v4h1\" />",
  "propeller": "<path d=\"M9 13a3 3 0 1 0 6 0a3 3 0 1 0 -6 0\" /> <path d=\"M14.167 10.5c.722 -1.538 1.156 -3.043 1.303 -4.514c.22 -1.63 -.762 -2.986 -3.47 -2.986s-3.69 1.357 -3.47 2.986c.147 1.471 .581 2.976 1.303 4.514\" /> <path d=\"M13.169 16.751c.97 1.395 2.057 2.523 3.257 3.386c1.3 1 2.967 .833 4.321 -1.512c1.354 -2.345 .67 -3.874 -.85 -4.498c-1.348 -.608 -2.868 -.985 -4.562 -1.128\" /> <path d=\"M8.664 13c-1.693 .143 -3.213 .52 -4.56 1.128c-1.522 .623 -2.206 2.153 -.852 4.498s3.02 2.517 4.321 1.512c1.2 -.863 2.287 -1.991 3.258 -3.386\" />",
} as const;

export type TablerName = keyof typeof PATHS;

/** En Tabler-ikon som inline SVG (stroke = currentColor), som Fleetdecks @tabler/icons-react. */
export function TablerIcon({ name, size = 16, stroke = 1.75, className, style }: { name: TablerName; size?: number; stroke?: number; className?: string; style?: CSSProperties }) {
  return <svg className={`i${className ? ` ${className}` : ""}`} width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" style={style} dangerouslySetInnerHTML={{ __html: PATHS[name] }}/>;
}
