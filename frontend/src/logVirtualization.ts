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
  detailHeight?: number;
  overscan?: number;
  expandedIndex?: number;
};

// 计算固定行高日志列表的可见窗口；展开行用额外高度修正后续行位置。
export function calculateVirtualLogWindow(options: VirtualLogWindowOptions): VirtualLogWindow {
  const itemCount = Math.max(0, Math.floor(options.itemCount));
  const rowHeight = options.rowHeight || 48;
  const detailHeight = options.detailHeight || 88;
  const viewportHeight = options.viewportHeight || 560;
  const overscan = options.overscan ?? 8;
  const expandedIndex =
    options.expandedIndex !== undefined && options.expandedIndex >= 0 && options.expandedIndex < itemCount
      ? options.expandedIndex
      : undefined;
  const extraHeight = expandedIndex === undefined ? 0 : detailHeight;

  if (!itemCount) {
    return { totalHeight: 0, items: [] };
  }

  // 将像素偏移映射回行号，展开区域内仍归属于被展开的主行。
  const indexAtOffset = (offset: number): number => {
    const safeOffset = Math.max(0, offset);
    if (expandedIndex === undefined) {
      return Math.min(itemCount - 1, Math.floor(safeOffset / rowHeight));
    }
    const expandedTop = expandedIndex * rowHeight;
    const expandedBottom = expandedTop + rowHeight + detailHeight;
    if (safeOffset < expandedTop) {
      return Math.min(itemCount - 1, Math.floor(safeOffset / rowHeight));
    }
    if (safeOffset < expandedBottom) {
      return expandedIndex;
    }
    return Math.min(itemCount - 1, Math.floor((safeOffset - detailHeight) / rowHeight));
  };

  const startIndex = Math.max(0, indexAtOffset(options.scrollTop) - overscan);
  const endIndex = Math.min(
    itemCount - 1,
    indexAtOffset(options.scrollTop + viewportHeight) + overscan
  );
  const items: VirtualLogItem[] = [];
  for (let index = startIndex; index <= endIndex; index += 1) {
    const isExpanded = index === expandedIndex;
    items.push({
      index,
      top: index * rowHeight + (expandedIndex !== undefined && index > expandedIndex ? detailHeight : 0),
      height: rowHeight + (isExpanded ? detailHeight : 0)
    });
  }

  return {
    totalHeight: itemCount * rowHeight + extraHeight,
    items
  };
}
