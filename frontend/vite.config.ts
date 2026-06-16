import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Vite 构建配置：把 React 控制台输出到 FastAPI 可托管的静态目录。
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../codex_api_service/static/ui",
    emptyOutDir: true
  },
  test: {
    environment: "jsdom",
    globals: true
  }
});
