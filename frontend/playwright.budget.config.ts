import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: "./e2e", testMatch: "budget.spec.ts", workers: 1,
  use: { baseURL: "http://127.0.0.1:18086", trace: "retain-on-failure" },
  webServer: {
    command: `TEST_APP_PORT=18086 TEST_MODEL_PORT=18087 ${process.env.TEST_PYTHON || "../.venv/bin/python"} ../tests/browser_capabilities_server.py`,
    url: "http://127.0.0.1:18086/api/health", reuseExistingServer: false, timeout: 30000,
  },
});
