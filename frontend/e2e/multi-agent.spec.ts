import { test, expect } from "@playwright/test";
import { configureChatModel, loginUser } from "./auth";

// 通过真实主从工具流验证只读子会话、多轮任务及刷新恢复。
test("主智能体汇总子任务并可查看只读会话", async ({ page }) => {
  await loginUser(page);
  await configureChatModel(page, `http://127.0.0.1:${process.env.TEST_MODEL_PORT || "18081"}/v1`, "controlled-model");
  await page.goto("/");
  await page.getByRole("button", { name: /新建会话|开始新对话/ }).click();
  await page.getByRole("textbox", { name: "消息", exact: true }).fill("__multi_agent_browser__");
  await page.getByRole("button", { name: "发送消息" }).click();
  await expect(page.getByText("主智能体已经汇总子任务。", { exact: true })).toBeVisible();
  const task = page.getByRole("button", { name: "研究子任务 · 已完成" });
  await expect(task).toBeVisible();
  await expect(page.getByText("子智能体补充交付结果。", { exact: true })).toBeVisible();
  const taskBox = await task.boundingBox();
  const answerBox = await page.getByText("主智能体已经汇总子任务。", { exact: true }).boundingBox();
  expect(taskBox).not.toBeNull();
  expect(answerBox).not.toBeNull();
  expect(taskBox!.y).toBeLessThan(answerBox!.y);
  await expect(page.getByText("子智能体公开交付结果。", { exact: true })).toHaveCount(0);
  await task.click();
  const dialog = page.getByRole("dialog", { name: "研究子任务" });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("第 1 轮", { exact: true })).toBeVisible();
  await expect(dialog.getByText("子智能体公开交付结果。", { exact: true })).toBeVisible();
  await expect(dialog.getByText("第 2 轮", { exact: true })).toBeVisible();
  await expect(dialog.getByText("子智能体补充交付结果。", { exact: true })).toBeVisible();
  await expect(dialog.getByText("此会话由主智能体管理，无法直接输入。", { exact: true })).toBeVisible();
  await expect(dialog.getByRole("textbox")).toHaveCount(0);
  await dialog.getByRole("button", { name: "关闭子任务会话" }).click();
  await page.reload();
  await expect(task).toBeVisible();
  await task.click();
  await expect(dialog.getByText("子智能体补充交付结果。", { exact: true })).toBeVisible();
});
