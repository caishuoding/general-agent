import { test, expect } from "@playwright/test";

// 在管理页明确修改，预算暂停如实展示，聊天再次更正后能查看新的来源。
test("管理页编辑及聊天更正使用同一长期记忆", async ({ page }) => {
  const base = `http://127.0.0.1:${process.env.TEST_MODEL_PORT || "18099"}/v1`;
  await page.request.put("/api/settings", { data: { base_url: base, model: "chat", api_key: "test-only" } });
  await page.request.put("/api/settings/embedding", { data: { base_url: base, model: "embed", dimensions: 3, api_key: "test-only" } });
  await page.request.post("/api/settings/capabilities/embedding");
  await page.goto("/");
  await page.getByLabel("消息", { exact: true }).fill("请记住：我住在杭州");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByText("已保存长期记忆：我住在杭州", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await page.getByRole("button", { name: "编辑记忆", exact: true }).click();
  await page.getByLabel("新的记忆事实").fill("我住在苏州");
  await page.request.put("/api/settings/budget", { data: { monthly_limit: "0" } });
  await page.getByRole("button", { name: "保存更正", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "云端请求已暂停" })).toBeVisible();
  await page.getByRole("button", { name: "仅允许下一次云端调用" }).click();
  await expect(page.getByTestId("memory-entry")).toContainText("我住在苏州");
  await expect(page.getByTestId("memory-entry")).toContainText("管理页更正");
  await page.reload();
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await expect(page.getByTestId("memory-entry")).toContainText("我住在苏州");
  await page.request.put("/api/settings/budget", { data: { monthly_limit: null } });
  await page.getByRole("button", { name: "新建会话" }).click();
  await page.getByLabel("消息", { exact: true }).fill("更正记忆：我住在苏州 => 我住在南京");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByText("已更正长期记忆：我住在南京", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await expect(page.getByTestId("memory-entry")).toHaveCount(1);
  await expect(page.getByTestId("memory-entry")).toContainText("我住在南京");
  await page.screenshot({ path: "test-results/memory-correction-management.png", fullPage: true });
  await page.getByRole("button", { name: "查看更正消息" }).click();
  await expect(page.getByText("已更正长期记忆：我住在南京", { exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/memory-correction.png", fullPage: true });
});
