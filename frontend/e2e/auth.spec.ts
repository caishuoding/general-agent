import { expect, test } from "@playwright/test";

import { loginAdmin } from "./auth";

test("未登录时只显示登录页，错误密码显示统一提示", async ({ page, context }) => {
  await context.clearCookies();
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
  await page.getByLabel("用户名").fill("admin");
  await page.getByLabel("密码").fill("wrong-password");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByText("用户名或密码错误。")).toBeVisible();
});

test("管理员可以登录并退出", async ({ page }) => {
  await loginAdmin(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "个人对话空间" })).toBeVisible();
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page.getByRole("heading", { name: "登录" })).toBeVisible();
});

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
  await page.request.post("/api/auth/logout");
  await page.goto("/");
  await page.getByLabel("用户名").fill("browser_user");
  await page.getByLabel("密码").fill("browser-temp-pass");
  await page.getByRole("button", { name: "登录" }).click();
  await expect(page.getByRole("heading", { name: "设置新密码" })).toBeVisible();
  await page.getByLabel("当前密码").fill("browser-temp-pass");
  await page.getByLabel("新密码", { exact: true }).fill("browser-user-password");
  await page.getByLabel("再次输入新密码").fill("browser-user-password");
  await page.getByRole("button", { name: "保存新密码" }).click();
  await expect(page.getByRole("heading", { name: "个人对话空间" })).toBeVisible();
  await expect(page.getByRole("button", { name: "设置", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "管理员后台", exact: true })).toHaveCount(0);
});
