import { test, expect } from "@playwright/test";

// 从浏览器验证首次配置、发送消息、刷新持久化和会话切换。
test("配置模型后可以对话并在刷新后继续", async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "个人对话空间" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page.getByLabel("API 地址", { exact: true }).fill("http://127.0.0.1:18081/v1");
  await page.getByLabel("模型名称", { exact: true }).fill("controlled-model");
  await page.getByLabel("API 密钥", { exact: true }).fill("browser-test-key");
  await page.getByRole("button", { name: "保存配置" }).click();
  await expect(page.getByRole("status")).toContainText("配置已保存");
  await page.getByRole("button", { name: "新建会话" }).click();
  await page
    .getByRole("textbox", { name: "消息", exact: true })
    .fill("帮我整理今天的计划");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(
    page.getByText("这是受控模型的测试回答。", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(
    page.getByText("这是受控模型的测试回答。", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "新建会话" }).click();
  await expect(
    page.getByText("这是受控模型的测试回答。", { exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "帮我整理今天的计划", exact: true })
    .click();
  await expect(
    page.getByText("这是受控模型的测试回答。", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await expect(
    page.getByText("长期记忆尚未启用", { exact: true }),
  ).toBeVisible();
});

// 真实接口的旧读取响应延迟到发送完成后送达，最新回答仍应保留在页面上。
test("较慢的会话读取不会覆盖刚收到的回答", async ({ page, request }) => {
  await request.put("/api/settings", {
    data: {
      base_url: "http://127.0.0.1:18081/v1",
      model: "controlled-model",
      api_key: "browser-test-key",
    },
  });
  const created = await request.post("/api/conversations", {
    data: { title: "延迟读取验收" },
  });
  const conversation = await created.json();
  await page.goto("/");
  await expect(
    page.getByRole("textbox", { name: "消息", exact: true }),
  ).toBeEnabled();
  const url = `http://127.0.0.1:18080/api/conversations/${conversation.id}`;
  let releaseResponse!: () => void;
  // 保存释放开关，只延迟真实响应，不伪造应用接口结果。
  const delayed = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  let capturedResponse!: () => void;
  // 确认服务器已读取旧消息，再允许测试发送下一条消息。
  const captured = new Promise<void>((resolve) => {
    capturedResponse = resolve;
  });
  // 缓存真实 GET 响应直到发送完成，以稳定复现网络响应乱序。
  await page.route(url, async (route) => {
    const response = await route.fetch();
    capturedResponse();
    await delayed;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "延迟读取验收", exact: true }).click();
  await captured;
  await page
    .getByRole("textbox", { name: "消息", exact: true })
    .fill("这条新消息不能消失");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(
    page.getByText("这是受控模型的测试回答。", { exact: true }),
  ).toBeVisible();
  const oldResponse = page.waitForResponse(url);
  releaseResponse();
  await (await oldResponse).finished();
  // 等待浏览器处理响应并完成一次绘制后，再检查用户可见的消息。
  await page.evaluate(async () => {
    // 两次绘制让异步读取和 React 更新都有机会完成。
    await new Promise<void>((resolve) => {
      // 下一帧继续等待一次绘制，不访问组件内部状态。
      requestAnimationFrame(function afterFirstFrame() {
        // 第二次绘制时结束等待，忽略动画帧时间参数。
        requestAnimationFrame(() => {
          resolve();
        });
      });
    });
  });
  await expect(
    page.getByText("这条新消息不能消失", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("这是受控模型的测试回答。", { exact: true }),
  ).toBeVisible();
});
