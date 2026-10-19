import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "mobile-keyboard.spec.ts",
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:18100",
    viewport: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
    trace: "retain-on-failure",
  },
  webServer: {
    command: `"${process.execPath}" node_modules/vite/bin/vite.js --host 127.0.0.1 --port 18100 --strictPort`,
    url: "http://127.0.0.1:18100",
    reuseExistingServer: false,
  },
});
