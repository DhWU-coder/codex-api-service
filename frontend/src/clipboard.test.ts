import { afterEach, describe, expect, it, vi } from "vitest";

import { copyConnectionAddress } from "./clipboard";

const originalExecCommand = Object.getOwnPropertyDescriptor(document, "execCommand");

afterEach(() => {
  // 还原浏览器能力，避免兼容路径的模拟影响其他测试。
  document.body.innerHTML = "";
  if (originalExecCommand) Object.defineProperty(document, "execCommand", originalExecCommand);
  else Reflect.deleteProperty(document, "execCommand");
  vi.unstubAllGlobals();
});

describe("connection address clipboard", () => {
  it("uses the clipboard API when available", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    await copyConnectionAddress("http://10.0.0.1:1219/v1");
    expect(writeText).toHaveBeenCalledWith("http://10.0.0.1:1219/v1");
    expect(document.querySelector("textarea")).toBeNull();
  });

  it.each(["missing", "denied"])("falls back when the clipboard API is %s and restores focus", async (mode) => {
    // HTTP 页面缺失 API 或权限被拒绝时仍尝试复制，并清理临时文本框。
    vi.stubGlobal("navigator", mode === "missing" ? {} : { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    const button = document.createElement("button");
    document.body.appendChild(button);
    button.focus();
    const execCommand = vi.fn(() => {
      expect((document.activeElement as HTMLTextAreaElement).value).toBe("http://10.0.0.1:1219/ui");
      return true;
    });
    Object.defineProperty(document, "execCommand", { configurable: true, value: execCommand });
    await copyConnectionAddress("http://10.0.0.1:1219/ui");
    expect(execCommand).toHaveBeenCalledWith("copy");
    expect(document.activeElement).toBe(button);
    expect(document.querySelector("textarea")).toBeNull();
  });

  it.each([false, undefined])("reports failure when legacy copying is unavailable or returns %s", async (result) => {
    // 浏览器没有任何可用复制方式时必须失败，不能显示虚假的成功提示。
    vi.stubGlobal("navigator", {});
    Object.defineProperty(document, "execCommand", { configurable: true, value: result === undefined ? undefined : vi.fn(() => result) });
    await expect(copyConnectionAddress("http://127.0.0.1:1219/v1")).rejects.toThrow("复制失败");
    expect(document.querySelector("textarea")).toBeNull();
  });
});
