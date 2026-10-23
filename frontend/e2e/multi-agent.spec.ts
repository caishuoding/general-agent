import { test, expect } from "@playwright/test";
import { configureChatModel, loginUser } from "./auth";

// 通过真实主从工具流验证折叠任务、授权详情及刷新恢复。
test("主智能体汇总子任务并可展开交付详情", async ({ page }) => {
  await loginUser(page);
  await configureChatModel(page, `http://127.0.0.1:${process.env.TEST_MODEL_PORT || "18081"}/v1`, "controlled-model");
  await page.goto("/");
  await page.getByRole("button", { name: /新建会话|开始新对话/ }).click();
  await page.getByRole("textbox", { name: "消息", exact: true }).fill("__multi_agent_browser__");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByText("主智能体已经汇总子任务。", { exact: true })).toBeVisible();
  const task = page.getByText("研究子任务 · 已完成", { exact: true });
  await expect(task).toBeVisible();
  const taskBox = await task.boundingBox();
  const answerBox = await page.getByText("主智能体已经汇总子任务。", { exact: true }).boundingBox();
  expect(taskBox).not.toBeNull();
  expect(answerBox).not.toBeNull();
  expect(taskBox!.y).toBeLessThan(answerBox!.y);
  await expect(page.getByText("子智能体公开交付结果。", { exact: true })).toHaveCount(0);
  await task.click();
  await expect(page.getByText("子智能体公开交付结果。", { exact: true })).toBeVisible();
  await page.reload();
  await expect(task).toBeVisible();
  await task.click();
  await expect(page.getByText("子智能体公开交付结果。", { exact: true })).toBeVisible();
});
