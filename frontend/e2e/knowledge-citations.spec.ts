import { expect, test } from "@playwright/test";

test("知识库引用可定位来源并显示原图", /** 验证用户可见的引用锚点和图片来源卡片。 */ async function citationView({ page }) {
  const image = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nXQAAAAASUVORK5CYII=",
    "base64",
  );
  /** 返回包含一条已核验知识库图片引用的受控会话。 */
  function conversation() {
    return {
      id: "cited",
      title: "引用测试",
      messages: [{
        id: "answer-citation",
        role: "assistant",
        content: "季度增长来自图表。[K12]",
        status: "complete",
        attachments: [],
        web_sources: [],
        knowledge_sources: [{
          key: "K12",
          knowledge_base_id: "base-1",
          knowledge_base_name: "产品资料",
          document_id: "document-1",
          filename: "quarter.pdf",
          chunk_id: 3,
          location: "第 2 页第 1 张图片",
          excerpt: "季度增长图。",
          image_id: "image-1",
        }, {
          key: "K13",
          knowledge_base_id: "base-1",
          knowledge_base_name: "产品资料",
          document_id: "document-1",
          filename: "quarter.pdf",
          chunk_id: 4,
          location: "表格 8，第 1 行",
          excerpt: "| 指标 | 数值 |\n| --- | --- |\n| 季度增长 | 18% |",
          image_id: null,
        }],
        created_at: "2026-10-23T10:00:00Z",
      }],
    };
  }
  await page.route("**/api/**", /** 按路径返回隔离数据与内联原图。 */ async function routeApi(route) {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/images/image-1/file")) {
      await route.fulfill({ status: 200, contentType: "image/png", body: image });
      return;
    }
    let body: unknown = [];
    if (path === "/api/auth/me") body = { id: "citation", username: "citation", role: "user", must_change_password: false };
    if (path === "/api/conversations") body = [{ id: "cited", title: "引用测试" }];
    if (path === "/api/conversations/cited") body = conversation();
    await route.fulfill({ json: body });
  });

  await page.goto("/");
  const citation = page.getByRole("link", { name: "[quarter.pdf · 第 2 页第 1 张图片]", exact: true });
  await expect(citation).toBeVisible();
  await citation.click();
  const imageSource = page.locator(".knowledge-citation").filter({ hasText: "第 2 页第 1 张图片" });
  await expect(imageSource).toBeVisible();
  await expect(imageSource.getByText("产品资料", { exact: true })).toBeVisible();
  await expect(imageSource.getByText("季度增长图。", { exact: true })).toHaveCount(0);
  await expect(imageSource.getByRole("img", { name: "quarter.pdf · 第 2 页第 1 张图片" })).toBeVisible();
  const tableSource = page.locator(".knowledge-citation").filter({ hasText: "表格 8，第 1 行" });
  await expect(tableSource.locator("table")).toBeVisible();
  await expect(tableSource.getByRole("cell", { name: "季度增长" })).toBeVisible();
  await expect(tableSource.getByRole("cell", { name: "18%" })).toBeVisible();
});
