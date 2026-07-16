import type {
  AdminConfig,
  AdminHealth,
  AdminModelCatalog,
  AuthReloadResult,
  CodexUsageSnapshot,
  ParsedChatStreamEvent,
  RequestLogItem,
  OAuthAccountsSnapshot,
  OAuthLoginSession
} from "./types";
import type { DashboardRangePreset, DashboardSummary } from "./dashboard";

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
    throw new Error(`配置读取失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as AdminConfig;
}

// 保存控制台支持的配置字段。
export async function saveAdminConfig(
  apiKey: string,
  patch: unknown
): Promise<{ restart_required: boolean; applied?: boolean }> {
  const response = await fetch("/admin/config", {
    method: "PATCH",
    headers: {
      "Content-Type": "application/json",
      ...buildAuthHeaders(apiKey)
    },
    body: JSON.stringify(patch)
  });
  if (!response.ok) {
    throw new Error(`配置保存失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as { restart_required: boolean; applied?: boolean };
}

// 读取管理台运行健康状态。
export async function fetchAdminHealth(apiKey: string): Promise<AdminHealth> {
  const response = await fetch("/admin/health", {
    headers: buildAuthHeaders(apiKey)
  });
  if (!response.ok) {
    throw new Error(`状态读取失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as AdminHealth;
}

// 读取 Codex CLI 动态模型目录；force=true 时让后端强制刷新 CLI。
export async function fetchAdminModels(apiKey: string, force = false): Promise<AdminModelCatalog> {
  const path = force ? "/admin/models?refresh=true" : "/admin/models";
  const response = await fetch(path, {
    headers: buildAuthHeaders(apiKey)
  });
  if (!response.ok) {
    throw new Error(`模型目录读取失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as AdminModelCatalog;
}

// 从 Codex CLI/App 登录文件重新导入 OAuth 凭据。
export async function reloadCodexAuth(apiKey: string): Promise<AuthReloadResult> {
  const response = await fetch("/admin/auth/reload", {
    method: "POST",
    headers: buildAuthHeaders(apiKey)
  });
  if (!response.ok) {
    throw new Error(`OAuth 同步失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as AuthReloadResult;
}

// 读取当前 Codex OAuth 账号的额度状态；OAuth token 只在后端使用。
export async function fetchCodexUsage(apiKey: string): Promise<CodexUsageSnapshot> {
  const response = await fetch("/admin/codex/usage", {
    headers: buildAuthHeaders(apiKey)
  });
  if (!response.ok) {
    throw new Error(`额度状态读取失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as CodexUsageSnapshot;
}

// 读取项目内全部 OAuth 账号及其脱敏调度状态。
export async function fetchOAuthAccounts(apiKey: string): Promise<OAuthAccountsSnapshot> {
  const response = await fetch("/admin/oauth/accounts", { headers: buildAuthHeaders(apiKey) });
  if (!response.ok) {
    throw new Error(`OAuth 账号读取失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as OAuthAccountsSnapshot;
}

// 强制刷新全部启用账号的额度，并返回可直接替换页面状态的完整快照。
export async function refreshOAuthAccounts(apiKey: string): Promise<OAuthAccountsSnapshot> {
  const response = await fetch("/admin/oauth/accounts/refresh", {
    method: "POST",
    headers: buildAuthHeaders(apiKey)
  });
  if (!response.ok) {
    throw new Error(`额度刷新失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as OAuthAccountsSnapshot;
}

// 把当前 Codex CLI/App 登录同步到真实账号对应的项目目录。
export async function syncOAuthAccounts(apiKey: string): Promise<OAuthAccountsSnapshot> {
  const response = await fetch("/admin/oauth/accounts/sync", {
    method: "POST",
    headers: buildAuthHeaders(apiKey)
  });
  if (!response.ok) {
    throw new Error(`OAuth 同步失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as OAuthAccountsSnapshot;
}

export async function saveOAuthDispatch(
  apiKey: string,
  policy: { mode: "single"; singleAccountKey: string } | { mode: "multi"; enabledAccountKeys: string[] }
): Promise<OAuthAccountsSnapshot> {
  const response = await fetch("/admin/oauth/dispatch", {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...buildAuthHeaders(apiKey) },
    body: JSON.stringify(policy)
  });
  if (!response.ok) {
    throw new Error(`账户调度策略保存失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as OAuthAccountsSnapshot;
}

export async function updateOAuthAccount(
  apiKey: string,
  accountKey: string,
  patch: unknown
): Promise<OAuthAccountsSnapshot> {
  const response = await fetch(`/admin/oauth/accounts/${encodeURIComponent(accountKey)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json", ...buildAuthHeaders(apiKey) },
    body: JSON.stringify(patch)
  });
  if (!response.ok) {
    throw new Error(`OAuth 账号保存失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as OAuthAccountsSnapshot;
}

export async function deleteOAuthAccount(apiKey: string, accountKey: string): Promise<OAuthAccountsSnapshot> {
  const response = await fetch(`/admin/oauth/accounts/${encodeURIComponent(accountKey)}`, {
    method: "DELETE",
    headers: buildAuthHeaders(apiKey)
  });
  if (!response.ok) {
    throw new Error(`OAuth 账号删除失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as OAuthAccountsSnapshot;
}

export async function startOAuthLogin(apiKey: string, deviceAuth = false): Promise<OAuthLoginSession> {
  const response = await fetch("/admin/oauth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...buildAuthHeaders(apiKey) },
    body: JSON.stringify({ deviceAuth })
  });
  if (!response.ok) {
    throw new Error(`OAuth 登录启动失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as OAuthLoginSession;
}

export async function fetchOAuthLoginStatus(apiKey: string, sessionId: string): Promise<OAuthLoginSession> {
  const response = await fetch(`/admin/oauth/login/${encodeURIComponent(sessionId)}`, {
    headers: buildAuthHeaders(apiKey)
  });
  if (!response.ok) {
    throw new Error(`OAuth 登录状态读取失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as OAuthLoginSession;
}

// 读取 API 请求日志；limit=all 时用于本地看板全量统计。
export async function fetchRequestLogs(apiKey: string, limit: number | "all" = 100): Promise<RequestLogItem[]> {
  const response = await fetch(`/admin/requests?limit=${encodeURIComponent(String(limit))}`, {
    headers: buildAuthHeaders(apiKey)
  });
  if (!response.ok) {
    throw new Error(`请求日志读取失败：${await responseErrorMessage(response)}`);
  }
  const body = (await response.json()) as { items: RequestLogItem[] };
  return body.items;
}

// 读取后端基于完整持久化历史计算的看板摘要，避免浏览器下载全部日志。
export async function fetchDashboardSummary(
  apiKey: string,
  preset: DashboardRangePreset,
  recentDays: number
): Promise<DashboardSummary> {
  const params = new URLSearchParams({
    range: preset,
    recent_days: String(recentDays)
  });
  const response = await fetch(`/admin/dashboard?${params.toString()}`, {
    headers: buildAuthHeaders(apiKey)
  });
  if (!response.ok) {
    throw new Error(`看板读取失败：${await responseErrorMessage(response)}`);
  }
  return (await response.json()) as DashboardSummary;
}

// 兼容 OpenAI error envelope 和普通 HTTP 状态文本，给 UI 展示更具体的错误。
async function responseErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.clone().json()) as { error?: { message?: string }; detail?: string };
    if (typeof body.error?.message === "string") {
      return body.error.message;
    }
    if (typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    // 响应不是 JSON 时退回到 HTTP 状态码。
  }
  return String(response.status);
}
