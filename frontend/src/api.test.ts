import { describe, expect, it } from "vitest";

import { buildAuthHeaders, parseChatStreamLine } from "./api";

describe("frontend api helpers", () => {
  it("builds bearer headers only when api key is present", () => {
    // 空 key 不应产生 Authorization，避免发送无意义密钥。
    expect(buildAuthHeaders("")).toEqual({});

    // 有 key 时按 OpenAI-compatible API 的 Bearer 形式发送。
    expect(buildAuthHeaders("local-secret")).toEqual({ Authorization: "Bearer local-secret" });
  });

  it("parses chat stream delta and usage chunks", () => {
    // ChatCompletion chunk 的 delta content 应被提取给聊天窗口。
    expect(
      parseChatStreamLine(
        'data: {"choices":[{"delta":{"content":"hi"}}],"usage":null}'
      )
    ).toEqual({ type: "delta", text: "hi" });

    // usage chunk 的 choices 为空，前端用它更新状态栏。
    expect(
      parseChatStreamLine(
        'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2,"total_tokens":3}}'
      )
    ).toEqual({ type: "usage", usage: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 3 } });

    // DONE 行用于结束流式读取。
    expect(parseChatStreamLine("data: [DONE]")).toEqual({ type: "done" });
  });

  it("parses stream error events", () => {
    // 后端会把 Codex 上游流式异常转成 error 事件，避免浏览器显示 network error。
    expect(
      parseChatStreamLine(
        'data: {"error":{"message":"The model requires a newer version of Codex.","status_code":400}}'
      )
    ).toEqual({
      type: "error",
      message: "The model requires a newer version of Codex.",
      statusCode: 400
    });
  });
});
