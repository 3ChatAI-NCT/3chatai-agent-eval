import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/agent-eval/",
  plugins: [react()],
  server: {
    proxy: {
      "/api/agent-eval": "http://127.0.0.1:8080",
    },
  },
});
