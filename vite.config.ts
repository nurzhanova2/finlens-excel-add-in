import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { createDevServerConfig } from "./config/dev-server";

export default defineConfig(({ command }) => ({
  plugins: [react()],
  // HTTPS + /api proxy only for the real dev server; Vitest and builds need no certificates.
  server: command === "serve" && !process.env.VITEST ? createDevServerConfig(process.env) : undefined,
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
}));
