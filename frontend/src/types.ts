// 聊天消息在前端中的最小状态模型。
export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
};

// Chat Completions 流式 usage chunk 的字段。
export type ChatUsage = {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
};

// 前端解析 SSE 行后的统一事件。
export type ParsedChatStreamEvent =
  | { type: "delta"; text: string }
  | { type: "usage"; usage: ChatUsage }
  | { type: "error"; message: string; statusCode?: number }
  | { type: "done" }
  | { type: "ignore" };

// 管理接口返回的安全配置快照。
export type AdminConfig = {
  server: { host: string; port: number };
  api: { local_api_key_configured: boolean };
  codex: {
    default_model: string;
    available_models: string[];
    reasoning_effort: string;
    timeout_seconds: number;
    include_reasoning: boolean;
    fast_mode: boolean;
  };
  usage: { enabled: boolean; path: string };
  auth: { auth_path: string; import_auth_path: string };
  config_path: string;
};

// 管理接口返回的单个模型目录项，用于模型和 effort 下拉。
export type AdminModelCatalogEntry = {
  id: string;
  display_name: string;
  default_reasoning_effort: string;
  supported_reasoning_efforts: string[];
  source: "cli" | "compatibility" | "config";
};

// 管理接口返回的动态模型目录快照。
export type AdminModelCatalog = {
  models: AdminModelCatalogEntry[];
  effective_default_model: string;
  cache_state: "fresh" | "stale" | "fallback";
  source: string;
  warning?: string;
};

// 管理台 health 接口返回的运行状态。
export type AdminHealth = {
  server: { api: string; console: string };
  oauth: { available: boolean; expired?: boolean };
  usage: { enabled: boolean; writable: boolean; path: string };
  ui: { built: boolean };
  codex: { client_version: string; default_model?: string; fast_mode?: boolean };
};

// 重新导入 Codex 登录后的状态响应。
export type AuthReloadResult = {
  oauth: { available: boolean; expired?: boolean; reloaded: boolean };
  message?: string;
};

// 请求日志列表中的单条元数据记录。
export type RequestLogItem = {
  id: string;
  timestamp: string;
  method: string;
  path: string;
  model: string | null;
  status_code: number;
  duration_ms: number;
  usage: { total: number; input: number; cached: number; output: number; reasoning: number } | null;
  request_id: string | null;
  error: string | null;
};
