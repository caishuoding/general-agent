import { test, expect } from "@playwright/test";

// 从浏览器验证首次配置、发送消息、刷新持久化和会话切换。
test("配置模型后可以对话并在刷新后继续", async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "个人对话空间" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page.getByLabel("API 地址").fill("http://127.0.0.1:18081/v1");
  await page.getByLabel("模型名称").fill("controlled-model");
  await page.getByLabel("API 密钥").fill("browser-test-key");
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
