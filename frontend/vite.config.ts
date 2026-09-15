import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发模式下把 /api 与 /media 代理到本地后端（默认 8787）
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8787",
      "/media": "http://127.0.0.1:8787",
    },
  },
});
