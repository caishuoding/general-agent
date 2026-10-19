import { loginAdmin } from "./auth";
import { expect, test } from "@playwright/test";

// 每个用例先通过 API 登录管理员。
test.beforeEach(async ({ page }) => {
  await loginAdmin(page);
});

// 验证管理员后台的账号创建、停用和临时密码重置。
test("管理员可以在后台创建、停用和重置账号", async ({ page }) => {
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "账号与只读内容" })).toBeVisible();

  await page.getByLabel("用户名").fill("ui_user");
  await page.getByLabel("临时密码").fill("ui-temp-pass");
  await page.getByRole("button", { name: "创建账号" }).click();
  await expect(page.getByText("ui_user", { exact: true })).toBeVisible();

  const row = page.locator(".admin-user").filter({ hasText: "ui_user" });
  await row.getByRole("button", { name: "查看内容" }).click();
  await expect(page.getByRole("heading", { name: "ui_user 的只读内容" })).toBeVisible();

  await row.getByRole("button", { name: "停用" }).click();
  await expect(row.getByText("已停用")).toBeVisible();

  page.once("dialog", async (dialog) => {
    expect(dialog.type()).toBe("prompt");
    await dialog.accept("ui-reset-pass");
  });
  await row.getByRole("button", { name: "重置密码" }).click();
  await expect(page.getByText("ui_user", { exact: true })).toBeVisible();
});
