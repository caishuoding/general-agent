import { configureChatModel, loginUser } from "./auth";
import { test, expect } from '@playwright/test';

// 每个用例先通过 API 登录管理员。
test.beforeEach(async ({ page }) => {
  await loginUser(page);
});

// 在当前文字可用流程中延迟真实详情返回，验证删除旧会话资料不能覆盖新会话。
test('删除附件后的迟到详情不会覆盖新选会话', async ({ page }) => {
  const base = `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18117'}/v1`;
  await configureChatModel(page, base, 'chat');
  const a = await (await page.request.post('/api/conversations', { data: { title: '资料会话 A' } })).json();
  const b = await (await page.request.post('/api/conversations', { data: { title: '资料会话 B' } })).json();
  await page.request.post(`/api/conversations/${a.id}/messages`, { data: { content: '会话 A 独有消息' } });
  await page.request.post(`/api/conversations/${b.id}/messages`, { data: { content: '会话 B 独有消息' } });
  await page.goto('/');
  await page.getByRole('button', { name: '资料会话 A', exact: true }).click();
  await expect(page.getByText('会话 A 独有消息', { exact: true })).toBeVisible();
  await page.getByLabel('上传会话附件').setInputFiles({ name: '待删除.txt', mimeType: 'text/plain', buffer: Buffer.from('owned by A') });
  await expect(page.getByText('内容可读取 · 待删除.txt', { exact: true })).toBeVisible();
  let release!: () => void;
  let received!: () => void;
  const hold = new Promise<void>(
    // 保存放行方法，真正的接口响应在用户切换会话后再交给页面。
    (resolve) => { release = resolve; },
  );
  const arrived = new Promise<void>(
    // 确认删除引发的详情请求已经到达并取得真实后端响应。
    (resolve) => { received = resolve; },
  );
  await page.route(`**/api/conversations/${a.id}`,
    // 保留真实响应内容，仅控制HTTP返回时间，不伪造会话详情。
    async (route) => { const response = await route.fetch(); received(); await hold; await route.fulfill({ response }); },
    { times: 1 },
  );
  await page.getByRole('button', { name: '删除附件 待删除.txt' }).click();
  const dialog = page.getByRole('dialog', { name: '删除来源影响预览' });
  await dialog.getByRole('button', { name: '删除', exact: true }).click();
  try {
    await arrived;
    await page.getByRole('button', { name: '资料会话 B', exact: true }).click();
    await expect(page.getByText('会话 B 独有消息', { exact: true })).toBeVisible();
  } finally { release(); }
  await expect(dialog).not.toBeVisible();
  await expect(page.getByText('会话 B 独有消息', { exact: true })).toBeVisible();
  await expect(page.getByText('会话 A 独有消息', { exact: true })).not.toBeVisible();
});
