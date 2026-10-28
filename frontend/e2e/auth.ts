import type { Page } from "@playwright/test";

export const TEST_ADMIN_PASSWORD = "browser-admin-password";
export const TEST_USER_NAME = "browser_chat_user";
export const TEST_USER_PASSWORD = "browser-chat-password";
const TEST_USER_TEMP_PASSWORD = "browser-chat-temp";

// 通过 API 登录管理员并在首次运行时完成强制改密。
export async function loginAdmin(page: Page) {
  let response = await page.request.post("/api/admin/auth/login", {
    data: { username: "admin", password: "123456", expected_role: "admin" },
  });
  if (!response.ok()) {
    response = await page.request.post("/api/admin/auth/login", {
      data: { username: "admin", password: TEST_ADMIN_PASSWORD, expected_role: "admin" },
    });
  }
  if (!response.ok()) throw new Error("浏览器测试管理员登录失败");
  const user = await response.json();
  if (user.must_change_password) {
    const changed = await page.request.post("/api/admin/auth/password", {
      data: {
        current_password: "123456",
        new_password: TEST_ADMIN_PASSWORD,
      },
    });
    if (!changed.ok()) throw new Error("浏览器测试管理员首次改密失败");
  }
}

// 确保普通用户存在并以用户端身份登录，供聊天、附件和记忆测试使用。
export async function loginUser(page: Page) {
  let response = await page.request.post("/api/auth/login", {
    data: {
      username: TEST_USER_NAME,
      password: TEST_USER_PASSWORD,
      expected_role: "user",
    },
  });
  if (response.ok()) return;

  await loginAdmin(page);
  const created = await page.request.post("/api/admin/users", {
    data: {
      username: TEST_USER_NAME,
      password: TEST_USER_TEMP_PASSWORD,
      role: "user",
    },
  });
  if (![201, 422].includes(created.status())) {
    throw new Error("浏览器测试普通用户创建失败");
  }
  await page.request.post("/api/admin/auth/logout");
  response = await page.request.post("/api/auth/login", {
    data: {
      username: TEST_USER_NAME,
      password: TEST_USER_TEMP_PASSWORD,
      expected_role: "user",
    },
  });
  if (!response.ok()) throw new Error("浏览器测试普通用户登录失败");
  const user = await response.json();
  if (user.must_change_password) {
    const changed = await page.request.post("/api/auth/password", {
      data: {
        current_password: TEST_USER_TEMP_PASSWORD,
        new_password: TEST_USER_PASSWORD,
      },
    });
    if (!changed.ok()) throw new Error("浏览器测试普通用户首次改密失败");
  }
}

// 用管理员入口保存全局聊天模型配置，再切回普通用户会话。
export async function configureChatModel(
  page: Page,
  modelUrl: string,
  model: string,
) {
  await loginAdmin(page);
  const saved = await page.request.put("/api/settings", {
    data: {
      base_url: modelUrl,
      model,
      api_key: "browser-test-key",
    },
  });
  if (!saved.ok()) throw new Error("浏览器测试模型配置失败");
  await page.request.post("/api/admin/auth/logout");
  await loginUser(page);
}

// 用管理员入口保存全局搜索配置，再切回普通用户会话。
export async function configureSearchModel(
  page: Page,
  modelUrl: string,
  searchUrl: string,
) {
  await loginAdmin(page);
  const modelSaved = await page.request.put("/api/settings", {
    data: {
      base_url: modelUrl,
      model: "controlled-search-chat",
      api_key: "browser-test-key",
    },
  });
  const searchSaved = await page.request.put("/api/settings/search", {
    data: { base_url: searchUrl, api_key: "browser-search-key" },
  });
  if (!modelSaved.ok() || !searchSaved.ok()) {
    throw new Error("浏览器测试搜索配置失败");
  }
  await page.request.post("/api/admin/auth/logout");
  await loginUser(page);
}
