import { configureChatModel, loginUser } from "./auth";
import { test, expect } from '@playwright/test';

// 每个用例先通过 API 登录管理员。
test.beforeEach(async ({ page }) => {
  await loginUser(page);
});

// 通过真实上传、刷新、回答和切换验证临时托盘、消息归属与会话隔离。
test('上传文本只显示本次文件，刷新后按消息展示并继续用于回答', async ({ page }) => {
  await configureChatModel(
    page,
    `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18081'}/v1`,
    'controlled-model',
  );
  await page.goto('/');
  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.getByLabel('上传会话附件')).toBeEnabled();
  await page.getByLabel('上传会话附件').setInputFiles({ name: '浏览器计划.md', mimeType: 'text/markdown', buffer: Buffer.from('蓝鲸项目于星期五交付。') });
  await expect(page.getByText('可用 · 浏览器计划.md', { exact: true })).toBeVisible();
  await expect(page.getByLabel('本次上传资料')).toBeVisible();
  await page.screenshot({ path: '/private/tmp/general-agent-implementation/attachments-composer.png', fullPage: true });
  await page.reload();
  await expect(page.getByLabel('本次上传资料')).toHaveCount(0);
  await expect(page.getByRole('region', { name: '当前会话资料' })).toHaveCount(0);
  await expect(page.getByText(/本会话资料/)).toHaveCount(0);
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('蓝鲸项目什么时候交付？');
  await page.getByRole('button', { name: '发送消息' }).click();
  const sentMessage = page.locator('.message.user').filter({ hasText: '蓝鲸项目什么时候交付？' });
  await expect(sentMessage.getByText('浏览器计划.md', { exact: true })).toBeVisible();
  await expect(page.getByText('蓝鲸项目于星期五交付。', { exact: true })).toBeVisible();
  await expect(page.getByLabel('本次上传资料')).toHaveCount(0);
  await page.reload();
  await expect(page.locator('.message.user').filter({ hasText: '蓝鲸项目什么时候交付？' }).getByText('浏览器计划.md', { exact: true })).toBeVisible();
  await expect(page.getByText('蓝鲸项目于星期五交付。', { exact: true })).toBeVisible();
  await expect(page.getByLabel('本次上传资料')).toHaveCount(0);
  await expect(page.getByRole('region', { name: '当前会话资料' })).toHaveCount(0);
  await expect(page.getByText(/本会话资料/)).toHaveCount(0);
  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.getByText('可用 · 浏览器计划.md', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('本次上传资料')).toHaveCount(0);
  await expect(page.getByLabel('上传会话附件')).toBeEnabled();
  await page.getByLabel('上传会话附件').setInputFiles({ name: '损坏.txt', mimeType: 'text/plain', buffer: Buffer.from([255, 0, 10]) });
  await expect(page.getByText('失败 · 损坏.txt', { exact: true })).toBeVisible();
  await expect(page.getByText('无法读取文字；请上传 UTF-8 编码的 TXT 或 Markdown 文件。', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '蓝鲸项目什么时候交付？', exact: true }).click();
  await expect(page.getByText('浏览器计划.md', { exact: true })).toBeVisible();
  await expect(page.getByLabel('本次上传资料')).toHaveCount(0);
  await page.screenshot({ path: '/private/tmp/general-agent-implementation/attachments-10.png', fullPage: true });
});
