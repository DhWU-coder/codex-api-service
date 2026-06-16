import { describe, expect, it } from "vitest";

import { summarizeRequestLogs } from "./dashboard";
import type { RequestLogItem } from "./types";

// 构造看板测试日志，覆盖成功、失败、有 usage 和无 usage 的请求。
const requestLogs: RequestLogItem[] = [
  {
    id: "req_3",
    timestamp: "2026-06-16T08:03:00.000Z",
    method: "POST",
    path: "/v1/chat/completions",
    model: "gpt-5.5",
    status_code: 500,
    duration_ms: 3000,
    usage: null,
    request_id: null,
    error: "upstream error"
  },
  {
    id: "req_2",
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
    id: "req_1",
    timestamp: "2026-06-16T08:01:00.000Z",
    method: "POST",
    path: "/v1/responses",
    model: "gpt-5-mini",
    status_code: 200,
    duration_ms: 1000,
    usage: { total: 20, input: 12, cached: 2, output: 8, reasoning: 1 },
    request_id: "resp_1",
    error: null
  }
];

describe("dashboard summary", () => {
  it("aggregates token usage, request health, model mix, and trend bars", () => {
    // 看板应从最近请求日志里提取用户最关心的使用量和健康度。
    const summary = summarizeRequestLogs(requestLogs);

    expect(summary.requestCount).toBe(3);
    expect(summary.successCount).toBe(2);
    expect(summary.errorCount).toBe(1);
    expect(summary.successRate).toBe(67);
    expect(summary.averageDurationMs).toBe(2000);
    expect(summary.totalTokens).toBe(60);
    expect(summary.tokenBreakdown).toEqual({ input: 32, cached: 7, output: 23, reasoning: 5 });
    expect(summary.topModel).toBe("gpt-5.5");
    expect(summary.trend.map((item) => item.id)).toEqual(["req_1", "req_2", "req_3"]);
    expect(summary.trend.map((item) => item.totalTokens)).toEqual([20, 40, 0]);
  });

  it("returns stable empty-state values when there are no request logs", () => {
    // 空日志时看板不能显示 NaN 或 undefined，要给可读的零值。
    const summary = summarizeRequestLogs([]);

    expect(summary.requestCount).toBe(0);
    expect(summary.successRate).toBe(0);
    expect(summary.averageDurationMs).toBe(0);
    expect(summary.totalTokens).toBe(0);
    expect(summary.tokenBreakdown).toEqual({ input: 0, cached: 0, output: 0, reasoning: 0 });
    expect(summary.topModel).toBe("-");
    expect(summary.trend).toEqual([]);
  });
});
