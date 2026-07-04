import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev: proxy /ws to the local relay so the browser can use a same-origin
// WebSocket (and we avoid CORS). Production: the relay serves the built
// web/dist on the same origin, so /ws is same-origin naturally.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/ws": { target: "ws://localhost:8765", ws: true },
    },
  },
});
