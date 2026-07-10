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

// 看板范围预设和 codex-usage 的基础筛选保持一致。
export type DashboardRangePreset = "today" | "week" | "month" | "all" | "recent";

// 聚合函数的可选参数；now 主要用于测试固定时间。
export type DashboardSummaryOptions = {
  preset?: DashboardRangePreset;
  recentDays?: number;
  now?: Date;
};

// 时间分布图使用的单个桶。
export type DashboardTimelineBucket = {
  key: string;
  label: string;
  totalTokens: number;
  inputTokens: number;
  outputTokens: number;
  requestCount: number;
};

// 右侧分布榜单统一结构，便于复用渲染组件。
export type DashboardDistributionItem = {
  name: string;
  totalTokens: number;
  requestCount: number;
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
  timeline: DashboardTimelineBucket[];
  modelDistribution: DashboardDistributionItem[];
  statusDistribution: DashboardDistributionItem[];
  endpointDistribution: DashboardDistributionItem[];
  recentFailures: RequestLogItem[];
  slowRequests: RequestLogItem[];
  rangeLabel: string;
  bucketLabel: string;
  lastUpdated: string;
};

// 从请求日志计算 token、健康度、耗时、模型和趋势数据。
export function summarizeRequestLogs(logs: RequestLogItem[], options: DashboardSummaryOptions = {}): DashboardSummary {
  const now = options.now || new Date();
  const preset = options.preset || "all";
  const filteredLogs = filterLogsByRange(logs, preset, options.recentDays || 7, now);
  const requestCount = filteredLogs.length;
  const successCount = filteredLogs.filter((item) => item.status_code >= 200 && item.status_code < 400).length;
  const errorCount = requestCount - successCount;
  const totalDurationMs = filteredLogs.reduce((sum, item) => sum + item.duration_ms, 0);
  const tokenBreakdown = filteredLogs.reduce<TokenBreakdown>(
    (sum, item) => ({
      input: sum.input + (item.usage?.input || 0),
      cached: sum.cached + (item.usage?.cached || 0),
      output: sum.output + (item.usage?.output || 0),
      reasoning: sum.reasoning + (item.usage?.reasoning || 0)
    }),
    { input: 0, cached: 0, output: 0, reasoning: 0 }
  );
  const totalTokens = filteredLogs.reduce((sum, item) => sum + (item.usage?.total || 0), 0);
  const bucket = timelineBucketForPreset(preset);

  return {
    requestCount,
    successCount,
    errorCount,
    successRate: requestCount ? Math.round((successCount / requestCount) * 100) : 0,
    averageDurationMs: requestCount ? Math.round(totalDurationMs / requestCount) : 0,
    totalTokens,
    tokenBreakdown,
    topModel: topModel(filteredLogs),
    trend: recentTrend(filteredLogs),
    timeline: buildTimeline(filteredLogs, bucket),
    modelDistribution: distributionBy(filteredLogs, (item) => item.model || "未知模型"),
    statusDistribution: statusDistribution(filteredLogs),
    endpointDistribution: distributionBy(filteredLogs, (item) => item.path),
    recentFailures: recentFailures(filteredLogs),
    slowRequests: slowRequests(filteredLogs),
    rangeLabel: rangeLabel(preset, options.recentDays || 7),
    bucketLabel: bucket === "hour" ? "按小时" : "按天",
    lastUpdated: latestTimestamp(filteredLogs)
  };
}

