import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  Clock3,
  Copy,
  Database,
  Gauge,
  KeyRound,
  MessageSquare,
  Moon,
  RefreshCw,
  Save,
  Send,
  Settings,
  SlidersHorizontal,
  Sun,
  Square,
  Users,
  X,
  Zap
} from "lucide-react";
import { type ReactNode, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  buildAuthHeaders,
  fetchAdminHealth,
  fetchAdminConfig,
  fetchCodexUsage,
  fetchDashboardSummary,
  fetchAdminModels,
  fetchRequestLogs,
  fetchOAuthAccounts,
  saveOAuthDispatch,
  syncOAuthAccounts,
  updateOAuthAccount,
  deleteOAuthAccount,
  startOAuthLogin,
  fetchOAuthLoginStatus,
  parseChatStreamLine,
  refreshOAuthAccounts,
  reloadCodexAuth,
  saveAdminConfig
} from "./api";
import {
  formatCompactNumber,
  formatDuration,
  formatNumber,
  summarizeRequestLogs,
  type DashboardDistributionItem,
  type DashboardRangePreset,
  type DashboardSummary,
  type DashboardTimelineBucket
} from "./dashboard";
import {
  buildModelRequestRows,
  requestDefaultsForModel,
  serializeModelRequestOverrides
} from "./modelRequestConfig";
import { calculateVirtualLogWindow } from "./logVirtualization";
import type {
  AdminConfig,
  AdminHealth,
  AdminModelCatalog,
  AdminModelCatalogEntry,
  ChatMessage,
  ChatUsage,
  CodexUsageRateLimit,
  CodexUsageSnapshot,
  OAuthAccountsSnapshot,
  OAuthAccountSnapshot,
  OAuthLoginSession,
  ModelRequestConfigRow,
  RequestLogItem
} from "./types";

// 顶部导航标签的枚举，保持状态值简短稳定。
type ActiveTab = "dashboard" | "chat" | "logs" | "config" | "model-config" | "oauth-accounts" | "account-load" | "usage-status";

// 控制台主题只提供明确的浅色和深色两种渲染结果。
type ThemeMode = "light" | "dark";

// 主题选择保存在本地浏览器，刷新后保持用户偏好。
const THEME_STORAGE_KEY = "codex-console-theme";

// 没有目录元数据时保留旧四档，保证配置兜底仍可编辑。
const DEFAULT_REASONING_EFFORTS = ["low", "medium", "high", "xhigh"];

// API Service 用量看板支持的时间范围，顺序和页面分段控件一致。
const DASHBOARD_RANGE_OPTIONS: Array<{ value: DashboardRangePreset; label: string }> = [
  { value: "today", label: "今日" },
  { value: "week", label: "本周" },
  { value: "month", label: "本月" },
  { value: "all", label: "全部" },
  { value: "recent", label: "最近" }
];

// 配置表单只暴露第一版控制台支持安全编辑的字段。
type ConfigFormState = {
  localApiKey: string;
  localApiKeyTouched: boolean;
  defaultModel: string;
  usageEnabled: boolean;
  importAuthPath: string;
  globalMaxConcurrency: string;
  queueTimeoutSeconds: string;
};

type AccountEditorState = {
  mode: "rename" | "concurrency";
  account: OAuthAccountSnapshot;
  alias: string;
  concurrencyMode: "unlimited" | "limited";
  maxConcurrency: string;
  saving: boolean;
  error: string;
};

type DispatchEditorState = {
  mode: "single" | "multi";
  singleAccountKey: string;
  enabledAccountKeys: string[];
  saving: boolean;
  error: string;
};

function accountLoadScale(snapshot: OAuthAccountsSnapshot): number {
  // 统一刻度保证不同账号的负载条可以直接比较。
  return Math.max(
    5,
    snapshot.globalMaxConcurrency || 0,
    ...snapshot.accounts.map((account) => account.maxConcurrency || 0),
    ...snapshot.accounts.map((account) => account.currentConcurrency)
  );
}

// 生成前端本地消息 id，避免依赖服务端返回。
function newId(prefix: string): string {
  return `${prefix}_${Math.random().toString(16).slice(2)}_${Date.now()}`;
}

// 把配置快照转换成表单状态。
function formFromConfig(config: AdminConfig): ConfigFormState {
  return {
    localApiKey: "",
    localApiKeyTouched: false,
    defaultModel: config.codex.default_model,
    usageEnabled: config.usage.enabled,
    importAuthPath: config.auth.import_auth_path,
    globalMaxConcurrency: config.concurrency?.global_max == null ? "" : String(config.concurrency.global_max),
    queueTimeoutSeconds: String(config.concurrency?.queue_timeout_seconds ?? 600)
  };
}

// 为配置兜底模型构造最小目录项，避免目录接口失败时丢失当前模型。
function fallbackModelEntry(modelId: string, defaultEffort: string): AdminModelCatalogEntry {
  return {
    id: modelId,
    display_name: modelId,
    default_reasoning_effort: defaultEffort || "medium",
    supported_reasoning_efforts: DEFAULT_REASONING_EFFORTS,
    source: "config"
  };
}

// 按 id 去重合并模型项，目录项优先，当前值作为最后兜底。
function buildModelOptions(
  catalog: AdminModelCatalog | null,
  config: AdminConfig | null,
  selectedModels: string[],
  defaultEffort: string
): AdminModelCatalogEntry[] {
  const entries = catalog?.models.length
    ? catalog.models
    : (config?.codex.available_models || []).map((modelId) => fallbackModelEntry(modelId, defaultEffort));
  const merged = new Map<string, AdminModelCatalogEntry>();
  for (const entry of entries) {
    if (entry.id.trim()) {
      merged.set(entry.id, entry);
    }
  }
  for (const modelId of selectedModels) {
    if (modelId.trim() && !merged.has(modelId)) {
      merged.set(modelId, fallbackModelEntry(modelId, defaultEffort));
    }
  }
  return [...merged.values()];
}

function modelEntry(options: AdminModelCatalogEntry[], modelId: string): AdminModelCatalogEntry | undefined {
  return options.find((entry) => entry.id === modelId);
}

function reasoningOptionsFor(options: AdminModelCatalogEntry[], modelId: string): string[] {
  const supported = modelEntry(options, modelId)?.supported_reasoning_efforts.filter(Boolean);
  return supported?.length ? supported : DEFAULT_REASONING_EFFORTS;
}

function effortForModel(options: AdminModelCatalogEntry[], modelId: string, currentEffort: string): string {
  const supported = reasoningOptionsFor(options, modelId);
  if (supported.includes(currentEffort)) {
    return currentEffort;
  }
  return modelEntry(options, modelId)?.default_reasoning_effort || supported[0] || "medium";
}

// 判断 localStorage 里的主题值是否是当前版本支持的值。
function isThemeMode(value: string | null): value is ThemeMode {
  return value === "light" || value === "dark";
}

