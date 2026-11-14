/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The control plane (control.api) serves /api. `npm run dev:mock` points this at web/mock/server.mjs.
const target = process.env.PATCHWORK_API || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5180,
    strictPort: true,
    proxy: {
      "/api": {
        target,
        changeOrigin: true,
        // SSE: never buffer, never time out a long-lived stream.
        configure: (proxy) => {
          proxy.on("proxyRes", (res) => {
            if (String(res.headers["content-type"] || "").includes("text/event-stream")) {
              res.headers["cache-control"] = "no-cache";
              res.headers["x-accel-buffering"] = "no";
            }
          });
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
