import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: Vite on 127.0.0.1:5173 proxies the read-only API on 127.0.0.1:8765 (uvicorn eddie.api.app:app).
// Both bind to loopback only; view them from another machine through an SSH port forward.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": "http://127.0.0.1:8765",
      "/healthz": "http://127.0.0.1:8765",
    },
  },
  preview: { host: "127.0.0.1", port: 4173, strictPort: true },
  build: { outDir: "dist", sourcemap: false, emptyOutDir: true },
});
