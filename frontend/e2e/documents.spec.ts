import { test, expect } from '@playwright/test';

// 使用真实混合PDF上传，展示可读页、缺口和图片限制，回答出处定位实际读取页。
test('PDF部分可读范围与页码在上传和刷新后可见', async ({ page, request }) => {
  await request.put('/api/settings', { data: { base_url: `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18081'}/v1`, model: 'controlled', api_key: 'browser-test-key' } });
  await page.goto('/');
  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.getByLabel('上传会话附件')).toBeEnabled();
  await page.getByLabel('上传会话附件').setInputFiles('../tests/fixtures/partial.pdf');
  await expect(page.getByText('文字可用（部分） · partial.pdf', { exact: true })).toBeVisible();
  await expect(page.getByText('文字提取：1 / 2 页', { exact: true })).toBeVisible();
  await expect(page.getByText('第 2 页没有可提取文字（可能是空白或扫描图像）', { exact: true })).toBeVisible();
  await expect(page.getByText('仅提取文字；复杂图表、内嵌图片和特殊排版未被理解，需要将相关图像单独上传。', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('Readable report在哪一页？');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByRole('link', { name: 'partial.pdf · 第 1 页', exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('文字提取：1 / 2 页', { exact: true })).toBeVisible();
  await page.screenshot({ path: '/private/tmp/general-agent-implementation/documents-11.png', fullPage: true });
});
