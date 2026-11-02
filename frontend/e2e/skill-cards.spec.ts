import { test, expect } from '@playwright/test';
import { configureChatModel, loginUser } from './auth';

/** 验证 ppt-master 仅隐藏聊天选择入口，后端技能仍安装且普通发送不附带技能。 */
test('隐藏 ppt-master 选择入口并保留普通消息发送', async function skillCards({ page }) {
  await loginUser(page);
  await configureChatModel(page, `http://127.0.0.1:${process.env.TEST_MODEL_PORT || '18081'}/v1`, 'controlled-model');
  await page.goto('/');
  await page.getByRole('button', { name: '开始新对话' }).click();
  const input = page.getByRole('textbox', { name: '消息', exact: true });
  const card = page.getByRole('button', { name: '选择技能 ppt-master', exact: true });
  const catalog = await page.request.get('/api/skills');
  expect(await catalog.json()).toEqual(expect.arrayContaining([expect.objectContaining({ name: 'ppt-master' })]));
  await expect(card).toHaveCount(0);
  await expect(page.locator('.skill-picker')).toHaveCount(0);
  await expect(input).toHaveValue('');
  await expect(page.getByRole('button', { name: '发送消息' })).toBeDisabled();
  await input.fill('给我一份汇报建议');
  await expect(input).toHaveValue('给我一份汇报建议');
  /** 捕获真实流式发送请求，核验隐藏的技能不会注入普通用户消息。 */
  const request = page.waitForRequest(function isSend(value) { return value.method() === 'POST' && value.url().endsWith('/runs'); });
  await page.getByRole('button', { name: '发送消息' }).click();
  const sent = await request;
  const payload = sent.postDataJSON();
  expect(payload.skills).toEqual([]);
  expect(payload.content).toBe('给我一份汇报建议');
  await expect(page.locator('.message.user').last()).toContainText('给我一份汇报建议');
  await expect(page.getByLabel('本条消息选择的技能')).toHaveCount(0);
  await page.reload();
  await expect(card).toHaveCount(0);
  await expect(page.locator('.message.user').last()).toContainText('给我一份汇报建议');
});

/** 手机上也隐藏 ppt-master，同时保持中文输入法交互和页面宽度正常。 */
test('手机隐藏 ppt-master 后输入法交互保持可用', async function mobileCards({ page }) {
  await page.setViewportSize({ width: 390, height: 844 });
  await loginUser(page);
  await page.goto('/');
  const input = page.getByRole('textbox', { name: '消息', exact: true });
  const card = page.getByRole('button', { name: '选择技能 ppt-master', exact: true });
  await expect(input).toBeEnabled();
  await expect(card).toHaveCount(0);
  const messagesBefore = await page.locator('.message.user').count();
  await input.fill('准备中文汇报');
  await input.dispatchEvent('compositionstart');
  await page.keyboard.press('Enter');
  await expect(input).toHaveValue(/准备中文汇报/);
  await expect(page.locator('.message.user')).toHaveCount(messagesBefore);
  await input.dispatchEvent('compositionend');
  /** 读取实际文档尺寸，确认隐藏入口后手机页面没有横向滚动。 */
  expect(await page.evaluate(function widthFits() { return document.documentElement.scrollWidth <= window.innerWidth; })).toBe(true);
});

/** 隐藏技能入口后，发送失败仍恢复真实输入且不覆盖等待期间的新草稿。 */
test('隐藏技能入口后发送失败不覆盖新草稿', async function restoreSelection({ page }) {
  await loginUser(page);
  await page.goto('/');
  await page.getByRole('button', { name: '开始新对话' }).click();
  const input = page.getByRole('textbox', { name: '消息', exact: true });
  const card = page.getByRole('button', { name: '选择技能 ppt-master', exact: true });
  await expect(card).toHaveCount(0);
  await input.fill('旧需求');
  /** 只拦截本次发送请求，模拟后端保存前失败。 */
  await page.route('**/runs', async function failRun(route) {
    await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: '受控发送失败' }) });
  });
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(input).toHaveValue('旧需求');
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
  await expect(card).toHaveCount(0);
  await page.unroute('**/runs');
});
