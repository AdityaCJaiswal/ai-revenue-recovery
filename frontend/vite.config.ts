import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: vite on :5173 proxies API paths to the FastAPI backend on :8000.
// Demo: `npm run build` -> dist/ is served BY FastAPI (one process, wifi-proof).
const API_PATHS = ["/decisions", "/admin", "/health"];

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: Object.fromEntries(
      API_PATHS.map((p) => [p, { target: "http://localhost:8000", changeOrigin: true }]),
    ),
  },
  build: { outDir: "dist", sourcemap: false },
});
