import { describe, expect, it } from "vitest";

import { buildModelRequestRows, requestDefaultsForModel, serializeModelRequestOverrides } from "./modelRequestConfig";
import type { AdminConfig, AdminModelCatalog } from "./types";

function config(overrides: Partial<AdminConfig["codex"]> = {}): AdminConfig {
  return {
    server: { host: "127.0.0.1", port: 1219 },
    api: { local_api_key_configured: false },
    codex: {
      default_model: "gpt-5.4",
      available_models: ["gpt-5.4"],
      reasoning_effort: "medium",
      timeout_seconds: 120,
      include_reasoning: true,
      fast_mode: false,
      model_request_defaults: {},
      uses_legacy_request_defaults: false,
      ...overrides
    },
    usage: { enabled: true, path: ".codex-usage/usage.jsonl" },
    auth: { auth_path: "", import_auth_path: "" },
    config_path: "config.yaml"
  };
}

const catalog: AdminModelCatalog = {
  models: [
    {
      id: "gpt-5.4",
      display_name: "GPT-5.4",
      default_reasoning_effort: "medium",
      supported_reasoning_efforts: ["low", "medium", "high", "xhigh"],
      source: "cli"
    },
    {
      id: "gpt-5.6-sol",
      display_name: "GPT-5.6-Sol",
      default_reasoning_effort: "low",
      supported_reasoning_efforts: ["low", "medium", "high"],
      source: "cli"
    }
  ],
  effective_default_model: "gpt-5.4",
  cache_state: "fresh",
  source: "cli"
};

describe("model request config helpers", () => {
  it("merges catalog, saved overrides, and unavailable configured models", () => {
    const rows = buildModelRequestRows(
      catalog,
      config({
        model_request_defaults: {
          "gpt-5.4": { reasoning_effort: "high", fast_mode: true },
          "gpt-retired": { reasoning_effort: "xhigh", fast_mode: false }
        }
      })
    );

    expect(rows).toHaveLength(3);
    expect(rows[0]).toMatchObject({ id: "gpt-5.4", reasoningEffort: "high", fastMode: true, available: true });
    expect(rows[1]).toMatchObject({ id: "gpt-5.6-sol", reasoningEffort: "medium", fastMode: false });
    expect(rows[2]).toMatchObject({ id: "gpt-retired", reasoningEffort: "xhigh", available: false });
  });

  it("uses legacy globals only before migration", () => {
    const rows = buildModelRequestRows(
      catalog,
      config({ reasoning_effort: "high", fast_mode: true, uses_legacy_request_defaults: true })
    );

    expect(rows.every((row) => row.reasoningEffort === "high" && row.fastMode)).toBe(true);
  });

  it("accepts an old admin snapshot while the backend is restarting", () => {
    // 旧进程未返回新字段时继续使用旧全局值，不能让整个控制台白屏。
    const oldSnapshot = config({ reasoning_effort: "high", fast_mode: true });
    delete (oldSnapshot.codex as Partial<typeof oldSnapshot.codex>).model_request_defaults;
    delete (oldSnapshot.codex as Partial<typeof oldSnapshot.codex>).uses_legacy_request_defaults;

    const rows = buildModelRequestRows(catalog, oldSnapshot);

    expect(rows[0]).toMatchObject({ reasoningEffort: "high", fastMode: true });
  });

  it("serializes only non-default values and resolves chat defaults", () => {
    const rows = buildModelRequestRows(catalog, config());
    rows[0] = { ...rows[0], reasoningEffort: "high", fastMode: true };

    expect(serializeModelRequestOverrides(rows)).toEqual({
      "gpt-5.4": { reasoning_effort: "high", fast_mode: true }
    });
    expect(requestDefaultsForModel(rows, "gpt-5.4")).toEqual({ reasoning_effort: "high", fast_mode: true });
    expect(requestDefaultsForModel(rows, "unknown")).toEqual({ reasoning_effort: "medium", fast_mode: false });
  });
});
