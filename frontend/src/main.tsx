import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./styles/global.css";

async function bootstrap() {
  if (import.meta.env.DEV && new URLSearchParams(window.location.search).get("ui-audit") === "1") {
    const { installUiAuditApi } = await import("./ui-audit/installUiAuditApi");
    installUiAuditApi();
  }

  createRoot(document.getElementById("root") as HTMLElement).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void bootstrap();
