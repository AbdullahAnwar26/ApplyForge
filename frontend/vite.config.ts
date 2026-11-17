/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// Development: the browser calls same-origin /api/..., and Vite forwards it to the
// Django backend. No CORS change is needed and the backend address is not baked
// into the bundle. The API token is NEVER configured here or in any VITE_ variable:
// the user pastes it on the Connect screen at runtime.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const target = env.API_PROXY_TARGET || "http://127.0.0.1:8000";
  return {
    plugins: [react()],
    server: {
      port: 5173,
      strictPort: true,
      proxy: {
        "/api": { target, changeOrigin: false },
      },
    },
    preview: {
      port: 4173,
      proxy: {
        "/api": { target, changeOrigin: false },
      },
    },
    build: { sourcemap: false },
    test: {
      environment: "jsdom",
      globals: true,
      setupFiles: ["./src/test/setup.ts"],
      restoreMocks: true,
      // Multi-step UI flows run in parallel workers; 5s (the default) is too tight on a busy machine.
      testTimeout: 20_000,
      unstubGlobals: true,
    },
  };
});
