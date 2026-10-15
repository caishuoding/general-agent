export type Settings = {
  base_url: string;
  model: string;
  api_key_configured: boolean;
};
export type Conversation = { id: string; title: string; created_at: string };
export type Message = {
  id: string;
  role: "user" | "assistant";
  content: string;
  status: "complete" | "failed";
  created_at: string;
};
export type ConversationDetail = Conversation & { messages: Message[] };
export type Usage = {
  id: string;
  model: string;
  status: string;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  created_at: string;
};

// 访问同源应用接口，仅把后端提供的公开错误说明传递给界面。
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      ...options,
      headers: { "Content-Type": "application/json", ...options.headers },
    });
  } catch {
    throw new Error("无法连接本机应用，请确认应用已经启动。");
  }
  const body = await response.json().catch(
    // 非 JSON 错误使用通用提示，不把原始服务正文放入页面。
    () => ({ detail: "服务暂时不可用，请稍后重试。" }),
  );
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
