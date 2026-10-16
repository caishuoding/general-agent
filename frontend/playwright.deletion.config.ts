import { defineConfig } from "@playwright/test";
const appPort = process.env.TEST_APP_PORT || "18110";
const modelPort = process.env.TEST_MODEL_PORT || "18111";
export default defineConfig({
  testDir: "./e2e", testMatch: "deletion.spec.ts", workers: 1,
  use: { baseURL: `http://127.0.0.1:${appPort}`, trace: "retain-on-failure" },
  webServer: { command: `TEST_APP_PORT=${appPort} TEST_MODEL_PORT=${modelPort} ${process.env.TEST_PYTHON || "../.venv/bin/python"} ../tests/browser_memory_server.py`, url: `http://127.0.0.1:${appPort}/api/health`, reuseExistingServer: false, timeout: 30000 },
});
