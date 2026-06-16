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
