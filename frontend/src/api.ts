export type Settings = {
  base_url: string;
  model: string;
  api_key_configured: boolean;
  context_window_tokens?: number;
  output_reserve_tokens?: number;
};
export type AuthUser = {
  id: string;
  username: string;
  role: "admin" | "user";
  must_change_password: boolean;
};
export type Conversation = { id: string; title: string; created_at: string };
export type DocumentParseInfo = { format?: string; total_units?: number; readable_units?: number; coverage?: string; missing_units?: string[]; limitations?: string[] };
export type Attachment = { id: string; filename: string; size: number; status: "processing" | "ready" | "failed" | "deleting"; error: string | null; parse_info?: DocumentParseInfo; image_metadata: { format: string; mime: string; width: number; height: number } | null };
export type MessageAttachment = Pick<Attachment, "id" | "filename" | "size" | "status" | "error">;
export type AgentTask = { agent_id: string; run_id: string; title: string; status: string; result: string | null; error: string | null };
export type MessageProgress = { code: string; label: string };
export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: "generating" | "complete" | "stopped" | "failed";
  attachments: MessageAttachment[];
  agent_tasks?: AgentTask[];
  progress?: MessageProgress | null;
  search_action: { id: string; query: string; status: string; sources: { id: string; label: string }[]; error: string | null } | null;
  web_sources: { key: string; title: string; url: string; excerpt: string }[];
  knowledge_sources: { key: string; knowledge_base_id: string; knowledge_base_name: string; document_id: string; filename: string; chunk_id: number; location: string; page?: number | null; end_page?: number | null; excerpt: string; image_id: string | null; kind?: "text" | "table" | "image"; table?: { table_id: number; title?: string | null; columns: number; rows: number; header_rows: number; cells: { row: number; column: number; text: string; rowspan?: number; colspan?: number; header?: boolean }[]; source_rows: number[]; continued: boolean; render_as_image: boolean; image_id?: string | null } | null }[];
  reply_to: string | null;
  error: string | null;
  created_at: string;
};
export type ConversationDetail = Conversation & { messages: Message[] };

export type Usage = {
  agent_id?: string | null;
  agent_run_id?: string | null;
  assistant_message_id?: string | null;
  id: string;
  model: string;
  purpose: string;
  unit: string;
  credits: string | null;
  status: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  created_at: string;
};

// 会话失效事件名：任意业务请求返回 401 时广播，让页面回到登录状态。
export const AUTH_EXPIRED_EVENT = "general-agent:auth-expired";

// 访问同源 JSON 或文件上传接口，并为写操作附加稳定操作标识。
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
  if (!response.ok) {
    // 登录接口会正常返回 401 表示密码错误，其余 401 视为会话已失效。
    if (response.status === 401 && path !== "/auth/login") {
      window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT));
    }
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : "请求未完成，请检查输入。",
    );
  }
  return body as T;
}

// 将未知异常转为可展示的应用错误，避免页面因错误对象类型不同而崩溃。
export function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "操作未完成，请重试。";
}