// 从系统偏好解析默认主题；没有 matchMedia 时使用浅色兜底。
function systemTheme(): ThemeMode {
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

// 启动时优先使用用户选择，否则跟随系统主题。
function initialTheme(): ThemeMode {
  const savedTheme = localStorage.getItem(THEME_STORAGE_KEY);
  return isThemeMode(savedTheme) ? savedTheme : systemTheme();
}

// 把主题写到根节点，CSS 变量会根据 data-theme 切换。
function applyTheme(theme: ThemeMode): void {
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

// 侧栏展示真实访问地址，反代或端口变化时比配置文件更可靠。
function currentDisplayHost(): string {
  return window.location.host || "127.0.0.1:1219";
}

// 把 health/config 的技术字段翻译成控制台里可读的中文运行状态。
function runtimeStatusView(health: AdminHealth | null, healthError: string) {
  if (healthError) {
    const missingEndpoint = /404|not found/i.test(healthError);
    return {
      tone: "attention",
      summary: missingEndpoint ? "需要更新" : "需要检查",
      hint: missingEndpoint ? "运行状态接口不可用，请重启服务" : "运行状态读取失败，请检查访问密钥",
      details: ["登录：未读取", "请求：按模型配置", "用量：未读取", "CLI：未读取"]
    };
  }

  if (!health) {
    return {
      tone: "pending",
      summary: "读取中",
      hint: "正在读取运行状态",
      details: ["登录：读取中", "请求：按模型配置", "用量：读取中", "CLI：读取中"]
    };
  }

  const oauthExpired = health.oauth.expired === true;
  const needsAttention = !health.oauth.available || oauthExpired || (health.usage.enabled && !health.usage.writable);
  let usageText = "正常";
  if (!health.usage.enabled) {
    usageText = "已关闭";
  } else if (!health.usage.writable) {
    usageText = "不可写";
  }
  const cliVersion = health.codex.client_version || "未检测到";
  let loginText = "已检测到";
  if (!health.oauth.available) {
    loginText = "未检测到";
  } else if (oauthExpired) {
    loginText = "已过期";
  }

  return {
    tone: needsAttention ? "attention" : "ok",
    summary: needsAttention ? "需要检查" : "正常",
    hint: needsAttention ? "打开配置页查看详情" : "OAuth 和日志状态正常",
    details: [
      `登录：${loginText}`,
      "请求：按模型配置",
      `用量：${usageText}`,
      `CLI：${cliVersion}`
    ]
  };
}

// 消息内容拆成普通文本和 fenced code block 两类，避免使用危险 HTML。
type MessagePart =
  | { type: "text"; text: string }
  | { type: "code"; language: string; code: string };

// 解析最常用的 Markdown fenced code block，其余 Markdown 先保持纯文本渲染。
function splitMessageParts(content: string): MessagePart[] {
  const parts: MessagePart[] = [];
  const fencePattern = /```([^\n`]*)\n([\s\S]*?)```/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = fencePattern.exec(content)) !== null) {
    const before = content.slice(cursor, match.index);
    if (before) {
      parts.push({ type: "text", text: before });
    }
    parts.push({ type: "code", language: match[1].trim(), code: match[2].replace(/\n$/, "") });
    cursor = match.index + match[0].length;
  }
  const rest = content.slice(cursor);
  if (rest) {
    parts.push({ type: "text", text: rest });
  }
  return parts.length ? parts : [{ type: "text", text: content }];
}

// 渲染消息正文，代码块提供独立复制按钮。
function MessageContent({ content }: { content: string }) {
  const parts = splitMessageParts(content);
  return (
    <div className="message-content">
      {parts.map((part, index) =>
        part.type === "code" ? (
          <div className="code-block" key={`${part.type}_${index}`}>
            <div className="code-block-head">
              <span>{part.language || "code"}</span>
              <button
                aria-label="复制代码"
                className="icon-button inline"
                onClick={() => void navigator.clipboard.writeText(part.code)}
              >
                <Copy size={14} />
              </button>
            </div>
            <pre>
              <code>{part.code}</code>
            </pre>
          </div>
        ) : (
          <p key={`${part.type}_${index}`}>{part.text}</p>
        )
      )}
    </div>
  );
}

// 用量中心的 KPI 卡保持紧凑，图标只辅助识别，不抢数字层级。
function UsageMetricCard({
  label,
  value,
  hint,
  icon
}: {
  label: string;
  value: string;
  hint?: string;
  icon?: ReactNode;
}) {
  return (
    <article className="usage-metric">
      <div>
        <span>{label}</span>
        <strong title={value}>{value}</strong>
        {hint ? <small>{hint}</small> : null}
      </div>
      {icon ? <span className="usage-metric-icon">{icon}</span> : null}
    </article>
  );
}

// 时间分布图只展示成功请求的输入/输出 token，避免暴露请求正文。
function TimelinePanel({ buckets }: { buckets: DashboardTimelineBucket[] }) {
  const maxTokens = Math.max(...buckets.map((bucket) => bucket.totalTokens), 1);
  return (
    <section className="usage-panel timeline-panel" aria-label="时间分布">
      <div className="usage-panel-heading">
        <h3>时间分布</h3>
        <span>{buckets.length ? `${buckets.length} 个时间桶` : "暂无数据"}</span>
      </div>
      <div className="timeline-chart" aria-label="按时间聚合的 token 使用量">
        {buckets.length ? (
          <>
            <div className="timeline-plot" role="list">
              {buckets.map((bucket) => {
                const height = bucket.totalTokens
                  ? Math.max((bucket.totalTokens / maxTokens) * 100, 8)
                  : bucket.requestCount
                    ? 4
                    : 0;
                const inputPercent = bucket.totalTokens
                  ? Math.round((bucket.inputTokens / bucket.totalTokens) * 100)
                  : 0;
                const outputPercent = bucket.totalTokens ? Math.max(0, 100 - inputPercent) : 0;
                const tooltip = `${bucket.label} · ${formatNumber(bucket.totalTokens)} tokens · 输入 ${formatNumber(bucket.inputTokens)} · 输出 ${formatNumber(bucket.outputTokens)} · ${bucket.requestCount} 次成功请求`;
                return (
                  <div className="timeline-bar" key={bucket.key} role="listitem" aria-label={tooltip} tabIndex={0}>
                    <div className="timeline-stack-anchor" style={{ height: `${height}%` }}>
                      <div className="timeline-stack">
                        <span className="timeline-segment output" style={{ height: `${outputPercent}%` }} />
                        <span className="timeline-segment input" style={{ height: `${inputPercent}%` }} />
                      </div>
                      <div className="timeline-tooltip" role="tooltip">
                        <strong>{bucket.label}</strong>
                        <span>总 {formatNumber(bucket.totalTokens)} tokens</span>
                        <span>输入 {formatNumber(bucket.inputTokens)}</span>
                        <span>输出 {formatNumber(bucket.outputTokens)}</span>
                        <span>{bucket.requestCount} 次成功请求</span>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
            <div className="timeline-axis" aria-hidden="true">
              {buckets.map((bucket) => (
                <small key={bucket.key}>{bucket.label}</small>
              ))}
            </div>
          </>
        ) : (
          <div className="usage-empty">当前范围暂无请求</div>
        )}
      </div>
    </section>
  );
}

// 分布面板用于模型、状态和接口排行。
function DistributionPanel({
  title,
  items,
  valueLabel = "tokens"
}: {
  title: string;
  items: DashboardDistributionItem[];
  valueLabel?: string;
}) {
  const maxValue = Math.max(...items.map((item) => Math.max(item.totalTokens, item.requestCount)), 1);
  return (
    <section className="usage-panel distribution-panel" aria-label={title}>
      <div className="usage-panel-heading">
        <h3>{title}</h3>
      </div>
      <div className="distribution-list">
        {items.length ? (
          items.slice(0, 6).map((item) => {
            const displayValue = item.totalTokens ? formatNumber(item.totalTokens) : `${item.requestCount} 次`;
            const width = (Math.max(item.totalTokens, item.requestCount) / maxValue) * 100;
            return (
              <div className="distribution-row" key={item.name}>
                <div className="distribution-label">
                  <strong title={item.name}>{item.name}</strong>
                  <span>{displayValue}</span>
                </div>
                <div className="distribution-track" aria-label={`${item.name} ${displayValue} ${valueLabel}`}>
                  <span style={{ width: `${Math.max(width, item.requestCount ? 3 : 0)}%` }} />
                </div>
              </div>
            );
          })
        ) : (
          <div className="usage-empty compact">暂无数据</div>
        )}
      </div>
    </section>
  );
}

// 请求洞察列表复用失败请求和慢请求两类紧凑行。
function RequestInsightList({
  title,
  items,
  emptyText
}: {
  title: string;
  items: RequestLogItem[];
  emptyText: string;
}) {
  return (
    <section className="usage-panel request-insights" aria-label={title}>
      <div className="usage-panel-heading">
        <h3>{title}</h3>
      </div>
      <div className="insight-list">
        {items.length ? (
          items.map((item) => (
            <div className="insight-row" key={item.id}>
              <div>
                <strong>{item.path}</strong>
                <span>{item.model || "-"} · {new Date(item.timestamp).toLocaleTimeString()}</span>
              </div>
              <small>{item.status_code >= 400 ? item.status_code : formatDuration(item.duration_ms)}</small>
            </div>
          ))
        ) : (
          <div className="usage-empty compact">{emptyText}</div>
        )}
      </div>
    </section>
  );
}

function formatResetTime(timestamp: number): string {
  if (!timestamp) {
    return "-";
  }
  return new Date(timestamp * 1000).toLocaleString();
}

function formatDisplayWeight(weight: number): string {
  // 把调度权重放大一千倍，并用普通十进制展示。
  return new Intl.NumberFormat("zh-CN", {
    maximumFractionDigits: 4,
    useGrouping: false
  }).format(weight * 1000);
}

function primaryUsageWindow(account: OAuthAccountSnapshot) {
  // 读取账号主 5 小时额度窗口。
  return account.usage?.rateLimit.windows.find((item) => item.kind === "primary");
}

function quotaTone(remainingPercent: number): "normal" | "warning" | "critical" {
  // 额度低于不同阈值时使用更醒目的提示颜色。
  if (remainingPercent < 5) {
    return "critical";
  }
  if (remainingPercent < 10) {
    return "warning";
  }
  return "normal";
}

function formatPlanType(planType: string): string {
  const normalized = planType.trim();
  if (!normalized) {
    return "-";
  }
  return `${normalized.slice(0, 1).toUpperCase()}${normalized.slice(1).toLowerCase()}`;
}

function UsageLimitCard({ title, rateLimit }: { title: string; rateLimit: CodexUsageRateLimit }) {
  return (
    <section className="codex-limit-card">
      <div className="codex-limit-card-head">
        <h3>{title}</h3>
        <span className={rateLimit.limitReached ? "status-fail" : "status-ok"}>
          {rateLimit.limitReached ? "已触达" : rateLimit.allowed ? "可用" : "不可用"}
        </span>
      </div>
      <div className="codex-limit-windows">
        {rateLimit.windows.map((window) => (
          <div className="codex-limit-window" key={`${title}-${window.kind}`}>
            <div className="codex-limit-window-head">
              <span>{window.label} 剩余</span>
              <strong>{window.remainingPercent}%</strong>
            </div>
            <div className="codex-limit-track" aria-hidden="true">
              <span style={{ width: `${window.remainingPercent}%` }} />
            </div>
            <small>重置：{formatResetTime(window.resetAt)}</small>
          </div>
        ))}
      </div>
    </section>
  );
}

// 本地控制台主组件，包含聊天、日志和配置三个工作区。
export function App() {
  const [activeTab, setActiveTab] = useState<ActiveTab>("dashboard");
  const [apiKey, setApiKey] = useState(() => localStorage.getItem("codex-console-api-key") || "");
  const [theme, setTheme] = useState<ThemeMode>(() => initialTheme());
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [model, setModel] = useState("gpt-5.5");
  const [reasoningEffort, setReasoningEffort] = useState("medium");
  const [fastMode, setFastMode] = useState(false);
  const [isStreaming, setIsStreaming] = useState(false);
  const [usage, setUsage] = useState<ChatUsage | null>(null);
  const [status, setStatus] = useState("就绪");
  const [error, setError] = useState("");
  const [logs, setLogs] = useState<RequestLogItem[]>([]);
  const [health, setHealth] = useState<AdminHealth | null>(null);
  const [healthError, setHealthError] = useState("");
  const [config, setConfig] = useState<AdminConfig | null>(null);
  const [configForm, setConfigForm] = useState<ConfigFormState | null>(null);
  const [modelCatalog, setModelCatalog] = useState<AdminModelCatalog | null>(null);
  const [modelCatalogError, setModelCatalogError] = useState("");
  const [configSavedNote, setConfigSavedNote] = useState("");
  const [modelRequestRows, setModelRequestRows] = useState<ModelRequestConfigRow[]>([]);
  const [modelRequestDirty, setModelRequestDirty] = useState(false);
  const [modelConfigSavedNote, setModelConfigSavedNote] = useState("");
  const [authReloadNote, setAuthReloadNote] = useState("");
  const [isReloadingAuth, setIsReloadingAuth] = useState(false);
  const [codexUsage, setCodexUsage] = useState<CodexUsageSnapshot | null>(null);
  const [codexUsageError, setCodexUsageError] = useState("");
  const [isLoadingCodexUsage, setIsLoadingCodexUsage] = useState(false);
  const [oauthAccounts, setOauthAccounts] = useState<OAuthAccountsSnapshot | null>(null);
  const [isLoadingOauthAccounts, setIsLoadingOauthAccounts] = useState(false);
  const [oauthLoginSession, setOauthLoginSession] = useState<OAuthLoginSession | null>(null);
  const [accountEditor, setAccountEditor] = useState<AccountEditorState | null>(null);
  const [dispatchEditor, setDispatchEditor] = useState<DispatchEditorState | null>(null);
  const [dashboardPreset, setDashboardPreset] = useState<DashboardRangePreset>("today");
  const [dashboardRecentDays, setDashboardRecentDays] = useState(7);
  const [dashboardSummary, setDashboardSummary] = useState<DashboardSummary>(() =>
    summarizeRequestLogs([], { preset: "today", recentDays: 7 })
  );
  const [requestLogLimit, setRequestLogLimit] = useState<number | "all">(1000);
  const [isLoadingLogs, setIsLoadingLogs] = useState(false);
  const [logSearch, setLogSearch] = useState("");
  const [logStatusFilter, setLogStatusFilter] = useState("all");
  const [selectedLogId, setSelectedLogId] = useState<string | null>(null);
  const [logScrollTop, setLogScrollTop] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  const chatStreamRef = useRef<HTMLDivElement | null>(null);
  const logScrollRef = useRef<HTMLDivElement | null>(null);
  const modelRequestDirtyRef = useRef(false);

  // layout effect 可以在浏览器绘制前应用主题，避免启动时闪一下错误主题。
  useLayoutEffect(() => {
    applyTheme(theme);
  }, [theme]);

  // 没有手动保存主题时继续监听系统偏好变化。
  useEffect(() => {
    if (isThemeMode(localStorage.getItem(THEME_STORAGE_KEY)) || !window.matchMedia) {
      return;
    }
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    const syncSystemTheme = (event: MediaQueryListEvent) => setTheme(event.matches ? "dark" : "light");
    query.addEventListener("change", syncSystemTheme);
    return () => query.removeEventListener("change", syncSystemTheme);
  }, []);

  // API key 只保存在浏览器本地，用于访问本机服务。
  useEffect(() => {
    localStorage.setItem("codex-console-api-key", apiKey);
  }, [apiKey]);

  // 读取管理配置，并把可编辑字段放进表单。
  const loadConfig = useCallback(async () => {
    try {
      setError("");
      const loaded = await fetchAdminConfig(apiKey);
      setConfig(loaded);
      setConfigForm(formFromConfig(loaded));
      setModel(loaded.codex.default_model);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "配置读取失败");
    }
  }, [apiKey]);

  // 读取动态模型目录，供聊天页和配置页共享模型/effort 选择。
  const loadModelCatalog = useCallback(
    async (force = false) => {
      try {
        setModelCatalogError("");
        const loaded = await fetchAdminModels(apiKey, force);
        setModelCatalog(loaded);
      } catch (caught) {
        setModelCatalog(null);
        setModelCatalogError(caught instanceof Error ? caught.message : "模型目录读取失败");
      }
    },
    [apiKey]
  );

  // 读取运行健康状态，驱动侧栏状态条。
  const loadHealth = useCallback(async () => {
    try {
      setHealthError("");
      const loaded = await fetchAdminHealth(apiKey);
      setHealth(loaded);
    } catch (caught) {
      setHealth(null);
      setHealthError(caught instanceof Error ? caught.message : "状态读取失败");
    }
  }, [apiKey]);

  // 读取看板聚合结果，不再为首页下载完整请求日志。
  const loadDashboard = useCallback(async () => {
    try {
      setError("");
      const summary = await fetchDashboardSummary(apiKey, dashboardPreset, dashboardRecentDays);
      setDashboardSummary(summary);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "看板读取失败");
    }
  }, [apiKey, dashboardPreset, dashboardRecentDays]);

  // 仅在进入日志页或主动刷新时读取明细，默认加载最近 1000 条。
  const loadLogs = useCallback(async (limit: number | "all" = requestLogLimit) => {
    try {
      setError("");
      setIsLoadingLogs(true);
      const items = await fetchRequestLogs(apiKey, limit);
      setLogs(items);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "请求日志读取失败");
    } finally {
      setIsLoadingLogs(false);
    }
  }, [apiKey, requestLogLimit]);

  // 读取当前 OAuth 账号的 Codex 额度状态，失败只影响独立页面。
  const loadCodexUsage = useCallback(async () => {
    try {
      setCodexUsageError("");
      setIsLoadingCodexUsage(true);
      const snapshot = await fetchCodexUsage(apiKey);
      setCodexUsage(snapshot);
    } catch (caught) {
      setCodexUsageError(caught instanceof Error ? caught.message : "额度状态读取失败");
    } finally {
      setIsLoadingCodexUsage(false);
    }
  }, [apiKey]);

  const loadOAuthAccounts = useCallback(async () => {
    try {
      setError("");
      setIsLoadingOauthAccounts(true);
      setOauthAccounts(await fetchOAuthAccounts(apiKey));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "OAuth 账号读取失败");
    } finally {
      setIsLoadingOauthAccounts(false);
    }
  }, [apiKey]);

  // 手动刷新时更新页面当前实际展示的数据源，多账号模式由后端批量强刷。
  const refreshQuotaStatus = useCallback(async () => {
    try {
      setCodexUsageError("");
      setIsLoadingCodexUsage(true);
      if (oauthAccounts?.accounts.length) {
        setOauthAccounts(await refreshOAuthAccounts(apiKey));
      } else {
        setCodexUsage(await fetchCodexUsage(apiKey));
      }
    } catch (caught) {
      setCodexUsageError(caught instanceof Error ? caught.message : "额度状态刷新失败");
    } finally {
      setIsLoadingCodexUsage(false);
    }
  }, [apiKey, oauthAccounts]);

  const syncAccounts = useCallback(async () => {
    try {
      setError("");
      setIsLoadingOauthAccounts(true);
      setOauthAccounts(await syncOAuthAccounts(apiKey));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "OAuth 同步失败");
    } finally {
      setIsLoadingOauthAccounts(false);
    }
  }, [apiKey]);

  const openDispatchEditor = useCallback(() => {
    if (!oauthAccounts?.accounts.length) {
      return;
    }
    const firstEnabled = oauthAccounts.accounts.find((account) => account.enabled);
    setDispatchEditor({
      mode: oauthAccounts.dispatchMode,
      singleAccountKey: oauthAccounts.singleAccountKey || firstEnabled?.key || oauthAccounts.accounts[0].key,
      enabledAccountKeys: oauthAccounts.accounts.filter((account) => account.enabled).map((account) => account.key),
      saving: false,
      error: ""
    });
  }, [oauthAccounts]);

  const saveDispatchEditor = useCallback(async () => {
    if (!dispatchEditor || dispatchEditor.saving) {
      return;
    }
    if (dispatchEditor.mode === "multi" && dispatchEditor.enabledAccountKeys.length === 0) {
      setDispatchEditor({ ...dispatchEditor, error: "请至少启用一个账户" });
      return;
    }
    if (dispatchEditor.mode === "single" && !dispatchEditor.singleAccountKey) {
      setDispatchEditor({ ...dispatchEditor, error: "请选择一个账户" });
      return;
    }
    setDispatchEditor({ ...dispatchEditor, saving: true, error: "" });
    try {
      const snapshot = await saveOAuthDispatch(
        apiKey,
        dispatchEditor.mode === "single"
          ? { mode: "single", singleAccountKey: dispatchEditor.singleAccountKey }
          : { mode: "multi", enabledAccountKeys: dispatchEditor.enabledAccountKeys }
      );
      setOauthAccounts(snapshot);
      setDispatchEditor(null);
    } catch (caught) {
      setDispatchEditor({
        ...dispatchEditor,
        saving: false,
        error: caught instanceof Error ? caught.message : "账户调度策略保存失败"
      });
    }
  }, [apiKey, dispatchEditor]);

  const beginOAuthLogin = useCallback(async (deviceAuth = false) => {
    try {
      setError("");
      let session = await startOAuthLogin(apiKey, deviceAuth);
      setOauthLoginSession(session);
      // 登录最多等待十分钟；后端负责浏览器回调和临时目录清理。
      for (let attempt = 0; attempt < 600 && session.status === "waiting"; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 1000));
        session = await fetchOAuthLoginStatus(apiKey, session.id);
        setOauthLoginSession(session);
      }
      if (session.status === "success") {
        await loadOAuthAccounts();
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "OAuth 登录失败");
    }
  }, [apiKey, loadOAuthAccounts]);

  useEffect(() => {
    if (oauthLoginSession?.status !== "success") {
      return;
    }
    // 成功提示只短暂保留，避免完成后的 CLI 日志长期占据账号列表上方。
    const sessionId = oauthLoginSession.id;
    const timeout = window.setTimeout(() => {
      setOauthLoginSession((current) => current?.id === sessionId ? null : current);
    }, 3000);
    return () => window.clearTimeout(timeout);
  }, [oauthLoginSession]);

  const saveAccountEditor = useCallback(async () => {
    if (!accountEditor || accountEditor.saving) {
      return;
    }
    let patch: { alias: string } | { maxConcurrency: number | null };
    if (accountEditor.mode === "rename") {
      const alias = accountEditor.alias.trim();
      if (!alias) {
        setAccountEditor({ ...accountEditor, error: "账号别名不能为空" });
        return;
      }
      patch = { alias };
    } else if (accountEditor.concurrencyMode === "unlimited") {
      patch = { maxConcurrency: null };
    } else {
      const limit = Number(accountEditor.maxConcurrency);
      if (!Number.isInteger(limit) || limit <= 0) {
        setAccountEditor({ ...accountEditor, error: "最大并发数必须是正整数" });
        return;
      }
      patch = { maxConcurrency: limit };
    }
    setAccountEditor({ ...accountEditor, saving: true, error: "" });
    try {
      const snapshot = await updateOAuthAccount(apiKey, accountEditor.account.key, patch);
      setOauthAccounts(snapshot);
      setAccountEditor(null);
    } catch (caught) {
      setAccountEditor({
        ...accountEditor,
        saving: false,
        error: caught instanceof Error ? caught.message : "账号设置保存失败"
      });
    }
  }, [accountEditor, apiKey]);

  // 配置加载成功后，同步默认模型和动态目录到聊天区。
  useEffect(() => {
    void loadConfig();
    void loadModelCatalog();
  }, [loadConfig, loadModelCatalog]);

  // 看板是默认首页，启动时只读取服务端聚合数据。
  useEffect(() => {
    void loadDashboard();
  }, [loadDashboard]);

  // 健康状态变化频率低，初始化和访问密钥变化时读取即可。
  useEffect(() => {
    void loadHealth();
  }, [loadHealth]);

  useEffect(() => {
    if (activeTab !== "account-load") {
      return;
    }
    // 负载页可见时每秒读取实时并发，离开页面立即停止轮询。
    void loadOAuthAccounts();
    const interval = window.setInterval(() => void loadOAuthAccounts(), 1000);
    return () => window.clearInterval(interval);
  }, [activeTab, loadOAuthAccounts]);

  // 流式输出时自动滚动到最新消息。
  useEffect(() => {
    if (chatStreamRef.current) {
      chatStreamRef.current.scrollTop = chatStreamRef.current.scrollHeight;
    }
  }, [messages, isStreaming]);

  // 发送聊天请求并逐块读取 SSE。
  const sendMessage = useCallback(
    async (overrideText?: string) => {
      const text = (overrideText ?? input).trim();
      if (!text || isStreaming) {
        return;
      }

      const userMessage: ChatMessage = { id: newId("user"), role: "user", content: text };
      const assistantMessage: ChatMessage = { id: newId("assistant"), role: "assistant", content: "" };
      setMessages((current) => [...current, userMessage, assistantMessage]);
      setInput("");
      setUsage(null);
      setError("");
      setStatus("连接中");
      setIsStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;

      try {
        const response = await fetch("/v1/chat/completions", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            ...buildAuthHeaders(apiKey)
          },
          body: JSON.stringify({
            model,
            messages: [...messages, userMessage].map((message) => ({
              role: message.role,
              content: message.content
            })),
            stream: true,
            stream_options: { include_usage: true },
            reasoning_effort: reasoningEffort,
            fast_mode: fastMode
          }),
          signal: controller.signal
        });

        if (!response.ok || !response.body) {
          throw new Error(`请求失败：${response.status}`);
        }

        setStatus("生成中");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        let hadStreamError = false;

        while (true) {
          const { value, done } = await reader.read();
          if (done) {
            break;
          }
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() || "";

          for (const line of lines) {
            const parsed = parseChatStreamLine(line);
            if (parsed.type === "delta") {
              setMessages((current) =>
                current.map((message) =>
                  message.id === assistantMessage.id
                    ? { ...message, content: message.content + parsed.text }
                    : message
                )
              );
            }
            if (parsed.type === "usage") {
              setUsage(parsed.usage);
            }
            if (parsed.type === "error") {
              hadStreamError = true;
              const message = `请求失败：${parsed.message}`;
              setError(message);
              setStatus("失败");
              setMessages((current) =>
                current.map((messageItem) =>
                  messageItem.id === assistantMessage.id && !messageItem.content
                    ? { ...messageItem, content: message }
                    : messageItem
                )
              );
            }
            if (parsed.type === "done") {
              setStatus(hadStreamError ? "失败" : "完成");
            }
          }
        }
        setStatus(hadStreamError ? "失败" : "完成");
        void loadDashboard();
      } catch (caught) {
        if ((caught as Error).name === "AbortError") {
          setStatus("已停止");
        } else {
          setError(caught instanceof Error ? caught.message : "聊天请求失败");
          setStatus("失败");
        }
      } finally {
        setIsStreaming(false);
        abortRef.current = null;
      }
    },
    [apiKey, fastMode, input, isStreaming, loadDashboard, messages, model, reasoningEffort]
  );

  // 停止当前流式请求。
  const stopStreaming = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  // 手动切换主题后立即保存选择，后续刷新不再被系统偏好覆盖。
  const toggleTheme = useCallback(() => {
    setTheme((currentTheme) => {
      const nextTheme = currentTheme === "dark" ? "light" : "dark";
      localStorage.setItem(THEME_STORAGE_KEY, nextTheme);
      return nextTheme;
    });
  }, []);

  // 重发最近一条用户消息。
  const retryLast = useCallback(() => {
    const lastUser = [...messages].reverse().find((message) => message.role === "user");
    if (lastUser) {
      void sendMessage(lastUser.content);
    }
  }, [messages, sendMessage]);

  // 保存配置到 config.yaml。
  const saveConfig = useCallback(async () => {
    if (!configForm) {
      return;
    }
    try {
      setError("");
      setConfigSavedNote("");
      const result = await saveAdminConfig(apiKey, {
        ...(configForm.localApiKeyTouched ? { api: { local_api_key: configForm.localApiKey } } : {}),
        codex: {
          default_model: configForm.defaultModel
        },
        usage: { enabled: configForm.usageEnabled },
        auth: {
          import_auth_path: configForm.importAuthPath
        },
        concurrency: {
          global_max: configForm.globalMaxConcurrency,
          queue_timeout_seconds: configForm.queueTimeoutSeconds
        }
      });
      setConfigSavedNote(result.restart_required ? "已保存，重启服务后生效" : "已保存，已立即生效");
      setConfigForm({ ...configForm, localApiKey: "", localApiKeyTouched: false });
      void loadConfig();
      void loadHealth();
      void loadModelCatalog();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "配置保存失败");
    }
  }, [apiKey, configForm, loadConfig, loadHealth, loadModelCatalog]);

  // 保存全部模型请求缺省值；序列化时省略 medium/false，保持配置文件简洁。
  const saveModelConfig = useCallback(async () => {
    try {
      setError("");
      setModelConfigSavedNote("");
      const result = await saveAdminConfig(apiKey, {
        codex: { model_request_defaults: serializeModelRequestOverrides(modelRequestRows) }
      });
      setModelConfigSavedNote(result.restart_required ? "已保存，重启服务后生效" : "已保存，已立即生效");
      modelRequestDirtyRef.current = false;
      setModelRequestDirty(false);
      await Promise.all([loadConfig(), loadHealth(), loadModelCatalog()]);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "模型配置保存失败");
    }
  }, [apiKey, loadConfig, loadHealth, loadModelCatalog, modelRequestRows]);

  // 从 Codex CLI/App 的登录文件同步 OAuth 凭据，解决本服务缓存旧 token 的场景。
  const reloadAuth = useCallback(async () => {
    try {
      setError("");
      setAuthReloadNote("");
      setIsReloadingAuth(true);
      const result = await reloadCodexAuth(apiKey);
      setAuthReloadNote(result.oauth.reloaded ? "已同步 Codex 登录" : result.message || "未同步 Codex 登录");
      await loadHealth();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "OAuth 同步失败");
    } finally {
      setIsReloadingAuth(false);
    }
  }, [apiKey, loadHealth]);

  // 日志过滤只匹配元数据字段，仍然不读取或展示 prompt。
  const filteredLogs = useMemo(() => {
    const query = logSearch.trim().toLowerCase();
    return logs.filter((item) => {
      const statusMatches =
        logStatusFilter === "all" ||
        (logStatusFilter === "success" && item.status_code < 400) ||
        (logStatusFilter === "failed" && item.status_code >= 400);
      const queryText = [item.path, item.model, item.status_code, item.request_id, item.error]
        .filter((value) => value !== null && value !== undefined)
        .join(" ")
        .toLowerCase();
      return statusMatches && (!query || queryText.includes(query));
    });
  }, [logSearch, logStatusFilter, logs]);

  // 日志页摘要只统计当前已加载且经过筛选的明细。
  const loadedLogSummary = useMemo(
    () => summarizeRequestLogs(filteredLogs, { preset: "all" }),
    [filteredLogs]
  );
  const selectedLog = useMemo(
    () => logs.find((item) => item.id === selectedLogId) || null,
    [logs, selectedLogId]
  );
  const virtualLogWindow = useMemo(
    () =>
      calculateVirtualLogWindow({
        itemCount: filteredLogs.length,
        scrollTop: logScrollTop,
        viewportHeight: 512
      }),
    [filteredLogs.length, logScrollTop]
  );

  // 日志集合或筛选条件变化后回到顶部，避免旧滚动位置落到空白区域。
  useEffect(() => {
    setLogScrollTop(0);
    setSelectedLogId(null);
    if (logScrollRef.current) {
      logScrollRef.current.scrollTop = 0;
    }
  }, [logSearch, logStatusFilter, logs]);

  // 请求详情打开时支持 Escape 关闭，并在组件更新时清理监听。
  useEffect(() => {
    if (!selectedLogId) {
      return;
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setSelectedLogId(null);
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [selectedLogId]);

  // 切换加载数量时立即读取对应范围，全部模式仍由虚拟列表控制 DOM 数量。
  const changeRequestLogLimit = useCallback(
    (rawValue: string) => {
      const nextLimit: number | "all" = rawValue === "all" ? "all" : Number(rawValue);
      setRequestLogLimit(nextLimit);
      void loadLogs(nextLimit);
    },
    [loadLogs]
  );

  // 中文运行状态同时驱动左侧汇总和配置页明细，避免两处文案漂移。
  const runtimeStatus = useMemo(
    () => runtimeStatusView(health, healthError),
    [health, healthError]
  );

  const modelOptions = useMemo(
    () =>
      buildModelOptions(
        modelCatalog,
        config,
        [model, configForm?.defaultModel || ""],
        "medium"
      ),
    [config, configForm?.defaultModel, model, modelCatalog]
  );
  const chatReasoningOptions = useMemo(() => reasoningOptionsFor(modelOptions, model), [model, modelOptions]);
  const oauthLoadScale = useMemo(() => oauthAccounts ? accountLoadScale(oauthAccounts) : 5, [oauthAccounts]);

  // 目录或配置变化时刷新模型配置行，但保留用户尚未保存的编辑。
  useEffect(() => {
    if (!config || modelRequestDirtyRef.current) {
      return;
    }
    const rows = buildModelRequestRows(modelCatalog, config);
    setModelRequestRows(rows);
    const defaults = requestDefaultsForModel(rows, config.codex.default_model);
    setReasoningEffort(defaults.reasoning_effort);
    setFastMode(defaults.fast_mode);
  }, [config, modelCatalog, modelRequestDirty]);

  // 目录刷新后，如果当前 effort 不再被所选模型支持，自动切到该模型默认值。
  useEffect(() => {
    setReasoningEffort((current) => effortForModel(modelOptions, model, current));
  }, [model, modelOptions]);

  const updateChatModel = useCallback(
    (nextModel: string) => {
      setModel(nextModel);
      const defaults = requestDefaultsForModel(modelRequestRows, nextModel);
      setReasoningEffort(effortForModel(modelOptions, nextModel, defaults.reasoning_effort));
      setFastMode(defaults.fast_mode);
    },
    [modelOptions, modelRequestRows]
  );

  const updateModelRequestRow = useCallback(
    (modelId: string, patch: Partial<Pick<ModelRequestConfigRow, "reasoningEffort" | "fastMode">>) => {
      setModelRequestRows((current) =>
        current.map((row) => (row.id === modelId ? { ...row, ...patch } : row))
      );
      modelRequestDirtyRef.current = true;
      setModelRequestDirty(true);
      setModelConfigSavedNote("");
    },
    []
  );

  const refreshConfigPage = useCallback(async () => {
    await Promise.all([loadConfig(), loadHealth(), loadModelCatalog(true)]);
  }, [loadConfig, loadHealth, loadModelCatalog]);

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-block">
          <div className="brand-mark">C</div>
          <div>
            <h1>Codex API Console</h1>
            <p>{currentDisplayHost()}</p>
          </div>
          <button
            aria-label={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"}
            className="theme-toggle"
            onClick={toggleTheme}
            title={theme === "dark" ? "切换到浅色模式" : "切换到深色模式"}
          >
            {theme === "dark" ? <Sun size={18} /> : <Moon size={18} />}
          </button>
        </div>

        <div className={`runtime-summary ${runtimeStatus.tone}`} aria-label="服务状态">
          <span className="runtime-summary-label">服务状态</span>
          <strong>{runtimeStatus.summary}</strong>
          <small>{runtimeStatus.hint}</small>
        </div>

        <nav className="nav-tabs" aria-label="主导航">
          <button
            className={activeTab === "dashboard" ? "active" : ""}
            onClick={() => {
              setActiveTab("dashboard");
              void loadDashboard();
            }}
          >
            <BarChart3 size={18} />
            看板
          </button>
          <button className={activeTab === "chat" ? "active" : ""} onClick={() => setActiveTab("chat")}>
            <MessageSquare size={18} />
            聊天
          </button>
          <button
            className={activeTab === "logs" ? "active" : ""}
            onClick={() => {
              setActiveTab("logs");
              void loadLogs(requestLogLimit);
            }}
          >
            <Activity size={18} />
            请求日志
          </button>
          <button
            className={activeTab === "config" ? "active" : ""}
            onClick={() => {
              setActiveTab("config");
              void loadConfig();
              void loadModelCatalog();
              void loadOAuthAccounts();
            }}
          >
            <Settings size={18} />
            配置
          </button>
          <button
            className={activeTab === "model-config" ? "active" : ""}
            onClick={() => {
              setActiveTab("model-config");
              void loadConfig();
              void loadModelCatalog();
            }}
          >
            <SlidersHorizontal size={18} />
            模型配置
          </button>
          <button
            className={activeTab === "usage-status" ? "active" : ""}
            onClick={() => {
              setActiveTab("usage-status");
              void loadCodexUsage();
              void loadOAuthAccounts();
            }}
          >
            <Gauge size={18} />
            额度状态
          </button>
          <button
            className={activeTab === "oauth-accounts" ? "active" : ""}
            onClick={() => {
              setActiveTab("oauth-accounts");
              void loadOAuthAccounts();
            }}
          >
            <Users size={18} />
            OAuth 账号
          </button>
          <button
            className={activeTab === "account-load" ? "active" : ""}
            onClick={() => setActiveTab("account-load")}
          >
            <Activity size={18} />
            账户并发负载
          </button>
        </nav>

        <label className="api-key-field">
          <span>
            <KeyRound size={15} />
            访问密钥
          </span>
          <input
            type="password"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            placeholder="Bearer token"
          />
        </label>
      </aside>

      <main className="workspace">
        {error ? (
          <div className="banner error">
            <AlertTriangle size={18} />
            {error}
          </div>
        ) : null}

        {activeTab === "dashboard" ? (
          <section className="usage-dashboard" aria-label="数据看板">
            <header className="usage-topbar">
              <div>
                <h2>Codex API Service Usage</h2>
                <p>
                  {formatNumber(dashboardSummary.totalTokens)} tokens · {dashboardSummary.requestCount} 条请求 ·{" "}
                  {dashboardSummary.lastUpdated}
                </p>
              </div>
              <button className="secondary-button" onClick={() => void loadDashboard()}>
                <RefreshCw size={16} />
                刷新
              </button>
            </header>

            <div className="usage-filterbar" aria-label="用量范围">
              <div className="range-segments">
                {DASHBOARD_RANGE_OPTIONS.map((option) => (
                  <button
                    className={dashboardPreset === option.value ? "active" : ""}
                    key={option.value}
                    onClick={() => setDashboardPreset(option.value)}
                    type="button"
                  >
                    {option.label}
                  </button>
                ))}
              </div>
              {dashboardPreset === "recent" ? (
                <label className="recent-days-field">
                  <span>天数</span>
                  <input
                    min={1}
                    max={365}
                    type="number"
                    value={dashboardRecentDays}
                    onChange={(event) => setDashboardRecentDays(Math.max(1, Number(event.target.value) || 1))}
                  />
                </label>
              ) : null}
              <span className="usage-range-note">
                {dashboardSummary.rangeLabel} · {dashboardSummary.bucketLabel}
              </span>
            </div>

            <div className="usage-metrics" aria-label="API Service 用量概览">
              <UsageMetricCard
                icon={<Database size={18} />}
                label="总 tokens"
                value={formatNumber(dashboardSummary.totalTokens)}
              />
              <UsageMetricCard
                label="输入"
                value={formatNumber(dashboardSummary.tokenBreakdown.input)}
              />
              <UsageMetricCard
                label="缓存输入"
                value={formatNumber(dashboardSummary.tokenBreakdown.cached)}
              />
              <UsageMetricCard
                label="输出"
                value={formatNumber(dashboardSummary.tokenBreakdown.output)}
              />
              <UsageMetricCard
                label="推理输出"
                value={formatNumber(dashboardSummary.tokenBreakdown.reasoning)}
              />
              <UsageMetricCard
                icon={<Activity size={18} />}
                label="请求数"
                value={formatNumber(dashboardSummary.requestCount)}
              />
              <UsageMetricCard
                icon={<CheckCircle2 size={18} />}
                label="成功率"
                value={`${dashboardSummary.successRate}%`}
                hint={`${dashboardSummary.errorCount} 条失败`}
              />
              <UsageMetricCard
                icon={<Clock3 size={18} />}
                label="平均耗时"
                value={formatDuration(dashboardSummary.averageDurationMs)}
                hint={`常用模型 ${dashboardSummary.topModel}`}
              />
            </div>

            <div className="usage-main-grid">
              <TimelinePanel buckets={dashboardSummary.timeline} />
              <div className="usage-side-stack">
                <DistributionPanel title="模型分布" items={dashboardSummary.modelDistribution} />
                <DistributionPanel title="状态分布" items={dashboardSummary.statusDistribution} valueLabel="请求" />
                <DistributionPanel title="接口分布" items={dashboardSummary.endpointDistribution} />
              </div>
            </div>

            <div className="usage-bottom-grid">
              <RequestInsightList
                emptyText="当前范围没有失败请求"
                items={dashboardSummary.recentFailures}
                title="最近失败"
              />
              <RequestInsightList
                emptyText="当前范围没有请求"
                items={dashboardSummary.slowRequests}
                title="慢请求 Top"
              />
              <section className="usage-panel usage-log-status" aria-label="usage 日志状态">
                <div className="usage-panel-heading">
                  <h3>usage 日志</h3>
                </div>
                <div className="usage-status-grid">
                  <span>
                    <strong>{config ? (config.usage.enabled ? "开启" : "关闭") : "读取中"}</strong>
                    写入状态
                  </span>
                  <span>
                    <strong>{health?.usage.writable === false ? "不可写" : "正常"}</strong>
                    文件权限
                  </span>
                  <span title={config?.usage.path || ""}>
                    <strong>{config?.usage.path ? "已配置" : "读取中"}</strong>
                    日志路径
                  </span>
                </div>
              </section>
            </div>
          </section>
        ) : null}

        {activeTab === "chat" ? (
          <section className="chat-layout" aria-label="聊天">
            <header className="toolbar">
              <div>
                <h2>聊天</h2>
                <p>{status}</p>
              </div>
              <div className="control-row">
                <select value={model} onChange={(event) => updateChatModel(event.target.value)}>
                  {modelOptions.map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.display_name || item.id}
                    </option>
                  ))}
                </select>
                <select value={reasoningEffort} onChange={(event) => setReasoningEffort(event.target.value)}>
                  {chatReasoningOptions.map((effort) => (
                    <option key={effort} value={effort}>
                      {effort}
                    </option>
                  ))}
                </select>
                <label className="compact-toggle">
                  <input
                    type="checkbox"
                    checked={fastMode}
                    onChange={(event) => setFastMode(event.target.checked)}
                  />
                  <span>
                    <Zap size={14} />
                    快速模式
                  </span>
                </label>
              </div>
            </header>

            <div className="chat-stream" ref={chatStreamRef}>
              {messages.length === 0 ? (
                <div className="empty-state">
                  <MessageSquare size={28} />
                  <h3>开始一次本地 Codex 对话</h3>
                  <p>消息会通过当前服务的 OpenAI-compatible 接口发送。</p>
                </div>
              ) : (
                messages.map((message) => (
	                  <article className={`message ${message.role}`} key={message.id}>
	                    <div className="message-role">{message.role === "user" ? "你" : "Codex"}</div>
	                    <MessageContent content={message.content || (message.role === "assistant" ? "正在生成..." : "")} />
	                    {message.role === "assistant" && message.content ? (
	                      <button
	                        aria-label="复制消息"
	                        className="icon-button"
	                        onClick={() => void navigator.clipboard.writeText(message.content)}
	                      >
	                        <Copy size={15} />
	                      </button>
                    ) : null}
                  </article>
                ))
              )}
            </div>

            <footer className="composer">
              <textarea
                value={input}
                onChange={(event) => setInput(event.target.value)}
                placeholder="输入消息..."
                onKeyDown={(event) => {
                  // 中文等输入法确认候选词时不能误触发发送。
                  if (event.nativeEvent.isComposing) {
                    return;
                  }
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void sendMessage();
                  }
                }}
              />
              <div className="composer-actions">
                <div className="usage-pill">
                  {usage ? `tokens ${usage.total_tokens}` : "tokens --"}
                </div>
                <button className="secondary-button" onClick={retryLast} disabled={isStreaming || messages.length === 0}>
                  <RefreshCw size={16} />
                  重试
                </button>
                {isStreaming ? (
                  <button className="danger-button" onClick={stopStreaming}>
                    <Square size={16} />
                    停止
                  </button>
                ) : (
                  <button className="primary-button" onClick={() => void sendMessage()}>
                    <Send size={16} />
                    发送
                  </button>
                )}
              </div>
            </footer>
          </section>
        ) : null}

        {activeTab === "logs" ? (
          <section className="panel" aria-label="请求日志">
            <header className="toolbar">
              <div>
                <h2>请求日志</h2>
                <p>
                  {loadedLogSummary.requestCount} 条记录，{loadedLogSummary.successCount} 条成功，
                  {formatCompactNumber(loadedLogSummary.totalTokens)} tokens
                </p>
              </div>
              <button className="secondary-button" onClick={() => void loadLogs(requestLogLimit)} disabled={isLoadingLogs}>
                <RefreshCw size={16} />
                {isLoadingLogs ? "加载中" : "刷新"}
              </button>
            </header>
            <div className="log-controls">
              <input
                value={logSearch}
                onChange={(event) => setLogSearch(event.target.value)}
                placeholder="搜索日志"
              />
              <select value={logStatusFilter} onChange={(event) => setLogStatusFilter(event.target.value)}>
                <option value="all">全部状态</option>
                <option value="success">仅成功</option>
                <option value="failed">仅失败</option>
              </select>
              <label className="log-limit-field">
                <span>加载数量</span>
                <select
                  aria-label="日志加载数量"
                  value={String(requestLogLimit)}
                  onChange={(event) => changeRequestLogLimit(event.target.value)}
                >
                  <option value="100">100 条</option>
                  <option value="500">500 条</option>
                  <option value="1000">1000 条</option>
                  <option value="5000">5000 条</option>
                  <option value="all">全部</option>
                </select>
              </label>
            </div>
            <div
              aria-label="请求日志表格"
              className="virtual-log-table"
              ref={logScrollRef}
              role="table"
              onScroll={(event) => setLogScrollTop(Math.max(0, event.currentTarget.scrollTop - 48))}
            >
              <div className="virtual-log-grid virtual-log-header" role="row">
                <div role="columnheader">时间</div>
                <div role="columnheader">接口</div>
                <div role="columnheader">模型</div>
                <div role="columnheader">状态</div>
                <div role="columnheader">耗时</div>
                <div role="columnheader">Tokens</div>
                <div role="columnheader">输入</div>
                <div role="columnheader">输出</div>
                <div role="columnheader">推理</div>
                <div role="columnheader">错误</div>
              </div>
              <div className="virtual-log-spacer" style={{ height: virtualLogWindow.totalHeight }}>
                {virtualLogWindow.items.map((virtualItem) => {
                  const item = filteredLogs[virtualItem.index];
                  const isSelected = selectedLogId === item.id;
                  return (
                    <div
                      className={`virtual-log-item${isSelected ? " selected" : ""}`}
                      key={item.id}
                      role="row"
                      style={{ height: virtualItem.height, transform: `translateY(${virtualItem.top}px)` }}
                    >
                      <div
                        className="virtual-log-grid virtual-log-main"
                        onClick={() => setSelectedLogId(item.id)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            setSelectedLogId(item.id);
                          }
                        }}
                        tabIndex={0}
                      >
                        <div role="cell">{new Date(item.timestamp).toLocaleTimeString()}</div>
                        <div role="cell">{item.path}</div>
                        <div role="cell">{item.model || "-"}</div>
                        <div role="cell">
                          <span className={item.status_code < 400 ? "status-ok" : "status-fail"}>
                            {item.status_code}
                          </span>
                        </div>
                        <div role="cell">{item.duration_ms}ms</div>
                        <div role="cell">{item.usage?.total ?? "-"}</div>
                        <div role="cell">{item.usage?.input ?? "-"}</div>
                        <div role="cell">{item.usage?.output ?? "-"}</div>
                        <div role="cell">{item.usage?.reasoning ?? "-"}</div>
                        <div className="error-cell" role="cell">{item.error || "-"}</div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </section>
        ) : null}

        {activeTab === "config" ? (
          <section className="panel" aria-label="配置">
            <header className="toolbar">
              <div>
                <h2>配置</h2>
                <p>{config?.config_path || "config.yaml"}</p>
              </div>
              <button className="secondary-button" onClick={() => void refreshConfigPage()}>
                <RefreshCw size={16} />
                刷新
              </button>
            </header>

            <div className="runtime-detail-panel" aria-label="运行状态详情">
              <div className="runtime-detail-head">
                <div>
                  <h3>运行状态</h3>
                  <p>{runtimeStatus.hint}</p>
                </div>
                <div className="runtime-detail-actions">
                  {authReloadNote ? (
                    <span className="saved-note">
                      <CheckCircle2 size={16} />
                      {authReloadNote}
                    </span>
                  ) : null}
                  <button className="secondary-button" onClick={() => void reloadAuth()} disabled={isReloadingAuth}>
                    <KeyRound size={16} />
                    {isReloadingAuth ? "同步中" : "同步 Codex 登录"}
                  </button>
                </div>
              </div>
              <div className="runtime-detail-grid">
                {runtimeStatus.details.map((detail) => (
                  <span key={detail}>{detail}</span>
                ))}
              </div>
            </div>

            <div className="dispatch-policy-card">
              <div>
                <span>账户调度策略</span>
                <strong>
                  {oauthAccounts?.dispatchMode === "single" ? "单账户模式" : "多账户模式"}
                </strong>
                <small>
                  {oauthAccounts
                    ? `已启用 ${oauthAccounts.accounts.filter((account) => account.enabled).length}/${oauthAccounts.accounts.length} 个账户`
                    : "正在读取账户状态"}
                </small>
              </div>
              <button
                className="secondary-button"
                aria-label="设置账户调度策略"
                onClick={openDispatchEditor}
                disabled={!oauthAccounts?.accounts.length}
              >
                <SlidersHorizontal size={16} />
                设置
              </button>
            </div>

            {configForm ? (
              <div className="config-grid">
                <label>
                  <span>保存到配置的 API key</span>
                  <input
                    type="password"
                    value={configForm.localApiKey}
                    placeholder={config?.api.local_api_key_configured ? "已配置，输入新值后保存" : "未配置"}
                    onChange={(event) =>
                      setConfigForm({ ...configForm, localApiKey: event.target.value, localApiKeyTouched: true })
                    }
                  />
                  {config?.api.local_api_key_configured ? (
                    <button
                      className="secondary-button inline-field-button"
                      type="button"
                      onClick={() => setConfigForm({ ...configForm, localApiKey: "", localApiKeyTouched: true })}
                    >
                      清除 API key
                    </button>
                  ) : null}
                </label>
                <label>
                  <span>默认模型</span>
                  <select
                    aria-label="默认模型"
                    value={configForm.defaultModel}
                    onChange={(event) => setConfigForm({ ...configForm, defaultModel: event.target.value })}
                  >
                    {modelOptions.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.display_name || item.id}
                      </option>
                    ))}
                  </select>
                  {modelCatalogError ? <small className="field-error">{modelCatalogError}</small> : null}
                </label>
                <label>
                  <span>Codex 登录同步来源</span>
                  <input
                    value={configForm.importAuthPath}
                    onChange={(event) => setConfigForm({ ...configForm, importAuthPath: event.target.value })}
                  />
                </label>
                <label>
                  <span>项目 OAuth 账号库</span>
                  <input value={config?.auth.account_store_path || ".codex-oauth"} readOnly />
                </label>
                <label>
                  <span>全局最大并发数</span>
                  <input
                    type="number"
                    min="1"
                    value={configForm.globalMaxConcurrency}
                    placeholder="不限制"
                    onChange={(event) => setConfigForm({ ...configForm, globalMaxConcurrency: event.target.value })}
                  />
                </label>
                <label>
                  <span>等待队列超时（秒）</span>
                  <input
                    type="number"
                    min="1"
                    value={configForm.queueTimeoutSeconds}
                    onChange={(event) => setConfigForm({ ...configForm, queueTimeoutSeconds: event.target.value })}
                  />
                </label>
                <label className="toggle-line">
                  <input
                    type="checkbox"
                    checked={configForm.usageEnabled}
                    onChange={(event) => setConfigForm({ ...configForm, usageEnabled: event.target.checked })}
                  />
                  <span>写入 usage 日志</span>
                </label>
              </div>
            ) : (
              <div className="empty-state">
                <Settings size={28} />
                <h3>配置未加载</h3>
                <p>填写访问密钥后刷新配置。</p>
              </div>
            )}

            <div className="save-row">
              {configSavedNote ? (
                <span className="saved-note">
                  <CheckCircle2 size={16} />
                  {configSavedNote}
                </span>
              ) : null}
              <button className="primary-button" onClick={() => void saveConfig()} disabled={!configForm}>
                <Save size={16} />
                保存
              </button>
            </div>
          </section>
        ) : null}

        {activeTab === "model-config" ? (
          <section className="panel" aria-label="模型配置">
            <header className="toolbar">
              <div>
                <h2>模型配置</h2>
                <p>请求未显式传参时，按真实 Codex 模型应用以下缺省值。</p>
              </div>
              <button className="secondary-button" onClick={() => void loadModelCatalog(true)}>
                <RefreshCw size={16} />
                刷新模型
              </button>
            </header>

            {modelRequestRows.length ? (
              <div className="model-config-list">
                <div className="model-config-header" aria-hidden="true">
                  <span>模型</span>
                  <span>Reasoning effort</span>
                  <span>快速模式</span>
                  <span>操作</span>
                </div>
                {modelRequestRows.map((row) => (
                  <div className="model-config-row" key={row.id}>
                    <div className="model-config-name">
                      <strong>{row.displayName}</strong>
                      <small>{row.id}</small>
                      {!row.available ? <span className="model-unavailable">当前不可用</span> : null}
                    </div>
                    <label>
                      <span className="mobile-field-label">Reasoning effort</span>
                      <select
                        aria-label={`${row.id} Effort`}
                        value={row.reasoningEffort}
                        onChange={(event) =>
                          updateModelRequestRow(row.id, { reasoningEffort: event.target.value })
                        }
                      >
                        {row.supportedReasoningEfforts.map((effort) => (
                          <option key={effort} value={effort}>
                            {effort}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="model-fast-toggle">
                      <input
                        aria-label={`${row.id} 快速模式`}
                        type="checkbox"
                        checked={row.fastMode}
                        onChange={(event) => updateModelRequestRow(row.id, { fastMode: event.target.checked })}
                      />
                      <span>快速模式</span>
                    </label>
                    <button
                      aria-label={`${row.id} 恢复默认`}
                      className="secondary-button"
                      type="button"
                      onClick={() =>
                        updateModelRequestRow(row.id, { reasoningEffort: "medium", fastMode: false })
                      }
                    >
                      恢复默认
                    </button>
                  </div>
                ))}
              </div>
            ) : (
              <div className="empty-state">
                <SlidersHorizontal size={28} />
                <h3>模型目录未加载</h3>
                <p>{modelCatalogError || "刷新模型目录后再配置。"}</p>
              </div>
            )}

            <div className="save-row">
              {modelConfigSavedNote ? (
                <span className="saved-note">
                  <CheckCircle2 size={16} />
                  {modelConfigSavedNote}
                </span>
              ) : null}
              <button
                className="primary-button"
                onClick={() => void saveModelConfig()}
                disabled={!modelRequestRows.length}
              >
                <Save size={16} />
                保存模型配置
              </button>
            </div>
          </section>
        ) : null}

        {activeTab === "oauth-accounts" ? (
          <section className="panel" aria-label="OAuth 账号">
            <header className="toolbar">
              <div>
                <h2>OAuth 账号</h2>
                <p>管理项目账号库、账号并发和 Codex CLI 登录同步。</p>
              </div>
              <div className="toolbar-actions oauth-toolbar-actions">
                <button className="secondary-button oauth-toolbar-button" onClick={() => void loadOAuthAccounts()} disabled={isLoadingOauthAccounts}>
                  <RefreshCw size={16} />
                  刷新
                </button>
                <button className="primary-button oauth-toolbar-button" onClick={() => void syncAccounts()} disabled={isLoadingOauthAccounts}>
                  <KeyRound size={16} />
                  同步当前登录
                </button>
                <button className="primary-button oauth-toolbar-button" onClick={() => void beginOAuthLogin(false)} disabled={oauthLoginSession?.status === "waiting"}>
                  <Users size={16} />
                  添加账号
                </button>
                <button className="secondary-button oauth-toolbar-button" onClick={() => void beginOAuthLogin(true)} disabled={oauthLoginSession?.status === "waiting"}>
                  <KeyRound size={16} />
                  设备码登录
                </button>
              </div>
            </header>

            {oauthLoginSession ? (
              <div className={`banner ${oauthLoginSession.status === "failed" ? "error" : ""}`}>
                <KeyRound size={18} />
                <div>
                  <strong>{oauthLoginSession.message}</strong>
                  {oauthLoginSession.status === "waiting"
                    ? oauthLoginSession.output.map((line) => <div key={line}>{line}</div>)
                    : null}
                </div>
              </div>
            ) : null}

            {oauthAccounts?.accounts?.length ? (
              <div className="oauth-account-list">
                {oauthAccounts.accounts.map((account) => {
                  const primaryWindow = primaryUsageWindow(account);
                  const protectedSingleAccount =
                    oauthAccounts.dispatchMode === "single" && oauthAccounts.singleAccountKey === account.key;
                  const protectedLastMultiAccount =
                    oauthAccounts.dispatchMode === "multi"
                    && account.enabled
                    && oauthAccounts.accounts.filter((item) => item.enabled).length === 1;
                  const cannotDisable = protectedSingleAccount || protectedLastMultiAccount;
                  return (
                  <article className="oauth-account-card" key={account.key}>
                    <div className="oauth-account-head">
                      <div>
                        <h3>{account.alias}</h3>
                        <p>{formatPlanType(account.usage?.planType || "未知套餐")} · {account.source}</p>
                      </div>
                      <span className={`status-badge ${account.status === "available" ? "ok" : "warning"}`}>
                        {account.enabled ? account.status : "已停用"}
                      </span>
                    </div>
                    <div className="oauth-account-metrics">
                      <span><span className="metric-label">并发上限</span><strong>{account.maxConcurrency ?? "不限制"}</strong></span>
                      <span><span className="metric-label">权重</span><strong>{formatDisplayWeight(account.weight)}</strong></span>
                      <span><span className="metric-label">预计占比</span><strong>{Math.round(account.estimatedShare * 100)}%</strong></span>
                      <span><span className="metric-label">5h 剩余</span><strong>{primaryWindow?.remainingPercent ?? "-"}%</strong></span>
                      <span><span className="metric-label">5h 重置</span><strong>{formatResetTime(primaryWindow?.resetAt || 0)}</strong></span>
                    </div>
                    {account.lastError ? <div className="field-error">{account.lastError}</div> : null}
                    <div className="oauth-account-footer">
                      {primaryWindow ? (
                        <div className="oauth-quota-visual">
                          <div className="oauth-quota-visual-head">
                            <span>5h 剩余额度</span>
                            <strong>{primaryWindow.remainingPercent}%</strong>
                          </div>
                          <div
                            className={`oauth-quota-track ${quotaTone(primaryWindow.remainingPercent)}`}
                            role="progressbar"
                            aria-label={`${account.alias} 5h 剩余额度`}
                            aria-valuemin={0}
                            aria-valuemax={100}
                            aria-valuenow={primaryWindow.remainingPercent}
                          >
                            <span style={{ width: `${Math.max(0, Math.min(100, primaryWindow.remainingPercent))}%` }} />
                          </div>
                        </div>
                      ) : null}
                      <div className="oauth-account-actions">
                      <button
                        className="secondary-button"
                        onClick={() => setAccountEditor({
                          mode: "rename",
                          account,
                          alias: account.alias,
                          concurrencyMode: account.maxConcurrency == null ? "unlimited" : "limited",
                          maxConcurrency: account.maxConcurrency?.toString() || "",
                          saving: false,
                          error: ""
                        })}
                      >重命名</button>
                      <button
                        className="secondary-button"
                        onClick={() => setAccountEditor({
                          mode: "concurrency",
                          account,
                          alias: account.alias,
                          concurrencyMode: account.maxConcurrency == null ? "unlimited" : "limited",
                          maxConcurrency: account.maxConcurrency?.toString() || "1",
                          saving: false,
                          error: ""
                        })}
                      >并发设置</button>
                      <button
                        className="secondary-button"
                        disabled={cannotDisable && account.enabled}
                        title={cannotDisable && account.enabled ? "至少需要保留一个启用账户" : undefined}
                        onClick={async () => setOauthAccounts(await updateOAuthAccount(apiKey, account.key, { enabled: !account.enabled }))}
                      >{account.enabled ? "停用" : "启用"}</button>
                      <button className="secondary-button" onClick={() => void beginOAuthLogin(false)}>重新登录</button>
                      <button
                        className="danger-button"
                        disabled={protectedSingleAccount}
                        title={protectedSingleAccount ? "请先选择其他单账户" : undefined}
                        onClick={async () => {
                          if (window.confirm(`确认删除 ${account.alias}？`)) setOauthAccounts(await deleteOAuthAccount(apiKey, account.key));
                        }}
                      >删除</button>
                      </div>
                    </div>
                  </article>
                  );
                })}
              </div>
            ) : !isLoadingOauthAccounts ? (
              <div className="empty-state">
                <Users size={28} />
                <h3>还没有 OAuth 账号</h3>
                <p>先在 Codex CLI/App 登录，然后点击“添加 / 同步当前登录”。</p>
              </div>
            ) : null}
          </section>
        ) : null}

        {activeTab === "account-load" ? (
          <section className="panel" aria-label="账户并发负载">
            <header className="toolbar">
              <div>
                <h2>账户并发负载</h2>
                <p>每秒刷新账号实时请求数和全局等待队列。</p>
              </div>
              <button className="secondary-button" onClick={() => void loadOAuthAccounts()} disabled={isLoadingOauthAccounts}>
                <RefreshCw size={16} />
                刷新
              </button>
            </header>

            {oauthAccounts ? (
              <div className="account-load-page">
                <div className="account-load-summary">
                  <div><span>全局当前并发</span><strong>{oauthAccounts.globalCurrentConcurrency}</strong></div>
                  <div><span>全局并发上限</span><strong>{oauthAccounts.globalMaxConcurrency ?? "不限制"}</strong></div>
                  <div><span>等待队列</span><strong>{oauthAccounts.waitingQueueSize}</strong></div>
                  <div><span>调度模式</span><strong>{oauthAccounts.dispatchMode === "single" ? "单账户" : "多账户"}</strong></div>
                </div>
                <div className="account-load-chart" aria-label="账户实时并发条形图">
                  {oauthAccounts.accounts.map((account) => {
                    const capacity = account.maxConcurrency ?? account.currentConcurrency;
                    const primaryWindow = primaryUsageWindow(account);
                    const capacityPercent = Math.min(100, capacity / oauthLoadScale * 100);
                    const activePercent = capacity > 0
                      ? Math.min(100, account.currentConcurrency / capacity * 100)
                      : 0;
                    return (
                      <article className={`account-load-row ${account.enabled ? "" : "disabled"}`} key={account.key}>
                      <div className="account-load-label">
                        <strong>{account.alias}</strong>
                        <small>
                          {formatPlanType(account.usage?.planType || "未知套餐")} · {account.enabled ? account.status : "已停用"}
                        </small>
                        <small className="account-load-quota">
                          5h 剩余额度：{primaryWindow ? `${primaryWindow.remainingPercent}%` : "-"}
                        </small>
                        <small className="account-load-reset">
                          重置：{primaryWindow ? formatResetTime(primaryWindow.resetAt) : "-"}
                        </small>
                      </div>
                      <div className="account-load-scale">
                        <div
                          className="account-load-capacity"
                          aria-label={`${account.alias} 并发容量`}
                          style={{ width: `${capacityPercent}%` }}
                        >
                          <div className="account-load-track">
                            <span
                              aria-label={`${account.alias} 当前并发`}
                              style={{ width: `${activePercent}%` }}
                            />
                          </div>
                        </div>
                      </div>
                      <strong className="account-load-value">
                        {account.currentConcurrency} / {account.maxConcurrency ?? "不限制"}
                      </strong>
                      </article>
                    );
                  })}
                </div>
              </div>
            ) : !isLoadingOauthAccounts ? (
              <div className="empty-state">
                <Activity size={28} />
                <h3>并发负载未加载</h3>
                <p>刷新后查看账号实时请求数。</p>
              </div>
            ) : null}
          </section>
        ) : null}

        {activeTab === "usage-status" ? (
          <section className="panel" aria-label="额度状态">
            <header className="toolbar">
              <div>
                <h2>额度状态</h2>
                <p>当前 Codex OAuth 账号的 5h 和 weekly 额度窗口。</p>
              </div>
              <button className="secondary-button" onClick={() => void refreshQuotaStatus()} disabled={isLoadingCodexUsage}>
                <RefreshCw size={16} />
                {isLoadingCodexUsage ? "刷新中…" : "刷新"}
              </button>
            </header>

            {codexUsageError ? (
              <div className="banner error">
                <AlertTriangle size={18} />
                {codexUsageError}
              </div>
            ) : null}

            {oauthAccounts?.accounts?.some((account) => account.usage) ? (
              <div className="codex-usage-page">
                <section className="codex-usage-summary">
                  <div><span>启用账号</span><strong>{oauthAccounts.accounts.filter((item) => item.enabled).length}</strong></div>
                  <div><span>可调度账号</span><strong>{oauthAccounts.accounts.filter((item) => item.enabled && item.status === "available").length}</strong></div>
                  <div><span>全局当前并发</span><strong>{oauthAccounts.globalCurrentConcurrency}</strong></div>
                </section>
                {oauthAccounts.accounts.filter((account) => account.usage).map((account) => (
                  <section className="codex-additional-limits" key={account.key}>
                    <div className="oauth-account-head">
                      <div><h3>{account.alias}</h3><p><strong>{formatPlanType(account.usage?.planType || "-")}</strong> · 预计占比 {Math.round(account.estimatedShare * 100)}%</p></div>
                      <span className="status-badge ok">{account.status}</span>
                    </div>
                    {account.lastError ? (
                      <div className="banner error">
                        <AlertTriangle size={18} />
                        {account.lastError}
                      </div>
                    ) : null}
                    {account.usage ? <UsageLimitCard title="Codex 主额度" rateLimit={account.usage.rateLimit} /> : null}
                    {account.usage?.additionalRateLimits.map((item) => (
                      <UsageLimitCard key={item.meteredFeature || item.limitName} title={item.limitName} rateLimit={item.rateLimit} />
                    ))}
                  </section>
                ))}
              </div>
            ) : codexUsage ? (
              <div className="codex-usage-page">
                <section className="codex-usage-summary">
                  <div>
                    <span>账号套餐</span>
                    <strong>{formatPlanType(codexUsage.planType)}</strong>
                  </div>
                  <div>
                    <span>Credits</span>
                    <strong>{codexUsage.credits.unlimited ? "无限" : codexUsage.credits.balance}</strong>
                  </div>
                  <div>
                    <span>状态</span>
                    <strong>{codexUsage.rateLimit.limitReached ? "已触达限制" : "可用"}</strong>
                  </div>
                </section>

                <UsageLimitCard title="Codex 主额度" rateLimit={codexUsage.rateLimit} />

                {codexUsage.additionalRateLimits.length ? (
                  <div className="codex-additional-limits">
                    {codexUsage.additionalRateLimits.map((item) => (
                      <UsageLimitCard key={item.limitName} title={item.limitName} rateLimit={item.rateLimit} />
                    ))}
                  </div>
                ) : null}
              </div>
            ) : !isLoadingCodexUsage ? (
              <div className="empty-state">
                <Gauge size={28} />
                <h3>额度状态未加载</h3>
                <p>进入页面后会自动读取，也可以点击刷新。</p>
              </div>
            ) : null}
          </section>
        ) : null}
      </main>

      {selectedLog ? (
        <div
          className="request-log-drawer-backdrop"
          data-testid="request-log-drawer-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              setSelectedLogId(null);
            }
          }}
        >
          <aside
            aria-labelledby="request-log-drawer-title"
            aria-modal="true"
            className="request-log-drawer"
            role="dialog"
          >
            <header className="request-log-drawer-head">
              <div>
                <span>安全元数据</span>
                <h2 id="request-log-drawer-title">请求详情</h2>
              </div>
              <button
                aria-label="关闭请求详情"
                className="icon-button"
                onClick={() => setSelectedLogId(null)}
                type="button"
              >
                <X size={18} />
              </button>
            </header>
            <div className="request-log-drawer-body">
              <section className="request-detail-section">
                <h3>请求</h3>
                <div className="request-detail-grid">
                  <div><span>完整时间</span><strong>{new Date(selectedLog.timestamp).toLocaleString()}</strong></div>
                  <div><span>请求方法</span><strong>{selectedLog.method}</strong></div>
                  <div><span>接口</span><strong>{selectedLog.path}</strong></div>
                  <div><span>模型</span><strong>{selectedLog.model || "-"}</strong></div>
                  <div>
                    <span>响应模式</span>
                    <strong>
                      {typeof selectedLog.stream === "boolean"
                        ? selectedLog.stream ? "流式" : "非流式"
                        : "未记录"}
                    </strong>
                  </div>
                </div>
              </section>

              <section className="request-detail-section">
                <h3>请求策略</h3>
                <div className="request-detail-grid">
                  <div><span>Effort</span><strong>{selectedLog.reasoning_effort || "未记录"}</strong></div>
                  <div>
                    <span>Fast</span>
                    <strong>
                      {typeof selectedLog.fast_mode === "boolean"
                        ? selectedLog.fast_mode ? "开启" : "关闭"
                        : "未记录"}
                    </strong>
                  </div>
                  <div>
                    <span>Service Tier</span>
                    <strong>
                      {selectedLog.service_tier ||
                        (typeof selectedLog.fast_mode === "boolean" ? "标准" : "未记录")}
                    </strong>
                  </div>
                </div>
              </section>

              <section className="request-detail-section">
                <h3>执行</h3>
                <div className="request-detail-grid">
                  <div><span>状态码</span><strong>{selectedLog.status_code}</strong></div>
                  <div><span>耗时</span><strong>{selectedLog.duration_ms}ms</strong></div>
                  <div><span>账户</span><strong>{selectedLog.account_alias || "未记录"}</strong></div>
                  <div className="request-detail-wide"><span>Request ID</span><strong>{selectedLog.request_id || "-"}</strong></div>
                </div>
              </section>

              <section className="request-detail-section">
                <h3>Tokens</h3>
                <div className="request-token-grid">
                  <div><span>总计</span><strong>{selectedLog.usage?.total ?? "-"}</strong></div>
                  <div><span>输入</span><strong>{selectedLog.usage?.input ?? "-"}</strong></div>
                  <div><span>缓存</span><strong>{selectedLog.usage?.cached ?? "-"}</strong></div>
                  <div><span>输出</span><strong>{selectedLog.usage?.output ?? "-"}</strong></div>
                  <div><span>推理</span><strong>{selectedLog.usage?.reasoning ?? "-"}</strong></div>
                </div>
              </section>

              {selectedLog.error ? (
                <section className="request-detail-section request-detail-error">
                  <h3>错误</h3>
                  <p>{selectedLog.error}</p>
                </section>
              ) : null}
            </div>
          </aside>
        </div>
      ) : null}

      {dispatchEditor && oauthAccounts ? (
        <div
          className="account-modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !dispatchEditor.saving) {
              setDispatchEditor(null);
            }
          }}
        >
          <form
            className="account-modal dispatch-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="dispatch-modal-title"
            onSubmit={(event) => {
              event.preventDefault();
              void saveDispatchEditor();
            }}
          >
            <div className="account-modal-head">
              <div>
                <span className="account-modal-eyebrow">OAuth 请求路由</span>
                <h2 id="dispatch-modal-title">设置账户调度策略</h2>
              </div>
              <button
                className="icon-button"
                type="button"
                aria-label="关闭弹窗"
                onClick={() => setDispatchEditor(null)}
                disabled={dispatchEditor.saving}
              >
                <X size={18} />
              </button>
            </div>

            <div className="account-modal-body">
              <div className="dispatch-mode-options">
                <label className={dispatchEditor.mode === "single" ? "selected" : ""}>
                  <input
                    type="radio"
                    name="dispatch-mode"
                    aria-label="单账户模式"
                    checked={dispatchEditor.mode === "single"}
                    onChange={() => setDispatchEditor({ ...dispatchEditor, mode: "single", error: "" })}
                  />
                  <span><strong>单账户模式</strong><small>所有新请求固定使用一个账户。</small></span>
                </label>
                <label className={dispatchEditor.mode === "multi" ? "selected" : ""}>
                  <input
                    type="radio"
                    name="dispatch-mode"
                    aria-label="多账户模式"
                    checked={dispatchEditor.mode === "multi"}
                    onChange={() => setDispatchEditor({ ...dispatchEditor, mode: "multi", error: "" })}
                  />
                  <span><strong>多账户模式</strong><small>在启用账户之间按额度权重调度。</small></span>
                </label>
              </div>

              {dispatchEditor.mode === "single" ? (
                <label className="account-modal-field">
                  <span>选择唯一账户</span>
                  <select
                    aria-label="单账户选择"
                    value={dispatchEditor.singleAccountKey}
                    onChange={(event) => setDispatchEditor({ ...dispatchEditor, singleAccountKey: event.target.value, error: "" })}
                  >
                    {oauthAccounts.accounts.map((account) => (
                      <option value={account.key} key={account.key}>{account.alias}</option>
                    ))}
                  </select>
                </label>
              ) : (
                <div className="dispatch-account-list">
                  {oauthAccounts.accounts.map((account) => {
                    const checked = dispatchEditor.enabledAccountKeys.includes(account.key);
                    return (
                      <label key={account.key}>
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={(event) => setDispatchEditor({
                            ...dispatchEditor,
                            enabledAccountKeys: event.target.checked
                              ? [...dispatchEditor.enabledAccountKeys, account.key]
                              : dispatchEditor.enabledAccountKeys.filter((key) => key !== account.key),
                            error: ""
                          })}
                        />
                        <span><strong>{account.alias}</strong><small>{formatPlanType(account.usage?.planType || "未知套餐")}</small></span>
                      </label>
                    );
                  })}
                </div>
              )}
              {dispatchEditor.error ? <div className="account-modal-error">{dispatchEditor.error}</div> : null}
            </div>

            <div className="account-modal-actions">
              <button className="secondary-button" type="button" onClick={() => setDispatchEditor(null)} disabled={dispatchEditor.saving}>
                取消
              </button>
              <button className="primary-button" type="submit" disabled={dispatchEditor.saving}>
                {dispatchEditor.saving ? "保存中" : "保存策略"}
              </button>
            </div>
          </form>
        </div>
      ) : null}

      {accountEditor ? (
        <div
          className="account-modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !accountEditor.saving) {
              setAccountEditor(null);
            }
          }}
        >
          <form
            className="account-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="account-modal-title"
            onSubmit={(event) => {
              event.preventDefault();
              void saveAccountEditor();
            }}
            onKeyDown={(event) => {
              if (event.key === "Escape" && !accountEditor.saving) {
                setAccountEditor(null);
              }
            }}
          >
            <div className="account-modal-head">
              <div>
                <span className="account-modal-eyebrow">{accountEditor.account.alias}</span>
                <h2 id="account-modal-title">
                  {accountEditor.mode === "rename" ? "重命名 OAuth 账号" : "设置账号并发"}
                </h2>
              </div>
              <button
                className="icon-button"
                type="button"
                aria-label="关闭弹窗"
                onClick={() => setAccountEditor(null)}
                disabled={accountEditor.saving}
              >
                <X size={18} />
              </button>
            </div>

            <div className="account-modal-body">
              {accountEditor.mode === "rename" ? (
                <label className="account-modal-field">
                  <span>账号别名</span>
                  <input
                    autoFocus
                    aria-label="账号别名"
                    value={accountEditor.alias}
                    onFocus={(event) => event.currentTarget.select()}
                    onChange={(event) => setAccountEditor({ ...accountEditor, alias: event.target.value, error: "" })}
                  />
                  <small>用于控制台展示，不会改变 OAuth 账号身份。</small>
                </label>
              ) : (
                <div className="account-concurrency-options">
                  <label className={accountEditor.concurrencyMode === "unlimited" ? "selected" : ""}>
                    <input
                      type="radio"
                      aria-label="不限制并发"
                      name="concurrency-mode"
                      checked={accountEditor.concurrencyMode === "unlimited"}
                      onChange={() => setAccountEditor({ ...accountEditor, concurrencyMode: "unlimited", error: "" })}
                    />
                    <span><strong>不限制并发</strong><small>账号可按调度权重接收任意数量请求。</small></span>
                  </label>
                  <label className={accountEditor.concurrencyMode === "limited" ? "selected" : ""}>
                    <input
                      type="radio"
                      aria-label="限制并发"
                      name="concurrency-mode"
                      checked={accountEditor.concurrencyMode === "limited"}
                      onChange={() => setAccountEditor({ ...accountEditor, concurrencyMode: "limited", error: "" })}
                    />
                    <span><strong>限制并发</strong><small>达到上限后，请求会等待其他槽位。</small></span>
                  </label>
                  {accountEditor.concurrencyMode === "limited" ? (
                    <label className="account-modal-field compact">
                      <span>最大并发数</span>
                      <input
                        autoFocus
                        aria-label="最大并发数"
                        type="number"
                        min="1"
                        step="1"
                        value={accountEditor.maxConcurrency}
                        onChange={(event) => setAccountEditor({ ...accountEditor, maxConcurrency: event.target.value, error: "" })}
                      />
                    </label>
                  ) : null}
                </div>
              )}
              {accountEditor.error ? <div className="account-modal-error">{accountEditor.error}</div> : null}
            </div>

            <div className="account-modal-actions">
              <button className="secondary-button" type="button" onClick={() => setAccountEditor(null)} disabled={accountEditor.saving}>
                取消
              </button>
              <button className="primary-button" type="submit" disabled={accountEditor.saving}>
                {accountEditor.saving ? "保存中" : "保存修改"}
              </button>
            </div>
          </form>
        </div>
      ) : null}
    </div>
  );
}
