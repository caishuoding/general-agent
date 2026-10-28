import { loginAdmin } from "./auth";
import { expect, test, type Page, type Route } from "@playwright/test";

/** 每个浏览器用例先通过独立入口登录唯一管理员，输入页面并建立管理端会话。 */
async function logInBeforeTest({ page }: { page: Page }) { await loginAdmin(page); }
test.beforeEach(logInBeforeTest);

/** 从用户界面验证账号创建、会话入口和临时密码只显示一次。 */
test("管理端只创建普通用户并展示一次临时密码", async ({ page }) => {
  await page.goto("/admin");
  await expect(page.getByRole("heading", { name: "用户账号" })).toBeVisible();
  await expect(page.getByText("仅查看，不会替用户发送消息")).toHaveCount(0);
  await page.getByRole("button", { name: "创建用户" }).click();
  await page.getByLabel("用户名", { exact: true }).fill("ui_user");
  await page.getByLabel("临时密码").fill("ui-temp-pass");
  await page.getByRole("button", { name: "创建账号" }).click();
  const card = page.locator(".admin-account-card").filter({ hasText: "ui_user" });
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: "查看数据" }).click();
  await expect(page.getByRole("heading", { name: "ui_user" })).toBeVisible();
  await page.getByRole("button", { name: "返回账号列表" }).click();
  await card.getByRole("button", { name: "重置密码" }).click();
  await page.getByRole("button", { name: "生成临时密码" }).click();
  await expect(page.locator(".admin-temporary-password")).not.toBeEmpty();
  await page.getByRole("button", { name: "完成" }).click();
  await expect(page.locator(".admin-temporary-password")).toHaveCount(0);
});

/** 延迟第一个账号的会话响应，验证切换后不会在第二个账号标题下展示旧账号内容。 */
test("快速切换账号时只显示当前账号的会话", async ({ page }) => {
  const first = await page.request.post("/api/admin/users", { data: { username: "race_first", password: "race-first-password" } });
  const second = await page.request.post("/api/admin/users", { data: { username: "race_second", password: "race-second-password" } });
  const firstId = (await first.json()).id as string;
  const secondId = (await second.json()).id as string;
  /** 按账号标识返回不同会话，并延迟第一个账号以触发切换竞态。 */
  async function handleConversationRoute(route: Route) {
    if (route.request().url().includes(firstId)) {
      await page.waitForTimeout(700);
      await route.fulfill({ json: [{ id: "11111111-1111-4111-8111-111111111111", title: "第一个账号的会话", created_at: "2026-10-28T00:00:00Z" }] });
      return;
    }
    if (route.request().url().includes(secondId)) {
      await route.fulfill({ json: [{ id: "22222222-2222-4222-8222-222222222222", title: "第二个账号的会话", created_at: "2026-10-28T00:00:00Z" }] });
      return;
    }
    await route.continue();
  }
  await page.route("**/api/admin/users/*/conversations*", handleConversationRoute);
  await page.goto("/admin");
  await page.locator(".admin-account-card").filter({ hasText: "race_first" }).getByRole("button", { name: "查看数据" }).click();
  await page.getByRole("button", { name: "返回账号列表" }).click();
  await page.locator(".admin-account-card").filter({ hasText: "race_second" }).getByRole("button", { name: "查看数据" }).click();
  await expect(page.getByRole("heading", { name: "race_second" })).toBeVisible();
  await expect(page.getByRole("button", { name: /第二个账号的会话/ })).toBeVisible();
  await page.waitForTimeout(800);
  await expect(page.getByText("第一个账号的会话")).toHaveCount(0);
});
