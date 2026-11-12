import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: the API and PayPal webhooks live on :8000; the proxy keeps everything same-origin.
export default defineConfig({
  plugins: [react()],
  envDir: "..",            // one .env in the repo root for backend AND frontend; only VITE_* vars reach the browser
  server: {
    port: 5173,
    proxy: {
      "/api": "http://localhost:8000",
      "/webhooks": "http://localhost:8000",
    },
  },
  build: { outDir: "dist", chunkSizeWarningLimit: 2600 },
});
