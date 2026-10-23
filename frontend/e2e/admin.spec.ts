import { loginAdmin } from "./auth";
import { expect, test } from "@playwright/test";

// 每个用例先通过 API 登录管理员。
test.beforeEach(async ({ page }) => {
  await loginAdmin(page);
});

// 验证管理员后台的账号创建、停用和临时密码重置。
test("管理员可以在后台创建、停用和重置账号", async ({ page }) => {
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "账号管理" })).toBeVisible();

  await page.getByRole("button", { name: "创建账号", exact: true }).click();

  await page.getByLabel("用户名").fill("ui_user");
  await page.getByLabel("临时密码").fill("ui-temp-pass");
  await page.getByRole("button", { name: "创建用户" }).click();
  await expect(page.getByText("ui_user", { exact: true })).toBeVisible();

  const row = page.locator(".admin-user").filter({ hasText: "ui_user" });
  await row.getByRole("button", { name: "查看用户数据" }).click();
  await expect(page.getByRole("heading", { name: "ui_user 的数据" })).toBeVisible();

  await row.locator(".admin-user-menu summary").click();
  page.once("dialog", async (dialog) => {
    expect(dialog.type()).toBe("confirm");
    await dialog.accept();
  });
  await row.getByRole("button", { name: "停用账号" }).click();
  await expect(row.getByText("已停用")).toBeVisible();

  await row.getByRole("button", { name: "设置临时密码" }).click();
  await expect(page.getByRole("dialog")).toContainText("为 ui_user 设置临时密码");
  await page.getByRole("dialog").getByRole("textbox", { name: "临时密码" }).fill("ui-reset-pass");
  await page.getByRole("button", { name: "保存临时密码" }).click();
  await expect(page.getByRole("status")).toContainText("临时密码已设置");
});
