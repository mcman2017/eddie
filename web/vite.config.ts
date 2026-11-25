import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: Vite on :5173 proxies the read-only API on :8765 (uvicorn eddie.api.app:app).
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/healthz": "http://127.0.0.1:8765",
    },
  },
  build: { outDir: "dist", sourcemap: false, emptyOutDir: true },
});
