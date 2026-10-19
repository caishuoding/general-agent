import { configureChatModel, loginUser } from "./auth";
import { test, expect } from '@playwright/test';

// 每个用例先通过 API 登录管理员。
test.beforeEach(async ({ page }) => {
  await loginUser(page);
});

// 从真实上传走到视觉回答，检查临时状态、消息归属和会话隔离。
test('图片上传后按消息展示并保持会话隔离', async ({ page }) => {
  await configureChatModel(
    page,
    `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18097'}/v1`,
    'controlled-image-model',
  );
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
  await expect(page.getByText('受控视觉服务返回红色。', { exact: true })).toBeVisible();
  const sentMessage = page.locator('.message.user').filter({ hasText: '这张图是什么颜色？' });
  await expect(sentMessage.getByText('图表截图.png', { exact: true })).toBeVisible();
  await expect(page.getByLabel('本次上传资料')).toHaveCount(0);
  await page.reload();
  await expect(page.getByText('受控视觉服务返回红色。', { exact: true })).toBeVisible();
  await expect(page.locator('.message.user').filter({ hasText: '这张图是什么颜色？' }).getByText('图表截图.png', { exact: true })).toBeVisible();
  await expect(page.getByLabel('本次上传资料')).toHaveCount(0);
  await expect(page.getByRole('region', { name: '当前会话资料' })).toHaveCount(0);
  await expect(page.getByText('PNG · 16 × 16 像素', { exact: true })).toHaveCount(0);
  await page.screenshot({ path: '/private/tmp/general-agent-implementation/images-12.png', fullPage: true });
  await page.getByRole('button', { name: '新建会话' }).click();
  await expect(page.getByText('可用 · 图表截图.png', { exact: true })).toHaveCount(0);
  await expect(page.getByLabel('本次上传资料')).toHaveCount(0);
  await expect(upload).toBeEnabled();
  await upload.setInputFiles({ name: '损坏.png', mimeType: 'image/png', buffer: bytes.subarray(0, 20) });
  await expect(page.getByText('失败 · 损坏.png', { exact: true })).toBeVisible();
  await expect(page.getByText('图片损坏、解码失败或尺寸过大', { exact: false })).toBeVisible();
});
