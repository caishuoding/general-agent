import { test, expect } from '@playwright/test';

// 通过真实上传、流式回答、刷新和切换验证会话资料与出处的完整用户路径。
test('上传文本后可引用，刷新保留而新会话不共享', async ({ page, request }) => {
  await request.put('/api/settings', { data: { base_url: `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18081'}/v1`, model: 'controlled-model', api_key: 'browser-test-key' } });
  await page.goto('/');
  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.getByLabel('上传会话附件')).toBeEnabled();
  await page.getByLabel('上传会话附件').setInputFiles({ name: '浏览器计划.md', mimeType: 'text/markdown', buffer: Buffer.from('蓝鲸项目于星期五交付。') });
  await expect(page.getByText('可用 · 浏览器计划.md', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('蓝鲸项目什么时候交付？');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByRole('link', { name: '浏览器计划.md · 第 1–1 行' })).toBeVisible();
  await expect(page.getByText('蓝鲸项目于星期五交付。', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('link', { name: '浏览器计划.md · 第 1–1 行' })).toBeVisible();
  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.getByText('可用 · 浏览器计划.md', { exact: true })).toHaveCount(0);
  await page.getByRole('button', { name: '蓝鲸项目什么时候交付？', exact: true }).click();
  await expect(page.getByText('可用 · 浏览器计划.md', { exact: true })).toBeVisible();
  await expect(page.getByLabel('上传会话附件')).toBeEnabled();
  await page.getByLabel('上传会话附件').setInputFiles({ name: '损坏.txt', mimeType: 'text/plain', buffer: Buffer.from([255, 0, 10]) });
  await expect(page.getByText('失败 · 损坏.txt', { exact: true })).toBeVisible();
  await expect(page.getByText('无法读取文字；请上传 UTF-8 编码的 TXT 或 Markdown 文件。', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('这个问题在文件失败时也要保留');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByRole('alert').filter({ hasText: '不能作为已理解的资料发送' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '消息', exact: true })).toHaveValue('这个问题在文件失败时也要保留');
  await page.screenshot({ path: '/private/tmp/general-agent-implementation/attachments-10.png', fullPage: true });
});
