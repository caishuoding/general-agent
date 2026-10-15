import { test, expect } from '@playwright/test';

// 真实浏览器上传、逐项批准、默认删除与保留来源标记，最后验证会话物理移除。
test('删除预览默认清关联记忆，明确保留项跨刷新可管理', async ({ page }) => {
  const base = `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18117'}/v1`;
  await page.request.put('/api/settings', { data: { base_url: base, model: 'chat', api_key: 'test-only' } });
  await page.request.put('/api/settings/embedding', { data: { base_url: base, model: 'embed', dimensions: 3, api_key: 'test-only' } });
  expect((await (await page.request.post('/api/settings/capabilities/embedding')).json()).status).toBe('passed');
  await page.goto('/');
  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.getByLabel('上传会话附件')).toBeEnabled();
  await page.getByLabel('上传会话附件').setInputFiles({ name: '朋友.txt', mimeType: 'text/plain', buffer: Buffer.from('陈敏住在杭州。陈敏喜欢乌龙茶。') });
  await expect(page.getByText('可用 · 朋友.txt', { exact: true })).toBeVisible();
  await page.getByLabel('消息', { exact: true }).fill('请从附件提取长期记忆');
  await page.getByRole('button', { name: '发送消息' }).click();
  for (const fact of ['住在杭州', '喜欢乌龙茶']) {
    const card = page.getByTestId('memory-candidate').filter({ hasText: fact });
    await card.getByLabel('事实归属').selectOption('other');
    await card.getByLabel('人物名称或关系').fill('陈敏');
    await card.getByRole('button', { name: '更新人物预览' }).click();
    await card.getByRole('button', { name: '确认保存' }).click();
    await expect(card).toContainText('已确认并保存');
  }
  await page.getByRole('button', { name: '删除附件 朋友.txt' }).click();
  const dialog = page.getByRole('dialog', { name: '删除来源影响预览' });
  await expect(dialog.getByRole('checkbox')).toHaveCount(2);
  await expect(dialog.getByRole('checkbox').first()).toBeChecked();
  await expect(dialog.getByRole('checkbox').last()).toBeChecked();
  await dialog.getByRole('checkbox', { name: /喜欢乌龙茶/ }).uncheck();
  await dialog.getByRole('button', { name: '确认删除来源' }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole('button', { name: '删除附件 朋友.txt' })).not.toBeVisible();
  await page.getByRole('button', { name: '长期记忆', exact: true }).click();
  const entry = page.getByTestId('memory-entry');
  await expect(entry).toHaveCount(1);
  await expect(entry).toContainText('喜欢乌龙茶');
  await expect(entry).toContainText('来源已删除');
  await expect(entry.getByRole('button', { name: '查看原消息' })).not.toBeVisible();
  await page.reload();
  await page.getByRole('button', { name: '删除当前会话' }).click();
  await expect(dialog.getByRole('checkbox')).toHaveCount(1);
  await dialog.getByRole('checkbox').uncheck();
  await dialog.getByRole('button', { name: '确认删除来源' }).click();
  await expect(dialog).not.toBeVisible();
  await expect(page.getByRole('navigation', { name: '会话列表' }).getByRole('button')).toHaveCount(0);
  await page.getByRole('button', { name: '长期记忆', exact: true }).click();
  await expect(entry).toHaveCount(1);
  await expect(entry).toContainText('来源已删除');
  await entry.screenshot({ path: 'test-results/source-deletion-retained.png' });
});
