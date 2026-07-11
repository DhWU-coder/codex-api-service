import { describe, expect, it } from "vitest";

import { calculateVirtualLogWindow } from "./logVirtualization";

describe("calculateVirtualLogWindow", () => {
  it("只返回首屏和预渲染范围", () => {
    const result = calculateVirtualLogWindow({ itemCount: 1000, scrollTop: 0 });

    expect(result.totalHeight).toBe(48_000);
    expect(result.items[0]).toEqual({ index: 0, top: 0, height: 48 });
    expect(result.items.length).toBeLessThan(40);
  });

  it("滚动到中部时计算正确的绝对位置", () => {
    const result = calculateVirtualLogWindow({ itemCount: 1000, scrollTop: 24_000 });

    expect(result.items[0].index).toBe(492);
    expect(result.items[0].top).toBe(23_616);
    expect(result.items.some((item) => item.index === 500)).toBe(true);
  });

  it("接近底部时不会超过日志总数", () => {
    const result = calculateVirtualLogWindow({ itemCount: 1000, scrollTop: 47_700 });

    expect(result.items.at(-1)?.index).toBe(999);
  });

  it("展开行会增加总高度并推后后续行", () => {
    const result = calculateVirtualLogWindow({
      itemCount: 1000,
      scrollTop: 480,
      expandedIndex: 10
    });

    expect(result.totalHeight).toBe(48_088);
    expect(result.items.find((item) => item.index === 10)).toEqual({ index: 10, top: 480, height: 136 });
    expect(result.items.find((item) => item.index === 11)?.top).toBe(616);
  });
});
