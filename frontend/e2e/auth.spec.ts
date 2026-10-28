import { expect, test } from "@playwright/test";

import { loginAdmin } from "./auth";

/** 验证未登录访问和统一登录错误提示，输入浏览器上下文并检查页面反馈。 */
test("未登录时只显示登录页，错误密码显示统一提示", async ({ page, context }) => {
  await context.clearCookies();
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
  await page.getByLabel("用户名").fill("admin");
  await page.getByLabel("密码").fill("wrong-password");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByText("用户名或密码错误。")).toBeVisible();
});

/** 验证管理员登录与主动退出，输入页面并检查入口状态。 */
test("管理员可以登录并退出", async ({ page }) => {
  await loginAdmin(page);
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "用户账号" })).toBeVisible();
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page.getByRole("heading", { name: "管理员登录" })).toBeVisible();
});

/** 验证管理员会话不会让用户端自动跳转到管理端，输入页面并检查两个入口。 */
test("管理端登录后访问用户端仍显示用户登录页", async ({ page }) => {
  await loginAdmin(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "登录", exact: true })).toBeVisible();
  await expect(page).toHaveURL(/\/$/);
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "用户账号" })).toBeVisible();
});

/** 验证新建账号首次登录必须先完成改密，输入页面并完成登录流程。 */
test("新账号首次登录显示强制改密页", async ({ page }) => {
  await loginAdmin(page);
  const created = await page.request.post("/api/admin/users", {
    data: {
      username: "browser_user",
      password: "browser-temp-pass",
      role: "user",
    },
  });
  expect(created.status()).toBe(201);
  await page.request.post("/api/admin/auth/logout");
  await page.goto("/");
  await page.getByLabel("用户名").fill("browser_user");
  await page.getByLabel("密码").fill("browser-temp-pass");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("heading", { name: "设置新密码" })).toBeVisible();
  await page.getByLabel("当前密码").fill("browser-temp-pass");
  await page.getByLabel("新密码", { exact: true }).fill("browser-user-password");
  await page.getByLabel("再次输入新密码").fill("browser-user-password");
  await page.getByRole("button", { name: "设置新密码" }).click();
  await expect(page.getByRole("heading", { name: "个人对话空间" })).toBeVisible();
  await expect(page.getByRole("button", { name: "设置", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "管理员后台", exact: true })).toHaveCount(0);
  await page.locator(".account-menu summary").click();
  await page.getByRole("button", { name: "修改密码" }).click();
  await expect(page.getByRole("heading", { name: "修改密码" })).toBeVisible();
  await page.getByLabel("当前密码").fill("browser-user-password");
  await page.getByLabel("新密码", { exact: true }).fill("browser-user-password-2");
  await page.getByLabel("再次输入新密码").fill("browser-user-password-2");
  await page.getByRole("button", { name: "更新密码" }).click();
  await expect(page.getByRole("heading", { name: "个人对话空间" })).toBeVisible();
});
