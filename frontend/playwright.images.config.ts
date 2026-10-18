import { defineConfig } from '@playwright/test';
const appPort = process.env.TEST_APP_PORT || '18096';
export default defineConfig({
  testDir: './e2e', testMatch: 'images.spec.ts', workers: 1,
  use: { baseURL: `http://127.0.0.1:${appPort}`, trace: 'retain-on-failure' },
  webServer: { command: `${process.env.TEST_PYTHON || '../.venv/bin/python'} ../tests/attachments/browser_images_server.py`, url: `http://127.0.0.1:${appPort}/api/health`, reuseExistingServer: false, timeout: 30000 },
});
