import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";
import { summarizeRequestLogs } from "./dashboard";

// 管理接口的最小配置响应，保证 App 初始化时不会访问真实后端。
const adminConfig = {
  server: { host: "127.0.0.1", port: 1219 },
  api: { local_api_key_configured: false },
  codex: {
    default_model: "gpt-5.5",
    available_models: ["gpt-5.5"],
    reasoning_effort: "medium",
    timeout_seconds: 120,
    include_reasoning: true,
    fast_mode: true,
    model_request_defaults: {
      "gpt-5.5": { reasoning_effort: "medium", fast_mode: true }
    },
    uses_legacy_request_defaults: false
  },
  usage: { enabled: true, path: ".codex-usage/usage.jsonl" },
  auth: { import_auth_path: "~/.codex/auth.json" },
  config_path: "config.yaml"
};

// 管理台 health 响应用于驱动左侧中文服务状态和配置页运行详情。
const adminHealth = {
  server: {
    api: "http://127.0.0.1:1219/v1",
    console: "http://127.0.0.1:1219/ui"
  },
  oauth: { available: true, expired: false },
  usage: { enabled: true, writable: true, path: ".codex-usage/usage.jsonl" },
  ui: { built: true },
  codex: { client_version: "0.136.0" }
};

// 模型目录测试数据模拟 Codex CLI 返回的动态模型和各自 effort 支持范围。
const adminModelCatalog = {
  models: [
    {
      id: "gpt-5.5",
      display_name: "gpt-5.5",
      default_reasoning_effort: "medium",
      supported_reasoning_efforts: ["medium", "high"],
      source: "cli"
    },
    {
      id: "gpt-5.6-mini",
      display_name: "gpt-5.6-mini",
      default_reasoning_effort: "low",
      supported_reasoning_efforts: ["low"],
      source: "cli"
    }
  ],
  effective_default_model: "gpt-5.5",
  cache_state: "fresh",
  source: "cli"
};

// 额度状态测试数据模拟 Codex usage backend 返回的脱敏摘要。
const codexUsageStatus = {
  planType: "pro",
  rateLimit: {
    allowed: true,
    limitReached: false,
    windows: [
      {
        label: "5h",
        kind: "primary",
        usedPercent: 33,
        remainingPercent: 67,
        limitWindowSeconds: 18000,
        resetAfterSeconds: 1200,
        resetAt: 1783814968
      },
      {
        label: "Weekly",
        kind: "secondary",
        usedPercent: 21,
        remainingPercent: 79,
        limitWindowSeconds: 604800,
        resetAfterSeconds: 580000,
        resetAt: 1784370805
      }
    ]
  },
  additionalRateLimits: [
    {
      limitName: "GPT-5.3-Codex-Spark",
      meteredFeature: "codex_spark",
      rateLimit: {
        allowed: true,
        limitReached: false,
        windows: [
          {
            label: "5h",
            kind: "primary",
            usedPercent: 0,
            remainingPercent: 100,
            limitWindowSeconds: 18000,
            resetAfterSeconds: 18000,
            resetAt: 1783817589
          }
        ]
      }
    }
  ],
  credits: { hasCredits: false, unlimited: false, overageLimitReached: false, balance: "0" }
};

// OAuth 账号页测试数据覆盖权重、主额度重置时间和账号编辑操作。
const oauthAccountsStatus = {
  accounts: [
    {
      key: "account-a",
      alias: "账号 A",
      enabled: true,
      source: "codex-cli",
      maxConcurrency: null,
      currentConcurrency: 2,
      status: "available",
      lastError: null,
      usage: codexUsageStatus,
      weight: 0.00338,
      estimatedShare: 1,
      nextRefreshAt: 1783815000
    }
  ],
  dispatchMode: "multi",
  singleAccountKey: null,
  globalCurrentConcurrency: 2,
  globalMaxConcurrency: null,
  waitingQueueSize: 3
};

