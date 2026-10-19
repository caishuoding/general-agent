import { configureChatModel, loginUser } from "./auth";
import { test, expect } from '@playwright/test';

// 每个用例先通过 API 登录管理员。
test.beforeEach(async ({ page }) => {
  await loginUser(page);
});

// 使用真实混合PDF上传，验证当前上传状态、消息归属和刷新后的全文上下文。
test('PDF当前上传状态可见，刷新后按消息展示并继续用于回答', async ({ page }) => {
  await configureChatModel(
    page,
    `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18081'}/v1`,
    'controlled',
  );
  await page.goto('/');
  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.getByLabel('上传会话附件')).toBeEnabled();
  await page.getByLabel('上传会话附件').setInputFiles('../tests/attachments/fixtures/partial.pdf');
  await expect(page.getByText('文字可用（部分） · partial.pdf', { exact: true })).toBeVisible();
  await expect(page.getByText('文字提取：1 / 2 页', { exact: true })).toBeVisible();
  await expect(page.getByText('第 2 页没有可提取文字（可能是空白或扫描图像）', { exact: true })).toBeVisible();
  await expect(page.getByText('仅提取文字；复杂图表、内嵌图片和特殊排版未被理解，需要将相关图像单独上传。', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('Readable report在哪一页？');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByText('Readable report on page one.', { exact: false })).toBeVisible();
  const sentMessage = page.locator('.message.user').filter({ hasText: 'Readable report在哪一页？' });
  await expect(sentMessage.getByText('partial.pdf', { exact: true })).toBeVisible();
  await expect(page.getByLabel('本次上传资料')).toHaveCount(0);
  await page.reload();
  await expect(page.getByText('Readable report on page one.', { exact: false })).toBeVisible();
  await expect(page.locator('.message.user').filter({ hasText: 'Readable report在哪一页？' }).getByText('partial.pdf', { exact: true })).toBeVisible();
  await expect(page.getByLabel('本次上传资料')).toHaveCount(0);
  await expect(page.getByRole('region', { name: '当前会话资料' })).toHaveCount(0);
  await expect(page.getByText(/本会话资料/)).toHaveCount(0);
  await expect(page.getByText('文字提取：1 / 2 页', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: '/private/tmp/general-agent-implementation/documents-11.png', fullPage: true });
});
