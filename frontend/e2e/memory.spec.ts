import { test, expect } from "@playwright/test";

// 从真实聊天明确保存，在新版管理页核对来源和持久化预算恢复。
test("明确记忆、来源跳转和保存后的预算恢复", async ({ page }) => {
  const base = `http://127.0.0.1:${process.env.TEST_MODEL_PORT || "18089"}/v1`;
  await page.request.put("/api/settings", { data: { base_url: base, model: "chat", api_key: "browser-test-only" } });
  await page.request.put("/api/settings/embedding", { data: { base_url: base, model: "embed", dimensions: 3, api_key: "browser-test-only" } });
  await page.request.post("/api/settings/capabilities/embedding");
  await page.goto("/");
  await page.getByLabel("消息", { exact: true }).fill("请记住：我喜欢简洁的中文回答");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByText("已保存长期记忆：我喜欢简洁的中文回答", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await expect(page.getByRole("heading", { name: "长期记忆" })).toBeVisible();
  await expect(page.getByTestId("memory-entry")).toHaveCount(1);
  await expect(page.getByTestId("memory-entry")).toContainText("我喜欢简洁的中文回答");
  await page.screenshot({ path: "test-results/memory-management.png", fullPage: true });
  await page.getByRole("button", { name: "查看原消息" }).click();
  await expect(page.getByText("请记住：我喜欢简洁的中文回答", { exact: true }).last()).toBeVisible();
  await page.request.put("/api/settings/budget", { data: { monthly_limit: "0.000012", prices: [{ base_url: base, model: "embed", input_per_million: "1" }, { base_url: base, model: "chat", input_per_million: "1", output_per_million: "1" }] } });
  await page.getByLabel("消息", { exact: true }).fill("请记住：我喜欢茶；再给我一个泡茶建议");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByText("已保存 · 我喜欢茶", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "仅允许下一次云端调用并继续" })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "仅允许下一次云端调用并继续" }).click();
  await expect(page.getByText("这是受控模型的测试回答。", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await expect(page.getByTestId("memory-entry")).toHaveCount(2);
});
