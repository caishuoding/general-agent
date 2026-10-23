import { test, expect } from '@playwright/test';
import { loginUser } from './auth';

test(
  '知识库上传、跨会话选择、取消选择和删除',
  /** 从真实浏览器操作独立资料入口，并核对选择由服务端持久保存。 */
  async ({ page }) => {
    await loginUser(page);
    await page.goto('/');
    await page.getByRole('button', { name: '知识库', exact: true }).click();
    await page.getByLabel('知识库名称', { exact: true }).fill('产品文档');
    await page.getByRole('button', { name: '创建知识库' }).click();
    await expect(page.getByLabel('当前知识库名称')).toHaveValue('产品文档');
    await page.getByLabel('上传知识库文档').setInputFiles({ name: 'manual.txt', mimeType: 'text/plain', buffer: Buffer.from('alpha handbook') });
    await expect(page.getByText('manual.txt', { exact: true })).toBeVisible();
    await expect(page.getByText('可检索 · 存在待重试项')).toBeVisible();
    await expect(page.getByText('向量索引失败，当前使用关键词检索，可重试。')).toBeVisible();
    await page.getByRole('button', { name: '返回对话' }).click();
    await page.getByRole('button', { name: '新建会话' }).click();
    await expect(page.getByRole('button', { name: '新建会话' })).toBeEnabled();
    await page.locator('.knowledge-selector summary').click();
    await page.getByRole('checkbox', { name: '产品文档' }).check();
    await expect(page.locator('.knowledge-selector summary')).toContainText('已选 1 个');
    await page.reload();
    await expect(page.locator('.knowledge-selector summary')).toContainText('已选 1 个');
    await page.locator('.knowledge-selector summary').click();
    await page.getByRole('checkbox', { name: '产品文档' }).uncheck();
    await expect(page.locator('.knowledge-selector summary')).toContainText('未选择');
    await page.getByRole('button', { name: '知识库', exact: true }).click();
    await expect(page.getByText('manual.txt', { exact: true })).toBeVisible();
    await page.screenshot({ path: '/tmp/general-agent-knowledge.png', fullPage: true });
    page.once('dialog',
      /** 用户点击删除后接受产品界面中的明确删除确认。 */
      (dialog) => void dialog.accept(),
    );
    await page.getByRole('button', { name: '删除文档', exact: true }).click();
    await expect(page.getByText('manual.txt', { exact: true })).toHaveCount(0);
    page.once('dialog',
      /** 清理本测试创建的知识库。 */
      (dialog) => void dialog.accept(),
    );
    await page.getByRole('button', { name: '删除知识库', exact: true }).click();
    await expect(page.getByText('创建一个知识库，开始整理资料。')).toBeVisible();
  },
);

test(
  '手机可从导航进入知识库且没有横向溢出',
  /** 在手机视口打开知识库管理，核对主操作与返回路径。 */
  async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await loginUser(page);
    await page.goto('/');
    await expect(page.getByRole('textbox', { name: '消息', exact: true })).toBeEnabled();
    await page.getByRole('button', { name: '打开会话列表' }).click();
    await page.getByRole('button', { name: '知识库', exact: true }).click();
    await expect(page.getByRole('region', { name: '知识库管理' })).toBeVisible();
    await expect(page.getByText('正在加载…', { exact: true })).toHaveCount(0);
    const overflow = await page.evaluate(
      /** 比较可视宽度与实际文档宽度，避免窄屏表单越界。 */
      () => document.documentElement.scrollWidth > window.innerWidth,
    );
    expect(overflow).toBe(false);
    await page.screenshot({ path: '/tmp/general-agent-knowledge-mobile.png', fullPage: true });
  },
);
