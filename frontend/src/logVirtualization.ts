export type VirtualLogItem = {
  index: number;
  top: number;
  height: number;
};

export type VirtualLogWindow = {
  totalHeight: number;
  items: VirtualLogItem[];
};

export type VirtualLogWindowOptions = {
  itemCount: number;
  scrollTop: number;
  viewportHeight?: number;
  rowHeight?: number;
  overscan?: number;
};

// 计算固定行高日志列表的可见窗口；详情抽屉不参与列表几何尺寸。
export function calculateVirtualLogWindow(options: VirtualLogWindowOptions): VirtualLogWindow {
  const itemCount = Math.max(0, Math.floor(options.itemCount));
  const rowHeight = options.rowHeight || 48;
  const viewportHeight = options.viewportHeight || 560;
  const overscan = options.overscan ?? 8;

  if (!itemCount) {
    return { totalHeight: 0, items: [] };
  }

  // 固定行高可以直接把像素偏移映射为行号。
  const indexAtOffset = (offset: number): number => {
    const safeOffset = Math.max(0, offset);
    return Math.min(itemCount - 1, Math.floor(safeOffset / rowHeight));
  };

  const startIndex = Math.max(0, indexAtOffset(options.scrollTop) - overscan);
  const endIndex = Math.min(
    itemCount - 1,
    indexAtOffset(options.scrollTop + viewportHeight) + overscan
  );
  const items: VirtualLogItem[] = [];
  for (let index = startIndex; index <= endIndex; index += 1) {
    items.push({
      index,
      top: index * rowHeight,
      height: rowHeight
    });
  }

  return {
    totalHeight: itemCount * rowHeight,
    items
  };
}
