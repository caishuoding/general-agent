import { test, expect } from "@playwright/test";

// 用真实界面验证独立嵌入配置、分项检查和配置变更后的结果失效。
test("设置分别检查四项能力并使旧结果失效", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page.getByLabel("API 地址", { exact: true }).fill("http://127.0.0.1:18085/v1");
  await page.getByLabel("模型名称", { exact: true }).fill("controlled-model");
  await page.getByLabel("API 密钥", { exact: true }).fill("browser-test-key");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await page.getByLabel("嵌入模型名称").fill("controlled-embedding");
  await page.getByLabel("嵌入维度").fill("3");
  await page.getByLabel("复用已保存聊天服务的地址与密钥").check();
  await page.getByRole("button", { name: "保存嵌入配置" }).click();
  await expect(page.getByText("嵌入配置已保存", { exact: false })).toBeVisible();
  await expect(page.getByText("每次检查都会发出可能计费的请求", { exact: false })).toBeVisible();
  await page.getByRole("button", { name: "检查文字" }).click();
  await expect(page.getByTestId("capability-text")).toContainText("通过");
  await page.getByRole("button", { name: "检查图片" }).click();
  await expect(page.getByTestId("capability-image")).toContainText("通过");
  await page.getByRole("button", { name: "检查工具调用" }).click();
  await expect(page.getByTestId("capability-tools")).toContainText("失败");
  await page.getByRole("button", { name: "检查嵌入" }).click();
  await expect(page.getByTestId("capability-embedding")).toContainText("维度 3");
  await page.getByLabel("模型名称", { exact: true }).fill("changed-model");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.getByTestId("capability-text")).toContainText("未验证");
  await expect(page.getByTestId("capability-embedding")).toContainText("通过");
  await page.getByTestId("capability-image").scrollIntoViewIfNeeded();
  await page.screenshot({ path: "test-results/capabilities-settings.png", fullPage: true });
  await page.reload();
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await expect(page.getByLabel("嵌入模型名称")).toHaveValue("controlled-embedding");
  await expect(page.getByLabel("API 密钥", { exact: true })).toHaveValue("");
  await expect(page.getByTestId("capability-text")).toContainText("未验证");
});
