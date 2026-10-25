import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { applyWorkspacePreferences, readWorkspacePreferences } from "./workspace-preferences";
import "./styles.css";

applyWorkspacePreferences(readWorkspacePreferences());

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
