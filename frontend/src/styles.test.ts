import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

// 读取真实 CSS 文件，避免主题变量被无意改回绿色系后测试仍然通过。
const styles = readFileSync(resolve(__dirname, "styles.css"), "utf-8");

describe("console visual theme", () => {
  it("uses a cool tech accent palette instead of the old green-dominant palette", () => {
    // 主按钮和看板图标应使用电蓝，表达科技控制台的主视觉。
    expect(styles).toContain("--primary-bg: #4f8cff");

    // Token 输出和推理使用蓝紫色，和主强调色形成冷色科技感。
    expect(styles).toContain("--token-output: #7aa7ff");
    expect(styles).toContain("--token-reasoning: #b26cff");

    // 侧栏背景切换为深空黑，而不是墨绿色。
    expect(styles).toContain("--sidebar-bg: #080d16");
  });
});
