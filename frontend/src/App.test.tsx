import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

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
    fast_mode: true
  },
  usage: { enabled: true, path: ".codex-usage/usage.jsonl" },
  auth: { auth_path: "~/.codex/auth.json", import_auth_path: "~/.codex/auth.json" },
  config_path: "config.yaml"
};

// 管理台 health 响应用于驱动左侧中文服务状态和配置页运行详情。
const adminHealth = {
  server: {
    api: "http://127.0.0.1:1219/v1",
    console: "http://127.0.0.1:1219/ui"
  },
  oauth: { available: true },
  usage: { enabled: true, writable: true, path: ".codex-usage/usage.jsonl" },
  ui: { built: true },
  codex: { client_version: "0.136.0" }
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
    error: null
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
    error: null
  }
];

describe("App theme mode", () => {
  let capturedRequests: Array<{ url: string; method: string; body: unknown }> = [];
  let healthEndpointAvailable = true;

  beforeEach(() => {
    capturedRequests = [];
    healthEndpointAvailable = true;

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
        if (init?.body) {
          // 记录前端发出的 JSON body，便于断言聊天和配置保存参数。
          capturedRequests.push({ url, method, body: JSON.parse(String(init.body)) });
        }
        if (url.startsWith("/admin/requests")) {
          return new Response(JSON.stringify({ items: requestLogItems }), { status: 200 });
        }
        if (url === "/admin/health") {
          if (!healthEndpointAvailable) {
            return new Response(JSON.stringify({ detail: "Not Found" }), { status: 404 });
          }
          return new Response(JSON.stringify(adminHealth), { status: 200 });
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
    // 默认首页应是看板，直接展示请求量和 token 使用概览。
    render(<App />);

    expect(await screen.findByRole("heading", { name: "数据看板" })).toBeTruthy();
    expect(await screen.findByText(window.location.host)).toBeTruthy();
    expect(await screen.findByText("服务状态")).toBeTruthy();
    expect(await screen.findByText("正常")).toBeTruthy();
    expect(screen.queryByText("OAuth ready")).toBeNull();
    expect(await screen.findByText("累计 tokens")).toBeTruthy();
    expect(await screen.findByText("60")).toBeTruthy();
    expect(await screen.findByText("100%")).toBeTruthy();
  });

  it("moves detailed runtime status to the config page", async () => {
    // 左侧只保留中文汇总，完整运行状态集中放到配置页便于理解。
    render(<App />);
    expect(await screen.findByText("服务状态")).toBeTruthy();
    expect(screen.queryByText("OAuth ready")).toBeNull();

    fireEvent.click(await screen.findByRole("button", { name: "配置" }));
    expect(await screen.findByRole("heading", { name: "运行状态" })).toBeTruthy();
    expect(await screen.findByText("登录：已检测到")).toBeTruthy();
    expect(await screen.findByText("速度：快速")).toBeTruthy();
    expect(await screen.findByText("用量：正常")).toBeTruthy();
    expect(await screen.findByText("CLI：0.136.0")).toBeTruthy();
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

  it("saves the default fast mode from the config page", async () => {
    // 配置页保存的是服务默认值，修改后需要后端重启才生效。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "配置" }));
    const defaultFastToggle = (await screen.findByRole("checkbox", { name: "默认快速模式" })) as HTMLInputElement;
    expect(defaultFastToggle.checked).toBe(true);

    // 关闭默认 fast 并保存，PATCH body 应写入 codex.fast_mode=false。
    fireEvent.click(defaultFastToggle);
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      const patchRequest = capturedRequests.find(
        (request) => request.url === "/admin/config" && request.method === "PATCH"
      );
      expect(patchRequest?.body).toMatchObject({ codex: { fast_mode: false } });
      expect(patchRequest?.body).not.toHaveProperty("api");
    });
  });

  it("filters and expands request logs", async () => {
    // 日志页应支持按文本过滤，并能展开查看 request id 和完整错误字段。
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "请求日志" }));
    fireEvent.change(await screen.findByPlaceholderText("搜索日志"), { target: { value: "/v1/responses" } });

    expect(await screen.findByText("/v1/responses")).toBeTruthy();
    expect(screen.queryByText("/v1/chat/completions")).toBeNull();

    fireEvent.click(screen.getByText("/v1/responses"));
    expect(await screen.findByText("request id")).toBeTruthy();
    expect(await screen.findByText("resp_1")).toBeTruthy();
  });
});
