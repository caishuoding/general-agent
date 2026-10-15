import { defineConfig } from "@playwright/test";

const appPort = process.env.TEST_APP_PORT || "18080";

export default defineConfig({
  testDir: "./e2e",
  testIgnore: ["capabilities.spec.ts", "budget.spec.ts", "memory.spec.ts", "recall.spec.ts", "correction.spec.ts", "images.spec.ts"],
  workers: 1,
  use: { baseURL: `http://127.0.0.1:${appPort}`, trace: "retain-on-failure" },
  webServer: {
    command: `${process.env.TEST_PYTHON || "../.venv/bin/python"} ../tests/browser_server.py`,
    url: `http://127.0.0.1:${appPort}/api/health`,
    reuseExistingServer: false,
    timeout: 30000,
  },
});
