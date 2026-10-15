import { test, expect } from "@playwright/test";

// 忘记长期记忆后刷新列表保持删除，回到原聊天仍可回看，明确聊天删除也不受零预算阻挡。
test("忘记指定记忆保留原聊天，管理页和聊天操作一致", async ({ page }) => {
  const base = `http://127.0.0.1:${process.env.TEST_MODEL_PORT || "18111"}/v1`;
  await page.request.put("/api/settings", { data: { base_url: base, model: "chat", api_key: "browser-test-only" } });
  await page.request.put("/api/settings/embedding", { data: { base_url: base, model: "embed", dimensions: 3, api_key: "browser-test-only" } });
  await page.request.post("/api/settings/capabilities/embedding");
  await page.goto("/");
  await page.getByLabel("消息", { exact: true }).fill("请记住：我住在苏州");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByText("已保存长期记忆：我住在苏州", { exact: true })).toBeVisible();
  await page.getByLabel("消息", { exact: true }).fill("请记住：我喜欢中文回答");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByText("已保存长期记忆：我喜欢中文回答", { exact: true })).toBeVisible();
  await page.request.put("/api/settings/budget", { data: { monthly_limit: "0" } });
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await page.getByTestId("memory-entry").filter({ has: page.locator(".memory-fact", { hasText: "我住在苏州" }) }).getByRole("button", { name: "忘记这条记忆" }).click();
  await expect(page.getByText("已忘记这条长期记忆；原聊天仍保留。", { exact: true })).toBeVisible();
  await expect(page.getByTestId("memory-entry")).toHaveCount(1);
  await page.screenshot({ path: "test-results/memory-deletion.png", fullPage: true });
  await page.reload();
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await expect(page.getByTestId("memory-entry")).toHaveCount(1);
  await page.getByRole("button", { name: "查看原消息" }).click();
  await expect(page.getByText("请记住：我住在苏州", { exact: true })).toBeVisible();
  await expect(page.getByText("这条长期记忆已忘记；原聊天仍保留。", { exact: true })).toBeVisible();
  await page.getByLabel("消息", { exact: true }).fill("删除记忆：我喜欢中文回答");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByText("已忘记这条长期记忆；原聊天仍保留。", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await expect(page.getByTestId("memory-entry")).toHaveCount(0);
});
