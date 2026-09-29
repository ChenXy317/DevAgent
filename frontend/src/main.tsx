import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import { applyStoredTheme } from "./store/themeStore";
import "./index.css";

applyStoredTheme();

const root = document.getElementById("root");
if (!root) {
  throw new Error("缺少根节点");
}

createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
