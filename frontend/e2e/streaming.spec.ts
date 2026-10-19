import { configureChatModel, loginUser } from "./auth";
import { test, expect, type Page } from "@playwright/test";

// 每个用例先通过 API 登录管理员。
test.beforeEach(async ({ page }) => {
  await loginUser(page);
});

// 通过真实应用接口配置受控模型，并用页面发送当前测试消息。
async function start(page: Page, content: string) {
  await configureChatModel(
    page,
    `http://127.0.0.1:${process.env.TEST_MODEL_PORT || "18081"}/v1`,
    "controlled-model",
  );
  await page.goto("/");
  await page.getByRole("button", { name: "新建会话" }).click();
  await expect(page.getByRole("button", { name: "新建会话" })).toBeEnabled();
  await page.getByRole("textbox", { name: "消息", exact: true }).fill(content);
  await page.getByRole("button", { name: "发送消息" }).click();
}

// 完整回答尚未产生时先显示第一段，刷新后仍显示完成结果。
test("逐步显示第一段然后完成", async ({ page }) => {
  await start(page, "验证分段回答");
  await expect(page.getByText("第一段", { exact: true })).toBeVisible();
  await expect(page.getByText("正在生成…", { exact: true })).toBeVisible();
  await expect(page.getByText("第一段，第二段。", { exact: true })).toBeVisible();
  await expect(page.getByText("正在生成…", { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText("第一段，第二段。", { exact: true })).toBeVisible();
});

// 用户停止后保存部分文字及停止状态，刷新不会恢复成生成中或完成。
test("停止保留部分内容和刷新后的状态", async ({ page }) => {
  await start(page, "验证停止回答");
  await expect(page.getByText("第一段", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "停止生成", exact: true }).click();
  await expect(page.getByText("已停止 · 已保留部分内容", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("已停止 · 已保留部分内容", { exact: true })).toBeVisible();
  await expect(page.getByText("第一段", { exact: true })).toBeVisible();
  await expect(page.getByText("停止生成不会撤回已经发出的服务请求", { exact: false })).toBeVisible();
});

// 模型断流后明确展示失败，重试创建新回答尝试且不重复用户消息。
test("断流后可重试且不重复用户消息", async ({ page }) => {
  await start(page, "验证断流重试");
  await expect(page.getByText("第一段", { exact: true })).toBeVisible();
  await expect(page.getByText("模型回答流中断", { exact: false })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: "重试回答", exact: true }).click();
  await expect(page.getByText("第一段，第二段。", { exact: true })).toBeVisible();
  await expect(page.locator(".message.user .message-content").filter({ hasText: "验证断流重试" })).toHaveCount(1);
  await expect(page.locator(".message.assistant.failed")).toHaveCount(1);
});
