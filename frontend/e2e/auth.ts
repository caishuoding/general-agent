import type { Page } from "@playwright/test";

export const TEST_ADMIN_PASSWORD = "browser-admin-password";

// 通过 API 登录管理员并在首次运行时完成强制改密。
export async function loginAdmin(page: Page) {
  let response = await page.request.post("/api/auth/login", {
    data: { username: "admin", password: "123456" },
  });
  if (!response.ok()) {
    response = await page.request.post("/api/auth/login", {
      data: { username: "admin", password: TEST_ADMIN_PASSWORD },
    });
  }
  if (!response.ok()) throw new Error("浏览器测试管理员登录失败");
  const user = await response.json();
  if (user.must_change_password) {
    const changed = await page.request.post("/api/auth/password", {
      data: {
        current_password: "123456",
        new_password: TEST_ADMIN_PASSWORD,
      },
    });
    if (!changed.ok()) throw new Error("浏览器测试管理员首次改密失败");
  }
}
