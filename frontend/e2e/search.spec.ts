import { test, expect } from '@playwright/test';

// 验证独立搜索设置，附件外发必须经过具体文本确认卡片。
test('联网确认卡片允许编辑、拒绝和单次批准并提供来源链接', async ({ page, request }) => {
  await request.put('/api/settings', { data: { base_url: `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18103'}/v1`, model: 'controlled-search-chat', api_key: 'test-key' } });
  await page.goto('/');
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByLabel('智谱 API 地址', { exact: true }).fill(`http://127.0.0.1:${process.env.TEST_SEARCH_PORT || '18104'}`);
  await page.getByLabel('智谱 API 密钥', { exact: true }).fill('browser-search-key');
  await page.getByRole('button', { name: '保存搜索配置', exact: true }).click();
  await expect(page.getByLabel('智谱 API 密钥', { exact: true })).toHaveValue('');
  await page.getByRole('button', { name: '新建会话', exact: true }).click();
  await expect(page.getByLabel('上传会话附件')).toBeEnabled();
  await page.getByLabel('上传会话附件').setInputFiles({ name: '位置.txt', mimeType: 'text/plain', buffer: Buffer.from('我的住址：上海市青松路 88 号。') });
  await expect(page.getByText('可用 · 位置.txt', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('联网搜索：根据附件里的住址查附近咖啡店');
  await page.getByRole('button', { name: '发送消息' }).click();
  const card = page.getByRole('region', { name: '搜索内容外发确认' });
  await expect(card.getByLabel('将发送给智谱的完整查询')).toHaveValue('上海市青松路 88 号附近咖啡店');
  await card.getByRole('button', { name: '允许发送以上内容并搜索', exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: '/private/tmp/general-agent-implementation/search-confirm-17.png', fullPage: true });
  await page.reload();
  await expect(card.getByLabel('将发送给智谱的完整查询')).toHaveValue('上海市青松路 88 号附近咖啡店');
  await card.getByLabel('将发送给智谱的完整查询').fill('上海市青松路附近咖啡店');
  await card.getByRole('button', { name: '保存查询修改', exact: true }).click();
  await expect(card.getByRole('button', { name: '允许发送以上内容并搜索', exact: true })).toBeEnabled();
  await card.getByRole('button', { name: '拒绝发送', exact: true }).click();
  await expect(page.getByText('已拒绝发送这些搜索内容，本次没有联网。', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('联网搜索：根据附件里的住址查附近咖啡店');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(card.getByLabel('将发送给智谱的完整查询')).toHaveValue('上海市青松路 88 号附近咖啡店');
  await card.getByRole('button', { name: '允许发送以上内容并搜索', exact: true }).click();
  await expect(page.getByRole('link', { name: '咖啡店信息', exact: true })).toHaveAttribute('href', 'https://cafe.example/nearby');
  await expect(page.getByText('联网搜索已完成', { exact: false })).toBeVisible();
  await page.screenshot({ path: '/private/tmp/general-agent-implementation/search-17.png', fullPage: true });
});
