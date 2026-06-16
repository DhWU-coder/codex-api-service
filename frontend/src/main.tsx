import React from "react";
import { createRoot } from "react-dom/client";

import { App } from "./App";
import "./styles.css";

// React 入口：把控制台挂载到 index.html 中的 root 节点。
createRoot(document.getElementById("root") as HTMLElement).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