// 找出出现次数最多的模型；没有模型时显示占位符。
function topModel(logs: RequestLogItem[]): string {
  return distributionBy(logs, (item) => item.model || "未知模型")[0]?.name || "-";
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

// 根据预设过滤日志，时间比较使用浏览器本地时间，和页面展示一致。
function filterLogsByRange(
  logs: RequestLogItem[],
  preset: DashboardRangePreset,
  recentDays: number,
  now: Date
): RequestLogItem[] {
  if (preset === "all") {
    return logs;
  }

  const end = now.getTime();
  let start: Date;
  if (preset === "today") {
    start = startOfDay(now);
  } else if (preset === "week") {
    start = startOfWeek(now);
  } else if (preset === "month") {
    start = new Date(now.getFullYear(), now.getMonth(), 1);
  } else {
    start = new Date(now);
    start.setDate(start.getDate() - Math.max(1, recentDays));
  }

  const startTime = start.getTime();
  return logs.filter((item) => {
    const time = new Date(item.timestamp).getTime();
    return Number.isFinite(time) && time >= startTime && time <= end;
  });
}

// 周范围从周一开始，符合中文工作周习惯。
function startOfWeek(value: Date): Date {
  const start = startOfDay(value);
  const day = start.getDay() || 7;
  start.setDate(start.getDate() - day + 1);
  return start;
}

function startOfDay(value: Date): Date {
  return new Date(value.getFullYear(), value.getMonth(), value.getDate());
}

function timelineBucketForPreset(preset: DashboardRangePreset): "hour" | "day" {
  return preset === "today" ? "hour" : "day";
}

function buildTimeline(logs: RequestLogItem[], bucket: "hour" | "day"): DashboardTimelineBucket[] {
  const buckets = new Map<string, DashboardTimelineBucket>();
  for (const item of sortOldestFirst(logs)) {
    // 时间分布只展示成功请求的真实 token，失败请求不进入 token 柱。
    if (item.status_code >= 400 || !item.usage) {
      continue;
    }
    const date = new Date(item.timestamp);
    if (Number.isNaN(date.getTime())) {
      continue;
    }
    const key = bucket === "hour" ? hourKey(date) : dayKey(date);
    const existing =
      buckets.get(key) ||
      ({
        key,
        label: bucket === "hour" ? hourLabel(date) : dayLabel(date),
        totalTokens: 0,
        inputTokens: 0,
        outputTokens: 0,
        requestCount: 0,
      } satisfies DashboardTimelineBucket);
    existing.totalTokens += item.usage.total;
    existing.inputTokens += item.usage.input;
    existing.outputTokens += item.usage.output;
    existing.requestCount += 1;
    buckets.set(key, existing);
  }
  return Array.from(buckets.values());
}

function distributionBy(logs: RequestLogItem[], nameFor: (item: RequestLogItem) => string): DashboardDistributionItem[] {
  const byName = new Map<string, DashboardDistributionItem>();
  for (const item of logs) {
    const name = nameFor(item) || "-";
    const existing = byName.get(name) || { name, totalTokens: 0, requestCount: 0 };
    existing.totalTokens += item.usage?.total || 0;
    existing.requestCount += 1;
    byName.set(name, existing);
  }
  return Array.from(byName.values()).sort(
    (left, right) => right.totalTokens - left.totalTokens || right.requestCount - left.requestCount
  );
}

function statusDistribution(logs: RequestLogItem[]): DashboardDistributionItem[] {
  const success = logs.filter((item) => item.status_code < 400);
  const failed = logs.filter((item) => item.status_code >= 400);
  return [
    {
      name: "成功",
      totalTokens: success.reduce((sum, item) => sum + (item.usage?.total || 0), 0),
      requestCount: success.length
    },
    {
      name: "失败",
      totalTokens: failed.reduce((sum, item) => sum + (item.usage?.total || 0), 0),
      requestCount: failed.length
    }
  ];
}

function recentFailures(logs: RequestLogItem[]): RequestLogItem[] {
  return sortNewestFirst(logs)
    .filter((item) => item.status_code >= 400)
    .slice(0, 5);
}

function slowRequests(logs: RequestLogItem[]): RequestLogItem[] {
  return [...logs].sort((left, right) => right.duration_ms - left.duration_ms).slice(0, 5);
}

function latestTimestamp(logs: RequestLogItem[]): string {
  const latest = sortNewestFirst(logs)[0]?.timestamp;
  return latest ? new Date(latest).toLocaleString() : "-";
}

function sortNewestFirst(logs: RequestLogItem[]): RequestLogItem[] {
  return [...logs].sort((left, right) => new Date(right.timestamp).getTime() - new Date(left.timestamp).getTime());
}

function sortOldestFirst(logs: RequestLogItem[]): RequestLogItem[] {
  return [...logs].sort((left, right) => new Date(left.timestamp).getTime() - new Date(right.timestamp).getTime());
}

function hourKey(date: Date): string {
  return `${dayKey(date)} ${String(date.getHours()).padStart(2, "0")}`;
}

function dayKey(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}

function hourLabel(date: Date): string {
  return `${String(date.getHours()).padStart(2, "0")}:00`;
}

function dayLabel(date: Date): string {
  return `${String(date.getMonth() + 1).padStart(2, "0")}/${String(date.getDate()).padStart(2, "0")}`;
}

function rangeLabel(preset: DashboardRangePreset, recentDays: number): string {
  if (preset === "today") {
    return "今日";
  }
  if (preset === "week") {
    return "本周";
  }
  if (preset === "month") {
    return "本月";
  }
  if (preset === "recent") {
    return `最近 ${recentDays} 天`;
  }
  return "全部";
}

// 紧凑数字格式用于指标卡，避免大 token 数挤压布局。
export function formatCompactNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 }).format(value);
}

// 完整数字用于用量分析页，保留千分位让大 token 更像正式报表。
export function formatNumber(value: number): string {
  return new Intl.NumberFormat("zh-CN").format(value);
}

// 毫秒耗时格式化为 ms 或 s，保证平均耗时一眼可读。
export function formatDuration(ms: number): string {
  if (ms < 1000) {
    return `${ms}ms`;
  }
  return `${(ms / 1000).toFixed(1)}s`;
}
