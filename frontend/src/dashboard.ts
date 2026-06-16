import type { RequestLogItem } from "./types";

// 看板中的 token 类型顺序固定，保证 UI 和测试展示稳定。
export type TokenBreakdown = {
  input: number;
  cached: number;
  output: number;
  reasoning: number;
};

// 趋势柱只保留渲染需要的字段，避免看板泄漏请求正文。
export type DashboardTrendItem = {
  id: string;
  timestamp: string;
  totalTokens: number;
  statusCode: number;
};

// 看板汇总数据是 UI 的唯一输入，组件不再重复写统计逻辑。
export type DashboardSummary = {
  requestCount: number;
  successCount: number;
  errorCount: number;
  successRate: number;
  averageDurationMs: number;
  totalTokens: number;
  tokenBreakdown: TokenBreakdown;
  topModel: string;
  trend: DashboardTrendItem[];
};

// 从请求日志计算 token、健康度、耗时、模型和趋势数据。
export function summarizeRequestLogs(logs: RequestLogItem[]): DashboardSummary {
  const requestCount = logs.length;
  const successCount = logs.filter((item) => item.status_code >= 200 && item.status_code < 400).length;
  const errorCount = requestCount - successCount;
  const totalDurationMs = logs.reduce((sum, item) => sum + item.duration_ms, 0);
  const tokenBreakdown = logs.reduce<TokenBreakdown>(
    (sum, item) => ({
      input: sum.input + (item.usage?.input || 0),
      cached: sum.cached + (item.usage?.cached || 0),
      output: sum.output + (item.usage?.output || 0),
      reasoning: sum.reasoning + (item.usage?.reasoning || 0)
    }),
    { input: 0, cached: 0, output: 0, reasoning: 0 }
  );
  const totalTokens = logs.reduce((sum, item) => sum + (item.usage?.total || 0), 0);

  return {
    requestCount,
    successCount,
    errorCount,
    successRate: requestCount ? Math.round((successCount / requestCount) * 100) : 0,
    averageDurationMs: requestCount ? Math.round(totalDurationMs / requestCount) : 0,
    totalTokens,
    tokenBreakdown,
    topModel: topModel(logs),
    trend: recentTrend(logs)
  };
}

// 找出出现次数最多的模型；没有模型时显示占位符。
function topModel(logs: RequestLogItem[]): string {
  const counts = new Map<string, number>();
  for (const item of logs) {
    if (!item.model) {
      continue;
    }
    counts.set(item.model, (counts.get(item.model) || 0) + 1);
  }

  let winner = "-";
  let winnerCount = 0;
  for (const [model, count] of counts) {
    if (count > winnerCount) {
      winner = model;
      winnerCount = count;
    }
  }
  return winner;
}

// 最近请求日志通常按新到旧返回，趋势图改成旧到新更符合阅读习惯。
function recentTrend(logs: RequestLogItem[]): DashboardTrendItem[] {
  return logs
    .slice(0, 20)
    .reverse()
    .map((item) => ({
      id: item.id,
      timestamp: item.timestamp,
      totalTokens: item.usage?.total || 0,
      statusCode: item.status_code
    }));
}

// 紧凑数字格式用于指标卡，避免大 token 数挤压布局。
export function formatCompactNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

// 毫秒耗时格式化为 ms 或 s，保证平均耗时一眼可读。
export function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${ms}ms`;
  }
  return `${(ms / 1000).toFixed(1)}s`;
}
