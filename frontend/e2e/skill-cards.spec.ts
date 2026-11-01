import { test, expect } from '@playwright/test';
import { configureChatModel, loginUser } from './auth';

/** 使用已登录的真实技能目录验证前端完整正文拼接、名称元数据和历史展示。 */
test('技能卡片选择由前端发送完整 SKILL.md 和名称', async function skillCards({ page }) {
  await loginUser(page);
  await configureChatModel(page, `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18081'}/v1`, 'controlled-model');
  await page.goto('/');
  await page.getByRole('button', { name: '开始新对话' }).click();
  const input = page.getByRole('textbox', { name: '消息', exact: true });
  const card = page.getByRole('button', { name: '选择技能 ppt-master', exact: true });
  await expect(card).toBeVisible();
  await expect(card.locator('.skill-choice-name')).toHaveText('ppt-master');
  await expect(card).not.toContainText('AI-driven presentation workflow');
  await expect(card).toHaveAttribute('aria-pressed', 'false');
  await card.click();
  await expect(card).toHaveAttribute('aria-pressed', 'true');
  await expect(input).toHaveValue('');
  await expect(page.getByRole('button', { name: '发送消息' })).toBeDisabled();
  await input.fill('给我一份汇报建议');
  await card.click();
  await expect(card).toHaveAttribute('aria-pressed', 'false');
  await card.click();
  await expect(input).toHaveValue('给我一份汇报建议');
  await expect(input).toBeFocused();
  await page.screenshot({ path: '/private/tmp/skill-cards-desktop.png', fullPage: true });
  /** 捕获真实流式发送请求，核验完整入口由前端拼接且输入框仍只保留真实需求。 */
  const request = page.waitForRequest(function isSend(value) { return value.method() === 'POST' && value.url().endsWith('/runs'); });
  await page.getByRole('button', { name: '发送消息' }).click();
  const sent = await request;
  const payload = sent.postDataJSON();
  expect(payload.skills).toEqual(['ppt-master']);
  expect(payload.content).toContain('【Skill：ppt-master】');
  expect(payload.content).toContain('AI-driven presentation workflow');
  expect(payload.content).toContain('【用户需求】\n给我一份汇报建议');
  await expect(page.locator('.message.user').last()).toContainText('给我一份汇报建议');
  await expect(page.getByLabel('本条消息选择的技能')).toContainText('ppt-master');
  await expect(card).toHaveAttribute('aria-pressed', 'false');
  await page.reload();
  await expect(page.getByLabel('本条消息选择的技能')).toContainText('ppt-master');
  await expect(page.locator('.message.user').last()).toContainText('AI-driven presentation workflow');
});

/** 手机卡片可见且点按选择保持输入焦点，不产生横向溢出。 */
test('手机技能卡片和输入法交互保持可用', async function mobileCards({ page }) {
  await page.setViewportSize({ width: 390, height: 844 });
  await loginUser(page);
  await page.goto('/');
  const input = page.getByRole('textbox', { name: '消息', exact: true });
  const card = page.getByRole('button', { name: '选择技能 ppt-master', exact: true });
  await expect(input).toBeEnabled();
  await expect(card).toBeVisible();
  const messagesBefore = await page.locator('.message.user').count();
  await input.fill('准备中文汇报');
  await input.dispatchEvent('compositionstart');
  await page.keyboard.press('Enter');
  await expect(input).toHaveValue(/准备中文汇报/);
  await expect(page.locator('.message.user')).toHaveCount(messagesBefore);
  await input.dispatchEvent('compositionend');
  await card.click();
  await expect(input).toBeFocused();
  await expect(card).toHaveAttribute('aria-pressed', 'true');
  /** 读取实际文档尺寸，确认卡片不会让手机页面横向滚动。 */
  expect(await page.evaluate(function widthFits() { return document.documentElement.scrollWidth <= window.innerWidth; })).toBe(true);
  await page.screenshot({ path: '/private/tmp/skill-cards-mobile.png', fullPage: true });
});

/** 服务端拒绝或发送失败时，卡片选择与真实输入一起恢复且不覆盖等待期间的新草稿。 */
test('发送失败恢复技能选择但不覆盖新草稿', async function restoreSelection({ page }) {
  await loginUser(page);
  await page.goto('/');
  await page.getByRole('button', { name: '开始新对话' }).click();
  const input = page.getByRole('textbox', { name: '消息', exact: true });
  const card = page.getByRole('button', { name: '选择技能 ppt-master', exact: true });
  await card.click();
  await input.fill('旧需求');
  /** 只拦截本次发送请求，模拟后端保存前失败。 */
  await page.route('**/runs', async function failRun(route) {
    await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: '受控发送失败' }) });
  });
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(input).toHaveValue('旧需求');
  await expect(card).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('alert')).toContainText('受控发送失败');
  await page.unroute('**/runs');
  await input.fill('新需求');
  let releaseFailure!: () => void;
  /** 暂存失败放行函数，便于用户先修改草稿再触发迟到响应。 */
  function waitForRelease(resolve: () => void) { releaseFailure = resolve; }
  const delayedFailure = new Promise<void>(waitForRelease);
  let captured!: () => void;
  /** 通知测试发送请求已经到达，可安全开始编辑新草稿。 */
  function waitForCapture(resolve: () => void) { captured = resolve; }
  const requestCaptured = new Promise<void>(waitForCapture);
  /** 保留失败响应直到用户编辑新草稿，验证迟到结果不能覆盖新输入。 */
  await page.route('**/runs', async function delayRun(route) {
    captured();
    await delayedFailure;
    await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: '迟到失败' }) });
  });
  await page.getByRole('button', { name: '发送消息' }).click();
  await requestCaptured;
  await input.fill('等待期间的新草稿');
  releaseFailure();
  await expect(page.getByRole('alert')).toContainText('迟到失败');
  await expect(input).toHaveValue('等待期间的新草稿');
  await expect(card).toHaveAttribute('aria-pressed', 'false');
  await page.unroute('**/runs');
});
