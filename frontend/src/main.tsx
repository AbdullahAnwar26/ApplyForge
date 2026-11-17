import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { App } from "./App";
import { createQueryClient } from "./api/queryClient";
import { AppProviders } from "./app/AppProviders";
import "./styles/tokens.css";
import "./styles/global.css";
import "./styles/components.css";
import "./styles/shell.css";
import "./styles/features.css";

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root element.");

const queryClient = createQueryClient();

createRoot(root).render(
  <StrictMode>
    <AppProviders queryClient={queryClient}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </AppProviders>
  </StrictMode>,
);
