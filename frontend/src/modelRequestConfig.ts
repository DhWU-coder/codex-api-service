import type {
  AdminConfig,
  AdminModelCatalog,
  AdminModelRequestDefault,
  ModelRequestConfigRow
} from "./types";

const DEFAULT_MODEL_REQUEST: AdminModelRequestDefault = {
  reasoning_effort: "medium",
  fast_mode: false
};

// 合并动态目录、持久化覆盖和旧配置迁移状态，生成页面稳定行模型。
export function buildModelRequestRows(
  catalog: AdminModelCatalog | null,
  config: AdminConfig
): ModelRequestConfigRow[] {
  const rows = new Map<string, ModelRequestConfigRow>();
  // 兼容静态资源先更新、后端进程仍返回旧配置快照的短暂重启窗口。
  const savedDefaults = config.codex.model_request_defaults || {};
  const usesLegacyDefaults = config.codex.uses_legacy_request_defaults ?? true;
  for (const entry of catalog?.models || []) {
    const saved = savedDefaults[entry.id];
    const defaults = saved ||
      (usesLegacyDefaults
        ? { reasoning_effort: config.codex.reasoning_effort, fast_mode: config.codex.fast_mode }
        : DEFAULT_MODEL_REQUEST);
    // 固定兜底始终是 medium；目录未声明时也把当前配置加入选项，避免读取后被静默改写。
    const supported = [...new Set([
      ...(entry.supported_reasoning_efforts.length
        ? entry.supported_reasoning_efforts
        : ["low", "medium", "high", "xhigh"]),
      "medium",
      defaults.reasoning_effort
    ])];
    rows.set(entry.id, {
      id: entry.id,
      displayName: entry.display_name || entry.id,
      reasoningEffort: defaults.reasoning_effort,
      fastMode: defaults.fast_mode,
      supportedReasoningEfforts: supported,
      available: true
    });
  }

  for (const [modelId, defaults] of Object.entries(savedDefaults)) {
    if (!rows.has(modelId)) {
      rows.set(modelId, {
        id: modelId,
        displayName: modelId,
        reasoningEffort: defaults.reasoning_effort,
        fastMode: defaults.fast_mode,
        supportedReasoningEfforts: [...new Set([defaults.reasoning_effort, "medium"])],
        available: false
      });
    }
  }
  return [...rows.values()];
}

// 保存时只保留偏离固定 medium/false 的模型，恢复默认即可删除 YAML 条目。
export function serializeModelRequestOverrides(
  rows: ModelRequestConfigRow[]
): Record<string, AdminModelRequestDefault> {
  return Object.fromEntries(
    rows
      .filter((row) => row.reasoningEffort !== "medium" || row.fastMode)
      .map((row) => [
        row.id,
        { reasoning_effort: row.reasoningEffort, fast_mode: row.fastMode }
      ])
  );
}

// 聊天页按当前真实模型取得已合并默认值，未知模型使用固定兜底。
export function requestDefaultsForModel(
  rows: ModelRequestConfigRow[],
  modelId: string
): AdminModelRequestDefault {
  const row = rows.find((item) => item.id === modelId);
  return row
    ? { reasoning_effort: row.reasoningEffort, fast_mode: row.fastMode }
    : { ...DEFAULT_MODEL_REQUEST };
}
