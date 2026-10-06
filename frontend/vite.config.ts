/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const apiPort = process.env.API_PORT || 8000;

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": `http://127.0.0.1:${apiPort}`,
    },
  },
  test: {
    environment: "jsdom",
    globals: false,
    setupFiles: ["./src/setupTests.ts"],
    css: true,
    // Full dashboard renders (Recharts + jsdom) take several seconds on the dev host.
    testTimeout: 20000,
    unstubGlobals: true,
  },
});
