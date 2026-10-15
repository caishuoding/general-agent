import { test, expect } from '@playwright/test';

// 全文任务显示持久进度，单次预算批准只前进一步，刷新后能从已完成分段继续。
test('全文总结可查看进度并跨刷新继续预算暂停', async ({ page, request }) => {
  await request.put('/api/settings', { data: { base_url: `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18081'}/v1`, model: 'controlled', api_key: 'browser-test-key' } });
  await request.put('/api/settings/budget', { data: { monthly_limit: '0', prices: [] } });
  await page.goto('/');
  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.getByLabel('上传会话附件')).toBeEnabled();
  const lines: string[] = [];
  for (let index = 1; index <= 24; index++) lines.push(`SECTION_${index} ` + 'background '.repeat(130));
  await page.getByLabel('上传会话附件').setInputFiles({ name: '长报告.txt', mimeType: 'text/plain', buffer: Buffer.from(lines.join('\n')) });
  await expect(page.getByText('可用 · 长报告.txt', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('请完整总结这份资料');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByText('全文总结 · 已完成 0 / 9 步 · 等待预算', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '仅允许下一次云端调用并继续' }).click();
  await expect(page.getByText('全文总结 · 已完成 1 / 9 步 · 等待预算', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('全文总结 · 已完成 1 / 9 步 · 等待预算', { exact: true })).toBeVisible();
  await request.put('/api/settings/budget', { data: { monthly_limit: null, prices: [] } });
  await page.getByRole('button', { name: '重新检查并继续' }).click();
  await expect(page.getByText('全文总结 · 已完成 9 / 9 步 · 已完成', { exact: true })).toBeVisible();
  await page.getByText('本次资料范围', { exact: true }).click();
  await expect(page.getByText('长报告.txt · 24 个可处理文字片段', { exact: true })).toBeVisible();
  await page.getByRole('region', { name: '资料处理进度' }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: '/private/tmp/general-agent-implementation/summary-13.png', fullPage: true });
});
