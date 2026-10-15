export type Settings = {
  base_url: string;
  model: string;
  api_key_configured: boolean;
};
export type Conversation = { id: string; title: string; created_at: string };
export type MemoryAction = { id: string; fact: string; status: "pending" | "saving" | "complete" | "failed"; memory_id: string | null; message_id: string; conversation_id: string; error: string | null; created_at: string };
export type MemoryUpdate = { id: string; action_id: string; fact: string; status: "pending" | "saving" | "complete" | "failed"; error: string | null; target_version: number; source_kind: "management" | "chat"; source_message_id: string | null; source_conversation_id: string | null; conversation_title?: string | null };
export type Attachment = { id: string; filename: string; size: number; image_metadata: { format: string; mime: string; width: number; height: number } | null; status: "processing" | "ready" | "failed"; error: string | null };
export type FileCitation = { key: string; attachment_id: string; filename: string; location: string; excerpt: string };
export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: "generating" | "paused" | "complete" | "stopped" | "failed";
  memory_action: MemoryAction | null;
  memory_update: MemoryUpdate | null;
  memory_recall: { status: "pending" | "complete" | "failed" | "skipped" | "unavailable"; count: number; detail: string } | null;
  budget_pause: { detail: string; operation_id: string; pause_id: string } | null;
  attachment_ids: string[];
  citations: FileCitation[];
  reply_to: string | null;
  error: string | null;
  created_at: string;
};
export type ConversationDetail = Conversation & { messages: Message[] };
export type BudgetDecision = { detail: string; operation_id: string; pause_id: string; resolve: (retry: boolean) => void };

export type Usage = {
  id: string;
  model: string;
  purpose: string;
  estimated_cost: string | null;
  status: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  created_at: string;
};

// 访问同源 JSON 或文件上传接口；预算暂停恢复复用原操作标识和请求正文。
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (!(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  if (options.method === "POST" && !headers.has("X-Operation-ID")) headers.set("X-Operation-ID", crypto.randomUUID());
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers,
    });
  } catch {
    throw new Error("无法连接本机应用，请确认应用已经启动。");
  }
  const body = await response.json().catch(
    // 非 JSON 错误使用通用提示，不把原始服务正文放入页面。
    () => ({ detail: "服务暂时不可用，请稍后重试。" }),
  );
  if (response.status === 402 && body.code === "budget_paused") {
    const retry = await new Promise<boolean>(
      // 将当前请求的持久化暂停挑战交给用户，用户决定前不发送后续请求。
      (resolve) => window.dispatchEvent(new CustomEvent<BudgetDecision>("budget-paused", { detail: { ...body, resolve } })),
    );
    if (!retry) throw new Error("本次操作已取消。");
    return api<T>(path, { ...options, headers });
  }
  if (!response.ok)
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : "请求未完成，请检查输入。",
    );
  return body as T;
}

// 将未知异常转为可展示的应用错误，避免页面因错误对象类型不同而崩溃。
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "操作未完成，请重试。";
}