// 看板测试用的请求日志，模拟后端 /admin/requests 返回值。
const requestLogItems = [
  {
    id: "req_dashboard_2",
    timestamp: "2026-06-16T08:02:00.000Z",
    method: "POST",
    path: "/v1/chat/completions",
    model: "gpt-5.5",
    status_code: 200,
    duration_ms: 2000,
    usage: { total: 40, input: 20, cached: 5, output: 15, reasoning: 4 },
    request_id: "resp_2",
    error: null,
    stream: true,
    reasoning_effort: "high",
    fast_mode: true,
    service_tier: "priority"
  },
  {
    id: "req_dashboard_1",
    timestamp: "2026-06-16T08:01:00.000Z",
    method: "POST",
    path: "/v1/responses",
    model: "gpt-5.5",
    status_code: 200,
    duration_ms: 1000,
    usage: { total: 20, input: 12, cached: 2, output: 8, reasoning: 1 },
    request_id: "resp_1",
    error: null,
    stream: false,
    reasoning_effort: "medium",
    fast_mode: false,
    service_tier: null
  }
];

describe("App theme mode", () => {
  let capturedRequests: Array<{ url: string; method: string; body?: unknown }> = [];
  let healthEndpointAvailable = true;
  let modelEndpointAvailable = true;
  let healthResponse = adminHealth;
  let modelCatalogResponse = adminModelCatalog;
  let requestLogsResponse = requestLogItems;
  let oauthLoginStartResponse: Record<string, unknown> | null = null;
  let oauthAccountsResponse = oauthAccountsStatus;
  let oauthAccountsRefreshResponse = oauthAccountsStatus;

  beforeEach(() => {
    capturedRequests = [];
    healthEndpointAvailable = true;
    modelEndpointAvailable = true;
    healthResponse = { ...adminHealth, oauth: { ...adminHealth.oauth } };
    modelCatalogResponse = adminModelCatalog;
    requestLogsResponse = requestLogItems;
    oauthLoginStartResponse = null;
    oauthAccountsResponse = oauthAccountsStatus;
    oauthAccountsRefreshResponse = oauthAccountsStatus;

    // 用内存版 localStorage 规避 jsdom 在当前环境里的存储实现差异。
    const memoryStorage = new Map<string, string>();
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => memoryStorage.get(key) ?? null,
      setItem: (key: string, value: string) => memoryStorage.set(key, value),
      removeItem: (key: string) => memoryStorage.delete(key),
      clear: () => memoryStorage.clear()
    });

    // 每个用例都从干净主题开始，避免状态互相污染。
    document.documentElement.removeAttribute("data-theme");

    // 模拟系统偏好为浅色，确保默认主题判断稳定。
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        addListener: vi.fn(),
        removeListener: vi.fn(),
        dispatchEvent: vi.fn()
      })
    });

    // App 首次渲染会读取管理接口，这里按路径返回 fake 数据。
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input);
        const method = init?.method || "GET";
        const capturedRequest: { url: string; method: string; body?: unknown } = { url, method };
        if (init?.body) {
          // 记录前端发出的 JSON body，便于断言聊天和配置保存参数。
          capturedRequest.body = JSON.parse(String(init.body));
        }
        capturedRequests.push(capturedRequest);
        if (url.startsWith("/admin/dashboard")) {
          const params = new URL(url, "http://test").searchParams;
          return new Response(
            JSON.stringify(
              summarizeRequestLogs(requestLogItems, {
                preset: (params.get("range") || "today") as "today" | "week" | "month" | "all" | "recent",
                recentDays: Number(params.get("recent_days") || 7),
                now: new Date("2026-06-16T12:00:00Z")
              })
            ),
            { status: 200 }
          );
        }
        if (url.startsWith("/admin/requests")) {
          const limit = new URL(url, "http://test").searchParams.get("limit") || "1000";
          const items = limit === "all" ? requestLogsResponse : requestLogsResponse.slice(0, Number(limit));
          return new Response(JSON.stringify({ items }), { status: 200 });
        }
        if (url === "/admin/health") {
          if (!healthEndpointAvailable) {
            return new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 });
          }
          return new Response(JSON.stringify(healthResponse), { status: 200 });
        }
        if (url.startsWith("/admin/models")) {
          if (!modelEndpointAvailable) {
            return new Response(JSON.stringify({ detail: "catalog unavailable" }), { status: 503 });
          }
          return new Response(JSON.stringify(modelCatalogResponse), { status: 200 });
        }
        if (url === "/admin/codex/usage") {
          return new Response(JSON.stringify(codexUsageStatus), { status: 200 });
        }
        if (url === "/admin/oauth/accounts") {
          return new Response(JSON.stringify(oauthAccountsResponse), { status: 200 });
        }
        if (url === "/admin/oauth/accounts/refresh" && method === "POST") {
          return new Response(JSON.stringify(oauthAccountsRefreshResponse), { status: 200 });
        }
        if (url === "/admin/oauth/accounts/account-a" && method === "PATCH") {
          return new Response(JSON.stringify(oauthAccountsResponse), { status: 200 });
        }
        if (url === "/admin/oauth/dispatch" && method === "PUT") {
          return new Response(JSON.stringify({ ...oauthAccountsStatus, dispatchMode: "single", singleAccountKey: "account-a" }), {
            status: 200
          });
        }
        if (url === "/admin/oauth/login" && method === "POST" && oauthLoginStartResponse) {
          return new Response(JSON.stringify(oauthLoginStartResponse), { status: 200 });
        }
        if (url === "/v1/chat/completions") {
          const streamBody =
            'data: {"choices":[{"delta":{"content":"```python\\nprint(1)\\n```"}}],"usage":null}\n\n' +
            "data: [DONE]\n\n";
          return new Response(streamBody, { status: 200, headers: { "Content-Type": "text/event-stream" } });
        }
        if (url === "/admin/config" && method === "PATCH") {
          return new Response(JSON.stringify({ restart_required: true }), { status: 200 });
        }
        if (url === "/admin/auth/reload" && method === "POST") {
          return new Response(JSON.stringify({ oauth: { available: true, expired: false, reloaded: true } }), {
            status: 200
          });
        }
        return new Response(JSON.stringify(adminConfig), { status: 200 });
      })
    );
  });

  afterEach(() => {
    // 清理 DOM 和 Vitest stub，保证后续测试不继承主题或 fetch。
    cleanup();
    vi.unstubAllGlobals();
  });

  it("toggles between light and dark mode and persists the choice", async () => {
    // 渲染后应按系统浅色偏好设置根节点主题。
    render(<App />);
    expect(document.documentElement.dataset.theme).toBe("light");

    // 点击主题按钮后应切换到深色并写入 localStorage。
    fireEvent.click(await screen.findByRole("button", { name: "切换到深色模式" }));
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("codex-console-theme")).toBe("dark");

    // 切换后按钮的无障碍名称也应反映下一步动作。
    expect(screen.getByRole("button", { name: "切换到浅色模式" })).toBeTruthy();
  });

  it("shows dashboard metrics from recent request logs", async () => {
    // 默认首页应是 API Service 用量中心，切到全部范围后展示历史请求概览。
    render(<App />);

    expect(await screen.findByRole("heading", { name: "Codex API Service Usage" })).toBeTruthy();
    expect(await screen.findByText(window.location.host)).toBeTruthy();
    expect(await screen.findByText("服务状态")).toBeTruthy();
    expect((await screen.findAllByText("正常")).length).toBeGreaterThan(0);
    expect(screen.queryByText("OAuth ready")).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "全部" }));
    expect(await screen.findByText("总 tokens")).toBeTruthy();
    expect((await screen.findAllByText("60")).length).toBeGreaterThan(0);
    expect(await screen.findByText("100%")).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "时间分布" })).toBeTruthy();
    expect(await screen.findByRole("heading", { name: "模型分布" })).toBeTruthy();
    expect(await screen.findByLabelText("06/16 · 60 tokens · 输入 32 · 输出 23 · 2 次成功请求")).toBeTruthy();
    expect(await screen.findByText("总 60 tokens")).toBeTruthy();
    await waitFor(() => {
      expect(capturedRequests.some((request) => request.url === "/admin/dashboard?range=all&recent_days=7")).toBe(true);
    });
    expect(capturedRequests.some((request) => request.url.startsWith("/admin/requests"))).toBe(false);
  });

  it("moves detailed runtime status to the config page", async () => {
    // 左侧只保留中文汇总，完整运行状态集中放到配置页便于理解。
    render(<App />);
    expect(await screen.findByText("服务状态")).toBeTruthy();
    expect(screen.queryByText("OAuth ready")).toBeNull();

    fireEvent.click(await screen.findByRole("button", { name: "配置" }));
    expect(await screen.findByRole("heading", { name: "运行状态" })).toBeTruthy();
    expect(await screen.findByText("登录：已检测到")).toBeTruthy();
    expect(await screen.findByText("请求：按模型配置")).toBeTruthy();
    expect(await screen.findByText("用量：正常")).toBeTruthy();
    expect(await screen.findByText("CLI：0.136.0")).toBeTruthy();
  });

  it("shows attention when the OAuth token file is expired", async () => {
    // token 文件存在但已过期时，状态区不能继续展示为正常。
    healthResponse = { ...adminHealth, oauth: { available: true, expired: true } };
    render(<App />);

    expect(await screen.findByText("需要检查")).toBeTruthy();
    fireEvent.click(await screen.findByRole("button", { name: "配置" }));
    expect(await screen.findByText("登录：已过期")).toBeTruthy();
  });

  it("syncs the latest Codex login from the config page", async () => {
    // Web 控制台应提供手动同步入口，用于本服务缓存旧 OAuth token 的场景。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "配置" }));
    fireEvent.click(await screen.findByRole("button", { name: "同步 Codex 登录" }));

    await waitFor(() => {
      expect(
        capturedRequests.some((request) => request.url === "/admin/auth/reload" && request.method === "POST")
      ).toBe(true);
    });
    expect(await screen.findByText("已同步 Codex 登录")).toBeTruthy();
  });

  it("shows restart guidance when the runtime health endpoint is unavailable", async () => {
    // 旧后端没有 /admin/health 时，应明确提示重启服务，而不是一直显示读取中。
    healthEndpointAvailable = false;
    render(<App />);

    expect(await screen.findByText("需要更新")).toBeTruthy();
    expect(await screen.findByText("运行状态接口不可用，请重启服务")).toBeTruthy();

    fireEvent.click(await screen.findByRole("button", { name: "配置" }));
    expect(await screen.findByText("登录：未读取")).toBeTruthy();
    expect(await screen.findByText("用量：未读取")).toBeTruthy();
    expect(await screen.findByText("CLI：未读取")).toBeTruthy();
  });

  it("renders streamed markdown code blocks with a copy action", async () => {
    // 聊天输出包含代码块时，应渲染为 code/pre，而不是普通段落文本。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "聊天" }));
    fireEvent.change(screen.getByPlaceholderText("输入消息..."), { target: { value: "code please" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    expect(await screen.findByText("print(1)", { selector: "code" })).toBeTruthy();
    expect(await screen.findByRole("button", { name: "复制代码" })).toBeTruthy();
  });

  it("sends the selected fast mode with chat requests", async () => {
    // 聊天页的 fast 开关默认来自管理配置，用户可以临时关闭本次请求。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "聊天" }));
    const fastToggle = (await screen.findByRole("checkbox", { name: "快速模式" })) as HTMLInputElement;
    expect(fastToggle.checked).toBe(true);

    // 关闭 fast 后发送消息，fetch body 中应包含 fast_mode=false。
    fireEvent.click(fastToggle);
    fireEvent.change(screen.getByPlaceholderText("输入消息..."), { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "发送" }));

    await waitFor(() => {
      const chatRequest = capturedRequests.find((request) => request.url === "/v1/chat/completions");
      expect(chatRequest?.body).toMatchObject({ fast_mode: false });
    });
  });

  it("sends a chat message with Enter", async () => {
    // 普通 Enter 应直接发送当前消息。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "聊天" }));
    const composer = screen.getByPlaceholderText("输入消息...");
    fireEvent.change(composer, { target: { value: "enter message" } });
    fireEvent.keyDown(composer, { key: "Enter" });

    await waitFor(() => {
      expect(capturedRequests.some((request) => request.url === "/v1/chat/completions")).toBe(true);
    });
  });

  it("keeps Shift+Enter for multiline chat input", async () => {
    // Shift+Enter 只用于换行，不能触发聊天请求。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "聊天" }));
    const composer = screen.getByPlaceholderText("输入消息...");
    fireEvent.change(composer, { target: { value: "multiline" } });
    fireEvent.keyDown(composer, { key: "Enter", shiftKey: true });

    expect(capturedRequests.some((request) => request.url === "/v1/chat/completions")).toBe(false);
  });

  it("does not send while an input method is composing", async () => {
    // 中文等输入法确认候选词时，Enter 不能误触发发送。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "聊天" }));
    const composer = screen.getByPlaceholderText("输入消息...");
    fireEvent.change(composer, { target: { value: "输入中" } });
    fireEvent.keyDown(composer, { key: "Enter", isComposing: true });

    expect(capturedRequests.some((request) => request.url === "/v1/chat/completions")).toBe(false);
  });

  it("saves model defaults from the independent model config page", async () => {
    // 独立模型配置页保存完整非默认覆盖映射。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "模型配置" }));
    const defaultFastToggle = (await screen.findByRole("checkbox", { name: "gpt-5.5 快速模式" })) as HTMLInputElement;
    expect(defaultFastToggle.checked).toBe(true);

    // 恢复固定默认后保存，映射中不应保留该模型。
    fireEvent.click(defaultFastToggle);
    fireEvent.click(screen.getByRole("button", { name: "保存模型配置" }));

    await waitFor(() => {
      const patchRequest = capturedRequests.find(
        (request) => request.url === "/admin/config" && request.method === "PATCH"
      );
      expect(patchRequest?.body).toEqual({ codex: { model_request_defaults: {} } });
      expect(patchRequest?.body).not.toHaveProperty("api");
    });
  });

  it("uses the dynamic model catalog for independent model effort choices", async () => {
    // 模型配置页为每个目录模型展示独立 effort 选项。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "模型配置" }));

    const effortSelect = (await screen.findByLabelText("gpt-5.5 Effort")) as HTMLSelectElement;
    expect([...effortSelect.options].map((option) => option.value)).toEqual(["medium", "high"]);
    expect(await screen.findByLabelText("gpt-5.6-mini Effort")).toBeTruthy();
  });

  it("shows Codex usage status in an independent tab under model config", async () => {
    // 额度状态是模型配置下方的独立导航页，不混进模型配置表单。
    render(<App />);

    const navButtons = await screen.findAllByRole("button");
    const modelConfigIndex = navButtons.findIndex((button) => button.textContent === "模型配置");
    const usageStatusIndex = navButtons.findIndex((button) => button.textContent === "额度状态");
    expect(modelConfigIndex).toBeGreaterThan(-1);
    expect(usageStatusIndex).toBe(modelConfigIndex + 1);

    fireEvent.click(screen.getByRole("button", { name: "额度状态" }));

    expect(await screen.findByRole("heading", { name: "额度状态" })).toBeTruthy();
    expect(await screen.findByText("Pro")).toBeTruthy();
    expect((await screen.findAllByText("5h 剩余")).length).toBeGreaterThan(0);
    expect(await screen.findByText("67%")).toBeTruthy();
    expect((await screen.findAllByText("Weekly 剩余")).length).toBeGreaterThan(0);
    expect(await screen.findByText("79%")).toBeTruthy();
    expect(await screen.findByText("GPT-5.3-Codex-Spark")).toBeTruthy();
    await waitFor(() => {
      expect(capturedRequests.some((request) => request.url === "/admin/codex/usage")).toBe(true);
    });
  });

  it("refreshes the visible multi-account quota snapshot", async () => {
    // 刷新按钮应替换账号卡片使用的 oauthAccounts，而不是只更新隐藏的单账号兼容数据。
    oauthAccountsRefreshResponse = {
      ...oauthAccountsStatus,
      accounts: oauthAccountsStatus.accounts.map((account) => ({
        ...account,
        usage: {
          ...codexUsageStatus,
          rateLimit: {
            ...codexUsageStatus.rateLimit,
            windows: codexUsageStatus.rateLimit.windows.map((window, index) =>
              index === 0 ? { ...window, usedPercent: 58, remainingPercent: 42 } : window
            )
          }
        }
      }))
    };
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "额度状态" }));
    expect(await screen.findByText("67%")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "刷新" }));

    expect(await screen.findByText("42%")).toBeTruthy();
    expect(
      capturedRequests.some(
        (request) => request.url === "/admin/oauth/accounts/refresh" && request.method === "POST"
      )
    ).toBe(true);
  });

  it("shows an account error when a quota refresh partially fails", async () => {
    // 后端按部分成功返回快照时，失败账号应保留旧额度并明确展示错误。
    oauthAccountsRefreshResponse = {
      ...oauthAccountsStatus,
      accounts: oauthAccountsStatus.accounts.map((account) => ({
        ...account,
        lastError: "额度读取失败"
      }))
    };
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "额度状态" }));
    await screen.findByText("67%");

    fireEvent.click(screen.getByRole("button", { name: "刷新" }));

    expect(await screen.findByText("额度读取失败")).toBeTruthy();
    expect(await screen.findByText("67%")).toBeTruthy();
  });

  it("shows scaled decimal weight and the five-hour reset time", async () => {
    // 账号卡片应该使用易读权重，并直接展示主额度重置时间。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "OAuth 账号" }));

    expect(await screen.findByText("3.38")).toBeTruthy();
    expect(screen.queryByText("3.38e-3")).toBeNull();
    expect(await screen.findByText("5h 重置")).toBeTruthy();
    expect(await screen.findByText(new Date(1783814968 * 1000).toLocaleString())).toBeTruthy();
  });

  it("separates current concurrency from the unlimited limit", async () => {
    // OAuth 账号页只保留配置上限，实时占用迁移到独立负载页。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "OAuth 账号" }));

    expect(await screen.findByText("并发上限")).toBeTruthy();
    expect(await screen.findByText("不限制")).toBeTruthy();
    expect(screen.queryByText("当前并发")).toBeNull();
  });

  it("configures a single-account dispatch policy in a themed dialog", async () => {
    // 配置页通过独立弹窗保存调度模式，不混入普通 YAML 保存按钮。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "配置" }));
    fireEvent.click(await screen.findByRole("button", { name: "设置账户调度策略" }));

    expect(await screen.findByRole("dialog", { name: "设置账户调度策略" })).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "单账户模式" }));
    fireEvent.change(screen.getByRole("combobox", { name: "单账户选择" }), { target: { value: "account-a" } });
    fireEvent.click(screen.getByRole("button", { name: "保存策略" }));

    await waitFor(() => {
      expect(capturedRequests).toContainEqual({
        url: "/admin/oauth/dispatch",
        method: "PUT",
        body: { mode: "single", singleAccountKey: "account-a" }
      });
    });
  });

  it("shows account concurrency load below OAuth accounts and refreshes every second", async () => {
    // 负载页独立展示当前并发和等待队列，并只在页面可见时持续刷新。
    render(<App />);
    const navButtons = await screen.findAllByRole("button");
    const oauthIndex = navButtons.findIndex((button) => button.textContent === "OAuth 账号");
    const loadIndex = navButtons.findIndex((button) => button.textContent === "账户并发负载");
    expect(loadIndex).toBe(oauthIndex + 1);

    fireEvent.click(screen.getByRole("button", { name: "账户并发负载" }));
    expect(await screen.findByRole("heading", { name: "账户并发负载" })).toBeTruthy();
    expect(await screen.findByText("等待队列")).toBeTruthy();
    expect(await screen.findByText("3", { selector: "strong" })).toBeTruthy();
    expect(await screen.findByText("账号 A")).toBeTruthy();
    expect(await screen.findByText("2 / 不限制")).toBeTruthy();
    expect(await screen.findByText("5h 剩余额度：67%")).toBeTruthy();
    expect(await screen.findByText(`重置：${new Date(1783814968 * 1000).toLocaleString()}`)).toBeTruthy();

    const initialLoads = capturedRequests.filter((request) => request.url === "/admin/oauth/accounts").length;
    await new Promise((resolve) => window.setTimeout(resolve, 1100));
    await waitFor(() => {
      expect(capturedRequests.filter((request) => request.url === "/admin/oauth/accounts").length).toBeGreaterThan(initialLoads);
    });
  });

  it("shows an accessible five-hour quota bar on OAuth account cards", async () => {
    // OAuth 账号卡片用进度条直观展示主额度剩余比例。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "OAuth 账号" }));

    const progress = await screen.findByRole("progressbar", { name: "账号 A 5h 剩余额度" });
    expect(progress.getAttribute("aria-valuenow")).toBe("67");
    expect((progress.firstElementChild as HTMLElement).style.width).toBe("67%");
    const footer = progress.closest(".oauth-account-footer");
    expect(footer).toBeTruthy();
    expect(footer?.querySelector(".oauth-account-actions")).toBeTruthy();
  });

  it("scales each capacity track by account limit or observed unlimited load", async () => {
    // 统一刻度为 5：不限账户当前 5 占满，上限 2 的账户轨道只占 40%。
    const baseAccount = oauthAccountsStatus.accounts[0];
    oauthAccountsResponse = {
      ...oauthAccountsStatus,
      globalCurrentConcurrency: 5,
      accounts: [
        { ...baseAccount, alias: "不限账户", currentConcurrency: 5, maxConcurrency: null },
        { ...baseAccount, key: "account-b", alias: "有限账户", currentConcurrency: 0, maxConcurrency: 2 }
      ]
    };
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "账户并发负载" }));

    const unlimitedCapacity = await screen.findByLabelText("不限账户 并发容量");
    const unlimitedActive = await screen.findByLabelText("不限账户 当前并发");
    const limitedCapacity = await screen.findByLabelText("有限账户 并发容量");
    const limitedActive = await screen.findByLabelText("有限账户 当前并发");

    expect(unlimitedCapacity.style.width).toBe("100%");
    expect(unlimitedActive.style.width).toBe("100%");
    expect(limitedCapacity.style.width).toBe("40%");
    expect(limitedActive.style.width).toBe("0%");
  });

  it("removes the legacy auth path field from config", async () => {
    // 旧单账号认证路径已停止兼容，配置页不应继续展示该输入框。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "配置" }));

    expect(screen.queryByText("旧单账号 Auth path（仅兼容迁移）")).toBeNull();
  });

  it("hides raw login output after success and dismisses the notice", async () => {
    // 成功提示只短暂说明结果，不能继续铺开 CLI 原始日志。
    oauthLoginStartResponse = {
      id: "login-success",
      deviceAuth: false,
      status: "success",
      message: "OAuth 账号添加成功",
      output: ["Successfully logged in", "https://auth.openai.com/private"],
      accountKey: "account-a"
    };
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "OAuth 账号" }));
    fireEvent.click(await screen.findByRole("button", { name: "添加账号" }));

    expect(await screen.findByText("OAuth 账号添加成功")).toBeTruthy();
    expect(screen.queryByText("Successfully logged in")).toBeNull();
    await waitFor(() => expect(screen.queryByText("OAuth 账号添加成功")).toBeNull(), { timeout: 4000 });
  });

  it("edits account alias in a themed dialog", async () => {
    // 重命名应使用页面内对话框，并提交新的账号别名。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "OAuth 账号" }));
    fireEvent.click(await screen.findByRole("button", { name: "重命名" }));

    const dialog = await screen.findByRole("dialog", { name: "重命名 OAuth 账号" });
    const input = screen.getByRole("textbox", { name: "账号别名" });
    fireEvent.change(input, { target: { value: "主力账号" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    expect(dialog).toBeTruthy();
    await waitFor(() => {
      expect(capturedRequests).toContainEqual({
        url: "/admin/oauth/accounts/account-a",
        method: "PATCH",
        body: { alias: "主力账号" }
      });
    });
  });

  it("sets account concurrency with an explicit unlimited choice", async () => {
    // 并发设置弹窗应该用明确模式代替留空约定。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "OAuth 账号" }));
    fireEvent.click(await screen.findByRole("button", { name: "并发设置" }));

    expect(await screen.findByRole("dialog", { name: "设置账号并发" })).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: "限制并发" }));
    fireEvent.change(screen.getByRole("spinbutton", { name: "最大并发数" }), { target: { value: "8" } });
    fireEvent.click(screen.getByRole("button", { name: "保存修改" }));

    await waitFor(() => {
      expect(capturedRequests).toContainEqual({
        url: "/admin/oauth/accounts/account-a",
        method: "PATCH",
        body: { maxConcurrency: 8 }
      });
    });
  });

  it("keeps only default model on the ordinary config page", async () => {
    // 服务配置页不再展示全局 effort 和快速模式。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "配置" }));

    expect(await screen.findByLabelText("默认模型")).toBeTruthy();
    expect(screen.queryByText("Reasoning effort")).toBeNull();
    expect(screen.queryByText("默认快速模式")).toBeNull();
  });

  it("refreshes and saves with the model catalog lifecycle", async () => {
    // 手动刷新应强刷 CLI；保存后只重新读取非强制目录，避免无意义地连点 CLI。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "配置" }));
    fireEvent.click(await screen.findByRole("button", { name: "刷新" }));

    await waitFor(() => {
      expect(capturedRequests.some((request) => request.url === "/admin/models?refresh=true")).toBe(true);
    });

    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(capturedRequests.some((request) => request.url === "/admin/config" && request.method === "PATCH")).toBe(
        true
      );
    });
    const patchIndex = capturedRequests.findIndex(
      (request) => request.url === "/admin/config" && request.method === "PATCH"
    );
    const afterPatch = capturedRequests.slice(patchIndex + 1).map((request) => request.url);
    expect(afterPatch).toContain("/admin/models");
    expect(afterPatch).not.toContain("/admin/models?refresh=true");
  });

  it("falls back to config models when the model catalog cannot be loaded", async () => {
    // 目录接口失败时仍应保留配置里的模型，避免配置页无法保存当前值。
    modelEndpointAvailable = false;
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "配置" }));

    expect(await screen.findByText("模型目录读取失败：catalog unavailable")).toBeTruthy();
    const modelSelect = (await screen.findByLabelText("默认模型")) as HTMLSelectElement;
    expect([...modelSelect.options].map((option) => option.value)).toEqual(["gpt-5.5"]);
  });

  it("filters request logs and opens a safe metadata drawer", async () => {
    // 日志页应支持按文本过滤，点击后通过右侧抽屉展示安全元数据。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "请求日志" }));
    await waitFor(() => {
      expect(capturedRequests.some((request) => request.url === "/admin/requests?limit=1000")).toBe(true);
    });
    fireEvent.change(await screen.findByPlaceholderText("搜索日志"), { target: { value: "/v1/responses" } });

    expect(await screen.findByText("/v1/responses")).toBeTruthy();
    expect(screen.queryByText("/v1/chat/completions")).toBeNull();

    fireEvent.click(screen.getByText("/v1/responses"));
    const dialog = await screen.findByRole("dialog", { name: "请求详情" });
    expect(dialog).toBeTruthy();
    expect(await screen.findByText("Request ID")).toBeTruthy();
    expect(await screen.findByText("resp_1")).toBeTruthy();
    expect(await screen.findByText("非流式")).toBeTruthy();
    expect(await screen.findByText("medium")).toBeTruthy();
    expect(await screen.findByText("关闭")).toBeTruthy();
    expect(await screen.findByText("标准")).toBeTruthy();
    expect(screen.queryByText("request id")).toBeNull();

    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "请求详情" })).toBeNull());
  });

  it("closes the request detail drawer from the close button and backdrop", async () => {
    // 关闭按钮和遮罩都应清除当前选择，避免抽屉残留在其他日志筛选结果上。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "请求日志" }));
    fireEvent.click(await screen.findByText("/v1/responses"));
    await screen.findByRole("dialog", { name: "请求详情" });

    fireEvent.click(screen.getByRole("button", { name: "关闭请求详情" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "请求详情" })).toBeNull());

    fireEvent.click(screen.getByText("/v1/responses"));
    await screen.findByRole("dialog", { name: "请求详情" });
    fireEvent.mouseDown(screen.getByTestId("request-log-drawer-backdrop"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "请求详情" })).toBeNull());
  });

  it("selects the request log count and virtualizes large results", async () => {
    // 即使选择全部，页面也只创建视口附近的日志行。
    requestLogsResponse = Array.from({ length: 3000 }, (_, index) => ({
      ...requestLogItems[index % requestLogItems.length],
      id: `large_${index}`,
      request_id: `resp_large_${index}`
    }));
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "请求日志" }));

    const limitSelect = (await screen.findByLabelText("日志加载数量")) as HTMLSelectElement;
    expect(limitSelect.value).toBe("1000");
    await waitFor(() => {
      expect(screen.getAllByRole("row").length).toBeLessThan(50);
    });

    fireEvent.change(limitSelect, { target: { value: "5000" } });
    await waitFor(() => {
      expect(capturedRequests.some((request) => request.url === "/admin/requests?limit=5000")).toBe(true);
    });

    fireEvent.change(limitSelect, { target: { value: "all" } });
    await waitFor(() => {
      expect(capturedRequests.some((request) => request.url === "/admin/requests?limit=all")).toBe(true);
    });
    expect(screen.getAllByRole("row").length).toBeLessThan(50);
  });
});
