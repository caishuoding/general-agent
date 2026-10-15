import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testIgnore: "capabilities.spec.ts",
  workers: 1,
  use: { baseURL: "http://127.0.0.1:18080", trace: "retain-on-failure" },
  webServer: {
    command: "../.venv/bin/python ../tests/browser_server.py",
    url: "http://127.0.0.1:18080/api/health",
    reuseExistingServer: false,
    timeout: 30000,
  },
});
