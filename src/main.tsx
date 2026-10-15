import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./taskpane/App";
import { startTaskPane } from "./taskpane/bootstrap";
import "./taskpane/styles.css";

const rootElement = document.getElementById("root");

if (!rootElement) {
  throw new Error("FinLens root element is missing.");
}

// React mounts only after Office.onReady so the compatibility check sees an initialized Office.context.
void startTaskPane(() => {
  createRoot(rootElement).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
});
