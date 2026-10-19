import { configureSearchModel, loginUser } from "./auth";
import { test, expect } from '@playwright/test';

// 每个用例先通过 API 登录管理员。
test.beforeEach(async ({ page }) => {
  await loginUser(page);
});

// 验证模型自主联网直接执行，并展示真实网页来源。
test('联网搜索作为模型工具直接执行并提供来源链接', async ({ page }) => {
  await configureSearchModel(
    page,
    `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18103'}/v1`,
    `http://127.0.0.1:${process.env.TEST_SEARCH_PORT || '18104'}`,
  );
  await page.goto('/');
  await page.getByRole('button', { name: '新建会话', exact: true }).click();
  await expect(page.getByLabel('上传会话附件')).toBeEnabled();
  await page.getByLabel('上传会话附件').setInputFiles({ name: '位置.txt', mimeType: 'text/plain', buffer: Buffer.from('我的住址：上海市青松路 88 号。') });
  await expect(page.getByText('可用 · 位置.txt', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('联网搜索：根据附件里的住址查附近咖啡店');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByText('咖啡店营业时间', { exact: true })).toBeVisible();
  await expect(page.getByText('正在生成…', { exact: true })).toBeVisible();
  await expect(page.getByText('咖啡店营业时间 9–18。[W1]', { exact: true })).toBeVisible();
  await expect(page.getByRole('link', { name: '咖啡店信息', exact: true })).toHaveAttribute('href', 'https://cafe.example/nearby');
  await page.screenshot({ path: '/private/tmp/general-agent-implementation/search-17.png', fullPage: true });
});
