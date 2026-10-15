import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "capabilities.spec.ts",
  workers: 1,
  use: { baseURL: "http://127.0.0.1:18084", trace: "retain-on-failure" },
  webServer: {
    command: "../.venv/bin/python ../tests/browser_capabilities_server.py",
    url: "http://127.0.0.1:18084/api/health",
    reuseExistingServer: false,
    timeout: 30000,
  },
});
