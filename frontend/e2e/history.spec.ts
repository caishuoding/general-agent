import { test, expect } from "@playwright/test";

// 明确查询前后核对历史隔离，并从持久出处跳回原会话中的真实消息。
test("旧聊天仅显式查询并可回到原消息", async ({ page }) => {
  const base = `http://127.0.0.1:${process.env.TEST_MODEL_PORT || "18115"}/v1`;
  await page.request.put("/api/settings", { data: { base_url: base, model: "controlled", api_key: "browser-test-only" } });
  const old = await (await page.request.post("/api/conversations", { data: { title: "蓝鲸项目讨论" } })).json();
  const source = await (await page.request.post(`/api/conversations/${old.id}/messages`, { data: { content: "蓝鲸计划周五交付，负责人小林。" } })).json();
  const sourceId = source.messages[0].id;
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话" }).click();
  await page.getByLabel("消息", { exact: true }).fill("今天聊蓝鲸计划");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.locator(".message.assistant.complete")).toHaveCount(1);
  await expect(page.getByLabel("历史聊天出处")).toHaveCount(0);
  await expect(page.getByText("蓝鲸计划周五交付，负责人小林。", { exact: true })).toHaveCount(0);
  await page.getByLabel("消息", { exact: true }).fill("查找旧聊天：蓝鲸");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.locator(".message.assistant.complete")).toHaveCount(2);
  const sources = page.getByLabel("历史聊天出处");
  await expect(sources).toContainText("未读取旧附件");
  await expect(sources).toContainText("蓝鲸计划周五交付，负责人小林。");
  await expect(sources.getByRole("button", { name: /蓝鲸项目讨论.*查看原消息/ })).toBeVisible();
  await page.reload();
  await expect(sources.getByRole("button", { name: /蓝鲸项目讨论.*查看原消息/ })).toBeVisible();
  await page.screenshot({ path: "/private/tmp/general-agent-implementation/history-16.png", fullPage: true });
  await sources.getByRole("button", { name: /蓝鲸项目讨论.*查看原消息/ }).click();
  await expect(page.locator(`#message-${sourceId}`)).toHaveClass(/source-highlight/);
  await expect(page.locator(`#message-${sourceId}`)).toContainText("蓝鲸计划周五交付，负责人小林。");
  await expect(page.getByLabel("历史聊天出处")).toHaveCount(0);
});
