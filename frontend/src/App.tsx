import {
  Activity,
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  Clock3,
  Copy,
  Database,
  KeyRound,
  MessageSquare,
  Moon,
  RefreshCw,
  Save,
  Send,
  Settings,
  Sun,
  Square,
  Zap
} from "lucide-react";
import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  buildAuthHeaders,
  fetchAdminHealth,
  fetchAdminConfig,
  fetchRequestLogs,
  parseChatStreamLine,
  saveAdminConfig
} from "./api";
import { formatCompactNumber, formatDuration, summarizeRequestLogs } from "./dashboard";
import type { AdminConfig, AdminHealth, ChatMessage, ChatUsage, RequestLogItem } from "./types";

// 顶部导航标签的枚举，保持状态值简短稳定。
type ActiveTab = "dashboard" | "chat" | "logs" | "config";

// 控制台主题只提供明确的浅色和深色两种渲染结果。
type ThemeMode = "light" | "dark";

// 主题选择保存在本地浏览器，刷新后保持用户偏好。
const THEME_STORAGE_KEY = "codex-console-theme";

// 看板默认读取最近 200 条请求，在本地工具里兼顾速度和统计可信度。
const DASHBOARD_LOG_LIMIT = 200;

// Token 拆分展示顺序固定，方便用户形成稳定阅读习惯。
const TOKEN_BREAKDOWN_ITEMS = [
  { key: "input", label: "输入" },
  { key: "output", label: "输出" },
  { key: "reasoning", label: "推理" },
  { key: "cached", label: "缓存" }
] as const;

