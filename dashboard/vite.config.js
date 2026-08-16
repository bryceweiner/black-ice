import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "path";

const BACKEND = process.env.BLACKICE_API || "http://127.0.0.1:8080";

export default defineConfig({
  plugins: [react()],
  define: { global: "window" },
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: BACKEND, changeOrigin: true },
      "/media": { target: BACKEND, changeOrigin: true },
      "/ws": { target: BACKEND, ws: true },
    },
  },
});
