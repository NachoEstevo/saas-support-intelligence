import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(
      [
        "/identity",
        "/health",
        "/conversations",
        "/jobs",
        "/approvals",
        "/tickets",
      ].map((path) => [
        path,
        { target: process.env.VITE_API_TARGET || "http://127.0.0.1:18080" },
      ]),
    ),
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test-setup.ts",
  },
});
