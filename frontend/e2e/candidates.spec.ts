import { test, expect } from "@playwright/test";

// 原生自动保存不显示确认卡，刷新后仍可查看编辑，并通过自然语言工具修改和删除。
test("自动记忆无需确认，页面管理和对话工具共用同一条目", async ({ page }) => {
  const base = `http://127.0.0.1:${process.env.TEST_MODEL_PORT || "18109"}/v1`;
  await page.request.put("/api/settings", { data: { base_url: base, model: "chat", api_key: "test-only" } });
  await page.request.put("/api/settings/embedding", { data: { base_url: base, model: "embed", dimensions: 3, api_key: "test-only" } });
  expect((await (await page.request.post("/api/settings/capabilities/embedding")).json()).status).toBe("passed");
  await page.goto("/");
  await page.getByLabel("消息", { exact: true }).fill("我住在杭州");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByTestId("automatic-memory")).toContainText("新增 1 条");
  await expect(page.getByTestId("memory-candidate")).toHaveCount(0);
  await page.reload();
  await expect(page.getByTestId("automatic-memory")).toContainText("新增 1 条");
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await expect(page.getByRole("heading", { name: "长期记忆", exact: true })).toBeVisible();
  await expect(page.getByTestId("memory-entry")).toHaveCount(1);
  await page.getByRole("button", { name: "编辑记忆" }).click();
  await page.getByLabel("新的记忆事实").fill("我住在南京");
  await page.getByRole("button", { name: "保存更正" }).click();
  await expect(page.getByTestId("memory-entry")).toContainText("我住在南京");
  await page.getByRole("button", { name: "查看原消息" }).click();
  await page.getByLabel("消息", { exact: true }).fill("把我的居住地改成苏州");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByText("已更新这条长期记忆。", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await expect(page.getByTestId("memory-entry")).toContainText("我住在苏州");
  await page.screenshot({ path: "test-results/native-memory-tools.png", fullPage: true });
  await page.getByRole("button", { name: "查看原消息" }).click();
  await page.getByLabel("消息", { exact: true }).fill("忘掉我住在哪里这件事");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByText("已忘记这条长期记忆；原聊天仍保留。", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await expect(page.getByTestId("memory-entry")).toHaveCount(0);
  await page.reload();
  expect((await (await page.request.get("/api/memories")).json()).length).toBe(0);
});

// 自动提取失败不冒充保存，修改服务配置后可直接重试原消息，无需再次发送或批准。
test("自动记忆失败后可独立重试", async ({ page }) => {
  const base = `http://127.0.0.1:${process.env.TEST_MODEL_PORT || "18109"}/v1`;
  await page.request.put("/api/settings", { data: { base_url: base, model: "bad-json", api_key: "test-only" } });
  await page.request.put("/api/settings/embedding", { data: { base_url: base, model: "embed", dimensions: 3, api_key: "test-only" } });
  expect((await (await page.request.post("/api/settings/capabilities/embedding")).json()).status).toBe("passed");
  await page.request.post("/api/conversations", { data: {} });
  await page.goto("/");
  await page.getByLabel("消息", { exact: true }).fill("我住在杭州");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByTestId("automatic-memory").last()).toContainText("自动记忆未完成");
  await expect(page.getByRole("button", { name: "重试自动记忆" })).toBeVisible();
  await page.request.put("/api/settings", { data: { base_url: base, model: "chat", api_key: "test-only" } });
  await page.getByRole("button", { name: "重试自动记忆" }).click();
  await expect(page.getByTestId("automatic-memory").last()).toContainText("新增 1 条");
  await expect(page.getByRole("button", { name: "重试自动记忆" })).toHaveCount(0);
  await page.getByRole("button", { name: "长期记忆", exact: true }).click();
  await expect(page.getByTestId("memory-entry")).toContainText("我住在杭州");
});