// 配置表单只暴露第一版控制台支持安全编辑的字段。
type ConfigFormState = {
  localApiKey: string;
  localApiKeyTouched: boolean;
  defaultModel: string;
  reasoningEffort: string;
  fastMode: boolean;
  usageEnabled: boolean;
  authPath: string;
  importAuthPath: string;
};

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
    reasoningEffort: config.codex.reasoning_effort,
    fastMode: config.codex.fast_mode,
    usageEnabled: config.usage.enabled,
    authPath: config.auth.auth_path,
    importAuthPath: config.auth.import_auth_path
  };
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
function runtimeStatusView(health: AdminHealth | null, fastMode: boolean, healthError: string) {
  if (healthError) {
    const missingEndpoint = /404|not found/i.test(healthError);
    return {
      tone: "attention",
      summary: missingEndpoint ? "需要更新" : "需要检查",
      hint: missingEndpoint ? "运行状态接口不可用，请重启服务" : "运行状态读取失败，请检查访问密钥",
      details: ["登录：未读取", `速度：${fastMode ? "快速" : "标准"}`, "用量：未读取", "CLI：未读取"]
    };
  }

  if (!health) {
    return {
      tone: "pending",
      summary: "读取中",
      hint: "正在读取运行状态",
      details: ["登录：读取中", `速度：${fastMode ? "快速" : "标准"}`, "用量：读取中", "CLI：读取中"]
    };
  }

  const needsAttention = !health.oauth.available || (health.usage.enabled && !health.usage.writable);
  let usageText = "正常";
  if (!health.usage.enabled) {
    usageText = "已关闭";
  } else if (!health.usage.writable) {
    usageText = "不可写";
  }
  const cliVersion = health.codex.client_version || "未检测到";

  return {
    tone: needsAttention ? "attention" : "ok",
    summary: needsAttention ? "需要检查" : "正常",
    hint: needsAttention ? "打开配置页查看详情" : "OAuth 和日志状态正常",
    details: [
      `登录：${health.oauth.available ? "已检测到" : "未检测到"}`,
      `速度：${fastMode ? "快速" : "标准"}`,
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

// 本地控制台主组件，包含聊天、日志和配置三个工作区。
export function App() {
  const [activeTab, setActiveTab] = useState<ActiveTab>("dashboard");
  const [apiKey, setApiKey] = useState(() => localStorage.getItem("codex-console-api-key") || "");
  const [theme, setTheme] = useState<ThemeMode>(() => initialTheme());
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [model, setModel] = useState("gpt-5.5");
  const [reasoningEffort, setReasoningEffort] = useState("medium");
  const [fastMode, setFastMode] = useState(true);
  const [isStreaming, setIsStreaming] = useState(false);
  const [usage, setUsage] = useState<ChatUsage | null>(null);
  const [status, setStatus] = useState("就绪");
  const [error, setError] = useState("");
  const [logs, setLogs] = useState<RequestLogItem[]>([]);
  const [health, setHealth] = useState<AdminHealth | null>(null);
  const [healthError, setHealthError] = useState("");
  const [config, setConfig] = useState<AdminConfig | null>(null);
  const [configForm, setConfigForm] = useState<ConfigFormState | null>(null);
  const [configSavedNote, setConfigSavedNote] = useState("");
  const [logSearch, setLogSearch] = useState("");
  const [logStatusFilter, setLogStatusFilter] = useState("all");
  const [expandedLogId, setExpandedLogId] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const chatStreamRef = useRef<HTMLDivElement | null>(null);

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

  // 配置加载成功后，同步默认模型和 reasoning effort 到聊天区。
  useEffect(() => {
    void loadConfig();
  }, []);

  // 当前日志统计摘要，驱动看板和日志页顶部信息。
  const dashboardSummary = useMemo(() => summarizeRequestLogs(logs), [logs]);

  // 读取管理配置，并把可编辑字段放进表单。
  const loadConfig = useCallback(async () => {
    try {
      setError("");
      const loaded = await fetchAdminConfig(apiKey);
      setConfig(loaded);
      setConfigForm(formFromConfig(loaded));
      setModel(loaded.codex.default_model);
      setReasoningEffort(loaded.codex.reasoning_effort);
      setFastMode(loaded.codex.fast_mode);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "配置读取失败");
    }
  }, [apiKey]);

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

  // 读取最近请求日志，日志不包含 prompt 和响应正文。
  const loadLogs = useCallback(async () => {
    try {
      setError("");
      const items = await fetchRequestLogs(apiKey, DASHBOARD_LOG_LIMIT);
      setLogs(items);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "请求日志读取失败");
    }
  }, [apiKey]);

  // 看板是默认首页，启动时主动加载最近请求日志。
  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  // 健康状态变化频率低，初始化和访问密钥变化时读取即可。
  useEffect(() => {
    void loadHealth();
  }, [loadHealth]);

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
        void loadLogs();
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
    [apiKey, fastMode, input, isStreaming, loadLogs, messages, model, reasoningEffort]
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
          default_model: configForm.defaultModel,
          reasoning_effort: configForm.reasoningEffort,
          fast_mode: configForm.fastMode
        },
        usage: { enabled: configForm.usageEnabled },
        auth: {
          auth_path: configForm.authPath,
          import_auth_path: configForm.importAuthPath
        }
      });
      setConfigSavedNote(result.restart_required ? "已保存，重启服务后生效" : "已保存，已立即生效");
      setConfigForm({ ...configForm, localApiKey: "", localApiKeyTouched: false });
      void loadConfig();
      void loadHealth();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "配置保存失败");
    }
  }, [apiKey, configForm, loadConfig, loadHealth]);

  // Token 拆分按实际组成计算占比；没有 usage 时保持 0 宽度。
  const tokenBreakdownTotal = TOKEN_BREAKDOWN_ITEMS.reduce(
    (sum, item) => sum + dashboardSummary.tokenBreakdown[item.key],
    0
  );

  // 趋势柱以当前窗口最大 token 为基准，保证小样本也能看出差异。
  const trendMaxTokens = Math.max(...dashboardSummary.trend.map((item) => item.totalTokens), 1);

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

  // 中文运行状态同时驱动左侧汇总和配置页明细，避免两处文案漂移。
  const runtimeStatus = useMemo(
    () => runtimeStatusView(health, fastMode, healthError),
    [fastMode, health, healthError]
  );

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
              void loadLogs();
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
              void loadLogs();
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
            }}
          >
            <Settings size={18} />
            配置
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
          <section className="panel dashboard-panel" aria-label="数据看板">
            <header className="toolbar">
              <div>
                <h2>数据看板</h2>
                <p>
                  最近 {dashboardSummary.requestCount} 条请求，常用模型 {dashboardSummary.topModel}
                </p>
              </div>
              <button className="secondary-button" onClick={() => void loadLogs()}>
                <RefreshCw size={16} />
                刷新
              </button>
            </header>

            <div className="dashboard-content">
              {/* 顶部指标区优先展示用量、稳定性和响应速度。 */}
              <div className="metric-grid" aria-label="使用概览">
                <article className="metric-card">
                  <span className="metric-icon">
                    <Database size={18} />
                  </span>
                  <div>
                    <p>累计 tokens</p>
                    <strong>{formatCompactNumber(dashboardSummary.totalTokens)}</strong>
                  </div>
                </article>
                <article className="metric-card">
                  <span className="metric-icon">
                    <Activity size={18} />
                  </span>
                  <div>
                    <p>请求数</p>
                    <strong>{dashboardSummary.requestCount}</strong>
                  </div>
                </article>
                <article className="metric-card">
                  <span className="metric-icon">
                    <CheckCircle2 size={18} />
                  </span>
                  <div>
                    <p>成功率</p>
                    <strong>{dashboardSummary.successRate}%</strong>
                  </div>
                </article>
                <article className="metric-card">
                  <span className="metric-icon">
                    <Clock3 size={18} />
                  </span>
                  <div>
                    <p>平均耗时</p>
                    <strong>{formatDuration(dashboardSummary.averageDurationMs)}</strong>
                  </div>
                </article>
              </div>

              <div className="dashboard-grid">
                {/* Token 结构用横向条展示，让输入、输出和推理占比一眼可扫。 */}
                <section className="dashboard-section" aria-label="Token 结构">
                  <div className="section-heading">
                    <h3>Token 结构</h3>
                    <span>{formatCompactNumber(tokenBreakdownTotal)} tokens</span>
                  </div>
                  <div className="token-breakdown">
                    {TOKEN_BREAKDOWN_ITEMS.map((item) => {
                      const value = dashboardSummary.tokenBreakdown[item.key];
                      const percent = tokenBreakdownTotal ? Math.round((value / tokenBreakdownTotal) * 100) : 0;
                      return (
                        <div className="token-row" key={item.key}>
                          <div className="token-row-head">
                            <span>{item.label}</span>
                            <strong>{formatCompactNumber(value)}</strong>
                          </div>
                          <div className="token-track" aria-label={`${item.label} ${percent}%`}>
                            <span className={`token-fill ${item.key}`} style={{ width: `${percent}%` }} />
                          </div>
                        </div>
                      );
                    })}
                  </div>
                </section>

                {/* 最近请求趋势只展示 token 高低和失败状态，不暴露请求内容。 */}
                <section className="dashboard-section" aria-label="最近请求趋势">
                  <div className="section-heading">
                    <h3>最近请求</h3>
                    <span>{dashboardSummary.errorCount} 条失败</span>
                  </div>
                  <div className="trend-chart">
                    {dashboardSummary.trend.length ? (
                      dashboardSummary.trend.map((item) => {
                        const height = Math.max((item.totalTokens / trendMaxTokens) * 100, item.totalTokens ? 12 : 4);
                        return (
                          <span
                            className={item.statusCode >= 400 ? "trend-bar failed" : "trend-bar"}
                            key={item.id}
                            style={{ height: `${height}%` }}
                            title={`${new Date(item.timestamp).toLocaleTimeString()} · ${item.totalTokens} tokens`}
                          />
                        );
                      })
                    ) : (
                      <div className="trend-empty">暂无请求</div>
                    )}
                  </div>
                </section>
              </div>

              {/* 健康摘要把错误数、模型和 usage 写入状态放在同一行，便于快速巡检。 */}
              <div className="health-strip" aria-label="运行摘要">
                <span>
                  <strong>{dashboardSummary.successCount}</strong>
                  成功
                </span>
                <span>
                  <strong>{dashboardSummary.errorCount}</strong>
                  失败
                </span>
                <span>
                  <strong>{dashboardSummary.topModel}</strong>
                  模型
                </span>
                <span>
                  <strong>{config ? (config.usage.enabled ? "开启" : "关闭") : "读取中"}</strong>
                  usage 日志
                </span>
              </div>
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
                <select value={model} onChange={(event) => setModel(event.target.value)}>
                  {(config?.codex.available_models || [model]).map((item) => (
                    <option key={item} value={item}>
                      {item}
                    </option>
                  ))}
                </select>
                <select value={reasoningEffort} onChange={(event) => setReasoningEffort(event.target.value)}>
                  <option value="low">low</option>
                  <option value="medium">medium</option>
                  <option value="high">high</option>
                  <option value="xhigh">xhigh</option>
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
                  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
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
                  {dashboardSummary.requestCount} 条记录，{dashboardSummary.successCount} 条成功，
                  {formatCompactNumber(dashboardSummary.totalTokens)} tokens
                </p>
              </div>
              <button className="secondary-button" onClick={() => void loadLogs()}>
                <RefreshCw size={16} />
                刷新
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
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>时间</th>
                    <th>接口</th>
                    <th>模型</th>
                    <th>状态</th>
                    <th>耗时</th>
                    <th>Tokens</th>
                    <th>输入</th>
                    <th>输出</th>
                    <th>推理</th>
                    <th>错误</th>
                  </tr>
	                </thead>
	                <tbody>
	                  {filteredLogs.map((item) => (
	                    <Fragment key={item.id}>
	                      <tr
	                        className="log-row"
	                        onClick={() => setExpandedLogId(expandedLogId === item.id ? null : item.id)}
	                      >
	                        <td>{new Date(item.timestamp).toLocaleTimeString()}</td>
	                        <td>{item.path}</td>
	                        <td>{item.model || "-"}</td>
	                        <td>
	                          <span className={item.status_code < 400 ? "status-ok" : "status-fail"}>
	                            {item.status_code}
	                          </span>
	                        </td>
	                        <td>{item.duration_ms}ms</td>
	                        <td>{item.usage?.total ?? "-"}</td>
	                        <td>{item.usage?.input ?? "-"}</td>
	                        <td>{item.usage?.output ?? "-"}</td>
	                        <td>{item.usage?.reasoning ?? "-"}</td>
	                        <td className="error-cell">{item.error || "-"}</td>
	                      </tr>
	                      {expandedLogId === item.id ? (
	                        <tr className="log-detail-row">
	                          <td colSpan={10}>
	                            <div className="log-detail">
	                              <span>request id</span>
	                              <strong>{item.request_id || "-"}</strong>
	                              <span>错误</span>
	                              <strong>{item.error || "-"}</strong>
	                            </div>
	                          </td>
	                        </tr>
	                      ) : null}
	                    </Fragment>
	                  ))}
	                </tbody>
	              </table>
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
              <button className="secondary-button" onClick={() => void loadConfig()}>
                <RefreshCw size={16} />
                刷新
              </button>
            </header>

            <div className="runtime-detail-panel" aria-label="运行状态详情">
              <div className="runtime-detail-head">
                <h3>运行状态</h3>
                <p>{runtimeStatus.hint}</p>
              </div>
              <div className="runtime-detail-grid">
                {runtimeStatus.details.map((detail) => (
                  <span key={detail}>{detail}</span>
                ))}
              </div>
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
                  <input
                    value={configForm.defaultModel}
                    onChange={(event) => setConfigForm({ ...configForm, defaultModel: event.target.value })}
                  />
                </label>
                <label>
                  <span>Reasoning effort</span>
                  <select
                    value={configForm.reasoningEffort}
                    onChange={(event) => setConfigForm({ ...configForm, reasoningEffort: event.target.value })}
                  >
                    <option value="low">low</option>
                    <option value="medium">medium</option>
                    <option value="high">high</option>
                    <option value="xhigh">xhigh</option>
                  </select>
                </label>
                <label className="toggle-line">
                  <input
                    type="checkbox"
                    checked={configForm.fastMode}
                    onChange={(event) => setConfigForm({ ...configForm, fastMode: event.target.checked })}
                  />
                  <span>默认快速模式</span>
                </label>
                <label>
                  <span>Auth path</span>
                  <input
                    value={configForm.authPath}
                    onChange={(event) => setConfigForm({ ...configForm, authPath: event.target.value })}
                  />
                </label>
                <label>
                  <span>Import auth path</span>
                  <input
                    value={configForm.importAuthPath}
                    onChange={(event) => setConfigForm({ ...configForm, importAuthPath: event.target.value })}
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
      </main>
    </div>
  );
}
