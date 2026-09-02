import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The built SPA is served by the loopforge backend (mounted at `/` via
// --static-dir ui/dist) and uses only relative /api and /ws URLs. The dev
// server proxies both to a locally running `loopforge serve` instance.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "dist",
  },
  server: {
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8123",
        changeOrigin: true,
      },
      "/ws": {
        target: "http://127.0.0.1:8123",
        changeOrigin: true,
        ws: true,
      },
    },
  },
});
