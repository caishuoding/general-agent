import { test, expect } from '@playwright/test';

// 从真实上传和失败状态走到能力验证及原消息重试，检查图片本身出处和会话隔离。
test('图片在验证视觉能力后重试并引用图片本身', async ({ page, request }) => {
  await request.put('/api/settings', { data: { base_url: `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18097'}/v1`, model: 'controlled-image-model', api_key: 'browser-test-key' } });
  await page.goto('/');
  await page.getByRole('button', { name: '新建会话' }).click();
  const upload = page.getByLabel('上传会话附件');
  await expect(upload).toHaveAttribute('accept', /\.png.*\.jpg.*\.jpeg/);
  await expect(upload).toBeEnabled();
  const bytes = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAIAAACQkWg2AAAAF0lEQVR4nGP4z8BAEiJN9aiGUQ1DSgMAkPn/Afnh+ngAAAAASUVORK5CYII=', 'base64');
  await upload.setInputFiles({ name: '图表截图.png', mimeType: 'image/png', buffer: bytes });
  await expect(page.getByText('可用 · 图表截图.png', { exact: true })).toBeVisible();
  await expect(page.getByText('PNG · 16 × 16 像素', { exact: true })).toBeVisible();
  await page.getByRole('textbox', { name: '消息', exact: true }).fill('这张图是什么颜色？');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.getByText('当前模型的图片能力尚未验证通过。', { exact: false })).toBeVisible();
  await page.getByRole('button', { name: '设置', exact: true }).click();
  await page.getByRole('button', { name: '检查图片', exact: true }).click();
  await expect(page.getByTestId('capability-image')).toContainText('通过');
  await page.getByRole('button', { name: '这张图是什么颜色？', exact: true }).click();
  await page.getByRole('button', { name: '重试回答', exact: true }).click();
  await expect(page.getByRole('link', { name: '图表截图.png · 图片本身', exact: true })).toBeVisible();
  await expect(page.getByText('图片本身；未执行 OCR 或预先文字提取。', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByRole('link', { name: '图表截图.png · 图片本身', exact: true })).toBeVisible();
  await page.screenshot({ path: '/private/tmp/general-agent-implementation/images-12.png', fullPage: true });
  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.getByText('可用 · 图表截图.png', { exact: true })).toHaveCount(0);
  await expect(upload).toBeEnabled();
  await upload.setInputFiles({ name: '损坏.png', mimeType: 'image/png', buffer: bytes.subarray(0, 20) });
  await expect(page.getByText('失败 · 损坏.png', { exact: true })).toBeVisible();
  await expect(page.getByText('图片损坏、解码失败或尺寸过大', { exact: false })).toBeVisible();
});
