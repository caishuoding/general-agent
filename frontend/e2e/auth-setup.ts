import { test } from "@playwright/test";

import { loginAdmin, loginAdminApi } from "./auth";

test.beforeEach(async ({ page, request }) => {
  await loginAdmin(page);
  await loginAdminApi(request);
});
