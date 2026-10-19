import { expect, test, type Page } from "@playwright/test";

declare global {
  interface Window {
    setKeyboardViewport: (height: number, top: number, event?: "resize" | "scroll") => void;
  }
}

// 注入 iOS 键盘的高度缩小和视口平移，保留真实页面布局及滚动行为。
test.beforeEach(async function openMobileChat({ page }) {
  // 无原生键盘的浏览器自动化中，模拟可视视口事件驱动实际应用代码。
  await page.addInitScript(function installKeyboardViewport() {
    const viewport = Object.assign(new EventTarget(), { height: 844, width: 390, offsetTop: 0, offsetLeft: 0, scale: 1 });
    Object.defineProperty(window, "visualViewport", { value: viewport });
    // 允许测试独立触发键盘动画中的 resize 和 scroll 事件。
    window.setKeyboardViewport = function setViewport(height, top, event = "resize") {
      viewport.height = height;
      viewport.offsetTop = top;
      viewport.dispatchEvent(new Event(event));
    };
  });
  // 使用隔离的会话数据，避免布局回归测试依赖数据库或真实账号。
  await page.route("**/api/**", async function mockChat(route) {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = [];
    if (path === "/api/auth/me") body = { id: "mobile", username: "mobile_user", role: "user", must_change_password: false };
    if (path === "/api/conversations") body = [{ id: "keyboard", title: "键盘回归会话" }];
    if (path === "/api/conversations/keyboard" || path === "/api/conversations/keyboard/runs") body = {
      id: "keyboard", title: "键盘回归会话",
      // 长历史用于核对键盘打开后最新消息仍位于消息区底部。
      messages: Array.from({ length: 16 }, function makeMessage(_, index) {
        return { id: `message-${index}`, role: "assistant", content: `${index === 15 ? "最后一条消息" : "历史消息"}\n\n${"这是用于验证阅读位置的会话内容。".repeat(8)}`, status: "complete", attachments: [], sources: [], web_sources: [], created_at: "2026-10-19T10:00:00Z" };
      }),
    };
    await route.fulfill({ json: body });
  });
  await page.goto("/");
  await expect(page.getByRole("textbox", { name: "消息", exact: true })).toBeEnabled();
  await expect(page.locator("#message-message-15")).toBeVisible();
});

// 可视区域与布局区域错位时，顶部和输入区都必须留在键盘上方。
async function expectVisibleWorkspace(page: Page, height: number, top: number) {
  // 等待页面处理视口事件后的布局，而非仅检查变量值。
  await expect.poll(async function measureWorkspace() {
    const header = await page.locator(".topbar").boundingBox();
    const composer = await page.locator(".composer").boundingBox();
    const messageArea = await page.locator(".chat-scroll").boundingBox();
    return !!header && !!composer && !!messageArea && header.y >= top - 1 && composer.y + composer.height <= top + height + 1 && messageArea.height >= 80;
  }).toBe(true);
}

// 重放键盘出现、仅平移和关闭，捕捉聊天界面被推离可视区域的症状。
test("键盘缩小并平移视口时会话和输入框保持可见", async function keyboardLifecycle({ page }, testInfo) {
  await page.getByRole("textbox", { name: "消息", exact: true }).focus();
  // 模拟 Safari 打开键盘后的可视区域。
  await page.evaluate(function showKeyboard() { window.setKeyboardViewport(370, 180); });
  await expectVisibleWorkspace(page, 370, 180);
  // 已在底部阅读时，键盘打开后仍应看到最新消息末尾。
  await expect.poll(async function latestMessageGap() {
    return page.locator(".chat-scroll").evaluate(function bottomGap(element) { return element.scrollHeight - element.scrollTop - element.clientHeight; });
  }).toBeLessThan(5);
  await page.screenshot({ path: testInfo.outputPath("keyboard-fixed.png"), clip: { x: 0, y: 180, width: 390, height: 370 } });
  // 浏览器只发出 scroll 时，仍需更新顶部偏移。
  await page.evaluate(function panKeyboardViewport() { window.setKeyboardViewport(370, 240, "scroll"); });
  await expectVisibleWorkspace(page, 370, 240);
  await page.evaluate(function hideKeyboard() { window.setKeyboardViewport(844, 0); });
  await expectVisibleWorkspace(page, 844, 0);
});

// 阅读历史时打开键盘不能强制跳到底部，也不能让文档产生外层滚动。
test("键盘调整保留历史阅读位置，回到最新只滚动消息区", async function preserveReading({ page }) {
  // 在真实消息滚动容器内进入历史位置。
  await page.locator(".chat-scroll").evaluate(function readHistory(element) { element.scrollTop = 180; });
  await expect(page.getByRole("button", { name: "↓ 回到最新" })).toBeVisible();
  await page.evaluate(function resizeWhileReading() { window.setKeyboardViewport(370, 180); });
  await expectVisibleWorkspace(page, 370, 180);
  await expect.poll(async function readingPosition() { return page.locator(".chat-scroll").evaluate(function scrollPosition(element) { return element.scrollTop; }); }).toBe(180);
  await page.getByRole("button", { name: "↓ 回到最新" }).click();
  await expect.poll(async function latestMessageGap() { return page.locator(".chat-scroll").evaluate(function bottomGap(element) { return element.scrollHeight - element.scrollTop - element.clientHeight; }); }).toBeLessThan(5);
  expect(await page.evaluate(function documentScroll() { return window.scrollY; })).toBe(0);
});

// 短屏和长草稿下仍能阅读消息；触屏发送后输入焦点保持，防止键盘闪退。
test("长草稿输入和触屏发送保持紧凑布局与输入焦点", async function sendWithoutKeyboardDismissal({ page }) {
  const input = page.getByRole("textbox", { name: "消息", exact: true });
  await input.fill("第一行\n第二行\n第三行\n第四行\n第五行\n第六行");
  await page.evaluate(function showKeyboard() { window.setKeyboardViewport(370, 180); });
  await expectVisibleWorkspace(page, 370, 180);
  // 在同一触屏交互链中触发按钮，验证点击后的实际焦点和提交结果。
  await page.getByRole("button", { name: "发送消息" }).tap();
  await expect(input).toHaveValue("");
  await expect(input).toBeFocused();
  await expectVisibleWorkspace(page, 370, 180);
});
