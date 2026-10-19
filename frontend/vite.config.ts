import { defineConfig } from "vite";
import { resolve } from "node:path";

export default defineConfig({
  server: { host: "127.0.0.1", proxy: { "/api": "http://127.0.0.1:8000" } },
  build: {
    rollupOptions: {
      input: {
        main: resolve(__dirname, "index.html"),
        admin: resolve(__dirname, "admin.html"),
      },
    },
  },
});
