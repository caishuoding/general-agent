import { expect, test } from "@playwright/test";

test("知识库媒体可嵌入正文并保留来源链接", /** 验证图片和表格的行内渲染及底部来源定位。 */ async function citationView({ page }) {
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
        content: "## 一、增长\n\n**小标题**\n正文与标题之间只有一个换行。\n\n季度增长来自图表。[K12]\n\n[[MEDIA:K12]]\n\n## 二、指标\n\n同期指标如下。[K13]\n\n[[MEDIA:K13]]\n\n普通说明用于检查来源摘要。[K14]",
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
          page: 2,
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
          page: 2,
          excerpt: "| 指标 | 数值 |\n| --- | --- |\n| 季度增长 | 18% |",
          image_id: null,
          kind: "table",
          table: {
            table_id: 8,
            title: "季度指标",
            columns: 3,
            rows: 2,
            header_rows: 1,
            cells: [
              { row: 0, column: 0, text: "指标", rowspan: 1, colspan: 1, header: true },
              { row: 0, column: 1, text: "数值", rowspan: 1, colspan: 2, header: true },
              { row: 1, column: 0, text: "季度增长", rowspan: 1, colspan: 1, header: false },
              { row: 1, column: 1, text: "18%", rowspan: 1, colspan: 1, header: false },
              { row: 1, column: 2, text: "上升", rowspan: 1, colspan: 1, header: false },
            ],
            source_rows: [1],
            continued: false,
            render_as_image: false,
            image_id: null,
          },
        }, {
          key: "K14",
          knowledge_base_id: "base-1",
          knowledge_base_name: "产品资料",
          document_id: "document-1",
          filename: "quarter.pdf",
          chunk_id: 5,
          location: "正文段落 42",
          page: 3,
          excerpt: "这一段是很长的普通文字切片内容，用来确认参考来源摘要只会显示一行，超过可用宽度后自动使用省略号而不是继续换行。",
          image_id: null,
          kind: "text",
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
  await expect(page.getByRole("heading", { name: "一、增长" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "二、指标" })).toBeVisible();
  await expect(page.locator(".answer-markdown p").filter({ hasText: "小标题" }).locator("br")).toHaveCount(1);
  const citations = page.locator(".answer-citation");
  await expect(citations).toHaveText(["[1]", "[2]", "[3]"]);
  await expect(page.locator(".answer-sources")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "参考来源 · 3", exact: true })).toBeVisible();
  const citationStyle = await citations.first().evaluate(function inspectCitation(element) {
    const style = getComputedStyle(element);
    return { verticalAlign: style.verticalAlign, fontSize: parseFloat(style.fontSize) };
  });
  expect(citationStyle.verticalAlign).toBe("super");
  expect(citationStyle.fontSize).toBeLessThan(13);
  await citations.first().click();
  await expect(page.locator(".answer-sources")).toBeVisible();
  const imageMedia = page.locator('.inline-knowledge-media[data-media="K12"]');
  await expect(imageMedia.getByRole("img", { name: "quarter.pdf · 第 2 页第 1 张图片" })).toBeVisible();
  await expect(imageMedia.getByText("季度增长图。", { exact: true })).toHaveCount(0);
  await expect(imageMedia.locator("figcaption")).toHaveCount(0);
  const imageStyle = await imageMedia.evaluate(function inspectImageMedia(element) {
    const style = getComputedStyle(element);
    return { border: style.borderTopWidth, background: style.backgroundColor, padding: style.paddingTop };
  });
  expect(imageStyle).toEqual({ border: "0px", background: "rgba(0, 0, 0, 0)", padding: "0px" });
  const tableMedia = page.locator('.inline-knowledge-media[data-media="K13"]');
  await expect(tableMedia.locator("table")).toBeVisible();
  await expect(tableMedia.locator(".structured-source-table")).toBeVisible();
  await expect(tableMedia.getByRole("columnheader", { name: "指标" })).toBeVisible();
  await expect(tableMedia.getByRole("columnheader", { name: "数值" })).toHaveAttribute("colspan", "2");
  await expect(tableMedia.getByRole("cell", { name: "季度增长" })).toBeVisible();
  await expect(tableMedia.getByRole("cell", { name: "18%" })).toBeVisible();
  await expect(tableMedia.getByRole("cell", { name: "上升" })).toBeVisible();
  await expect(tableMedia.getByRole("caption")).toHaveText("季度指标");
  await expect(page.locator(".knowledge-source-link")).toHaveCount(3);
  await expect(page.locator(".knowledge-citation")).toHaveCount(0);
  await expect(page.locator(".knowledge-source-link blockquote, .knowledge-source-link table, .knowledge-source-link img")).toHaveCount(0);
  await expect(page.locator('.knowledge-source-link a').first()).toHaveAttribute("href", /#page=2$/);
  const excerpt = page.locator(".knowledge-source-excerpt");
  await expect(excerpt).toHaveCount(1);
  const excerptStyle = await excerpt.evaluate(function inspectExcerpt(element) {
    const style = getComputedStyle(element);
    return { overflow: style.overflow, textOverflow: style.textOverflow, whiteSpace: style.whiteSpace };
  });
  expect(excerptStyle).toEqual({ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" });
});

test("复杂表格使用受保护区域截图兜底", /** 验证结构化表格不可靠时只展示原区域图片。 */ async function tablePreview({ page }) {
  const image = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nXQAAAAASUVORK5CYII=",
    "base64",
  );
  await page.route("**/api/**", /** 返回带复杂表格截图的已核验回答。 */ async function routeApi(route) {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/images/table-image/file")) {
      await route.fulfill({ status: 200, contentType: "image/png", body: image });
      return;
    }
    let body: unknown = [];
    if (path === "/api/auth/me") body = { id: "citation", username: "citation", role: "user", must_change_password: false };
    if (path === "/api/conversations") body = [{ id: "table-preview", title: "表格兜底" }];
    if (path === "/api/conversations/table-preview") body = {
      id: "table-preview", title: "表格兜底", messages: [{
        id: "answer-table-preview", role: "assistant", content: "复杂表格如下。[K20]\n\n[[MEDIA:K20]]",
        status: "complete", attachments: [], web_sources: [], created_at: "2026-10-23T10:00:00Z",
        knowledge_sources: [{
          key: "K20", knowledge_base_id: "base-1", knowledge_base_name: "产品资料",
          document_id: "document-1", filename: "complex.pdf", chunk_id: 9, location: "第 3 页表格 2 原区域截图",
          page: 3, excerpt: "无法可靠还原的复杂表格", image_id: "table-image", kind: "table",
          table: { table_id: 2, title: null, columns: 4, rows: 2, header_rows: 0, cells: [],
            source_rows: [1, 2], continued: false, render_as_image: true, image_id: "table-image" },
        }],
      }],
    };
    await route.fulfill({ json: body });
  });
  await page.goto("/");
  const media = page.locator('.inline-knowledge-media[data-media="K20"]');
  await expect(media.getByRole("img", { name: "complex.pdf · 第 3 页表格 2 原区域截图" })).toBeVisible();
  await expect(media.locator("table")).toHaveCount(0);
  await expect(media.locator("figcaption")).toHaveCount(0);
});
