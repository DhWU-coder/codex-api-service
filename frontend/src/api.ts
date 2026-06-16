import type { AdminConfig, ParsedChatStreamEvent, RequestLogItem } from "./types";

// 构造本地服务 Bearer 鉴权头；空 key 不发送 Authorization。
export function buildAuthHeaders(apiKey: string): Record<string, string> {
  const trimmed = apiKey.trim();
  return trimmed ? { Authorization: `Bearer ${trimmed}` } : {};
}

// 解析 OpenAI Chat Completions SSE 的单行 data。
export function parseChatStreamLine(line: string): ParsedChatStreamEvent {
  const trimmed = line.trim();
  if (!trimmed.startsWith("data:")) {
    return { type: "ignore" };
  }

  const payload = trimmed.slice("data:".length).trim();
  if (payload === "[DONE]") {
    return { type: "done" };
  }

  try {
    const parsed = JSON.parse(payload) as {
      choices?: Array<{ delta?: { content?: string } }>;
      usage?: { prompt_tokens: number; completion_tokens: number; total_tokens: number } | null;
      error?: { message?: string; status_code?: number };
    };

    // 后端会把上游流式异常转换成 error 事件，避免浏览器显示 network error。
    if (parsed.error && typeof parsed.error.message === "string") {
      return { type: "error", message: parsed.error.message, statusCode: parsed.error.status_code };
    }

    // usage chunk 的 choices 为空，OpenAI SDK 也按这个约定处理。
    if (Array.isArray(parsed.choices) && parsed.choices.length === 0 && parsed.usage) {
      return { type: "usage", usage: parsed.usage };
    }

    // 普通 delta chunk 从 choices[0].delta.content 中取文本。
    const text = parsed.choices?.[0]?.delta?.content;
    if (typeof text === "string") {
      return { type: "delta", text };
    }
  } catch {
    // 非法 JSON 行直接忽略，避免一个坏 chunk 打断整个 UI。
    return { type: "ignore" };
  }

  return { type: "ignore" };
}

// 读取安全配置快照。
export async function fetchAdminConfig(apiKey: string): Promise<AdminConfig> {
  const response = await fetch("/admin/config", {
    headers: buildAuthHeaders(apiKey)
  });
  if (!response.ok) {
    throw new Error(`配置读取失败：${response.status}`);
  }
  return (await response.json()) as AdminConfig;
}

// 保存控制台支持的配置字段。
export async function saveAdminConfig(apiKey: string, patch: unknown): Promise<{ restart_required: boolean }> {
  const response = await fetch("/admin/config", {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      ...buildAuthHeaders(apiKey)
    },
    body: JSON.stringify(patch)
  });
  if (!response.ok) {
    throw new Error(`配置保存失败：${response.status}`);
  }
  return (await response.json()) as { restart_required: boolean };
}

// 读取最近 API 请求日志；limit 用于看板拉取更多统计样本。
export async function fetchRequestLogs(apiKey: string, limit = 100): Promise<RequestLogItem[]> {
  const response = await fetch(`/admin/requests?limit=${limit}`, {
    headers: buildAuthHeaders(apiKey)
  });
  if (!response.ok) {
    throw new Error(`请求日志读取失败：${response.status}`);
  }
  const body = (await response.json()) as { items: RequestLogItem[] };
  return body.items;
}
