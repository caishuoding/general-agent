import { test, expect } from "@playwright/test";

// 通过真实新会话展示已取得的记忆数量，以及配置失效后的明确降级。
test("新会话显示相关记忆与未读取状态", async ({ page }) => {
  const base = `http://127.0.0.1:${process.env.TEST_MODEL_PORT || "18093"}/v1`;
  await page.request.put("/api/settings", { data: { base_url: base, model: "chat", api_key: "test-only" } });
  await page.request.put("/api/settings/embedding", { data: { base_url: base, model: "embed", dimensions: 3, api_key: "test-only" } });
  await page.request.post("/api/settings/capabilities/embedding");
  await page.goto("/");
  await page.getByLabel("消息", { exact: true }).fill("请记住：我喜欢简洁的中文回答");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByText("已保存长期记忆：我喜欢简洁的中文回答", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "新建会话" }).click();
  await page.getByLabel("消息", { exact: true }).fill("按我的偏好介绍你自己");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByTestId("memory-recall")).toContainText("已参考 1 条相关长期记忆");
  await expect(page.getByText("这是受控模型的测试回答。", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("memory-recall")).toContainText("已参考 1 条相关长期记忆");
  await page.screenshot({ path: "test-results/memory-recall.png", fullPage: true });
  await page.request.put("/api/settings/embedding", { data: { base_url: base, model: "embed", dimensions: 3, api_key: "changed-test-key" } });
  await page.getByRole("button", { name: "新建会话" }).click();
  await page.getByLabel("消息", { exact: true }).fill("继续按我的偏好回答");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByTestId("memory-recall")).toContainText("长期记忆未读取");
  await expect(page.getByText("这是受控模型的测试回答。", { exact: true })).toBeVisible();
});
