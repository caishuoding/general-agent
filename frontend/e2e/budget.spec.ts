import { test, expect } from "@playwright/test";

// 通过真实页面设置预算，并确认能力检查共用可明确批准的暂停提示。
test("预算暂停允许一次能力检查且展示未知费用",
// 验证一次批准、下一请求重新暂停及补价后的费用恢复。
async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "设置", exact: true }).click();
  await page.getByLabel("API 地址", { exact: true }).fill(`http://127.0.0.1:${process.env.TEST_MODEL_PORT || "18087"}/v1`);
  await page.getByLabel("模型名称", { exact: true }).fill("controlled-budget");
  await page.getByLabel("API 密钥", { exact: true }).fill("browser-test-key");
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await page.getByLabel("月度预算（人民币元）").fill("10");
  await page.getByRole("button", { name: "保存预算与单价" }).click();
  await page.getByRole("button", { name: "检查文字" }).click();
  await expect(page.getByRole("dialog", { name: "云端请求已暂停" })).toBeVisible();
  await expect(page.getByRole("dialog")).toContainText("缺少单价");
  await page.getByRole("button", { name: "仅允许下一次云端调用" }).click();
  await expect(page.getByTestId("capability-text")).toContainText("通过");
  await expect(page.getByTestId("monthly-summary")).toContainText("费用未知");
  await page.getByRole("button", { name: "检查图片" }).click();
  await expect(page.getByRole("dialog")).toContainText("本月存在费用未知");
  await page.getByRole("button", { name: "取消本次操作" }).click();
  await page.getByLabel("controlled-budget 输入单价").fill("1");
  await page.getByLabel("controlled-budget 输出单价").fill("1");
  await page.getByRole("button", { name: "保存预算与单价" }).click();
  await expect(page.getByTestId("monthly-summary")).toContainText("0.000005");
  await page.getByRole("button", { name: "检查图片" }).click();
  await expect(page.getByTestId("capability-image")).toContainText("通过");
  await page.getByTestId("monthly-summary").scrollIntoViewIfNeeded();
  await page.screenshot({ path: "test-results/budget-settings.png", fullPage: true });
});
