import React from "react";
import ReactDOM from "react-dom/client";
import { HashRouter } from "react-router-dom";
import { App } from "./App";
import { syncThemeColor } from "./components/AppShell";
import { readNav, readSkin, readTheme } from "./lib/appearance";
import "./styles/tokens.css";
import "./styles/app.css";
import "./styles/pro-dashboard.css";
import "./styles/pro-shell.css";
import "./styles/light.css";
import "./styles/fd-shared.css";
import "./styles/fleetdeck-app.css";
import "./styles/overview-fd.css";
import "./styles/overview-grid.css";
import "./styles/mobile.css";

// Temaet sættes før første billede, så baggrund og statuslinje ikke blinker hvidt.
const root = document.documentElement;
const theme = readTheme();
root.dataset.theme = theme !== "system" ? theme : window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
root.dataset.skin = readSkin();
root.dataset.nav = readNav();
syncThemeColor();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <HashRouter>
      <App />
    </HashRouter>
  </React.StrictMode>,
);
