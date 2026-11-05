// 局域网 HTTP 页面可能没有剪贴板 API，复制地址时使用兼容路径并明确报告失败。
export async function copyConnectionAddress(value: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return;
    } catch {
      // 权限拒绝时继续尝试由本次点击触发的传统复制。
    }
  }

  const previousFocus = document.activeElement;
  const selection = document.getSelection();
  const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, index) => selection.getRangeAt(index).cloneRange()) : [];
  const input = document.createElement("textarea");
  input.value = value;
  input.readOnly = true;
  input.style.position = "fixed";
  input.style.left = "-9999px";
  document.body.appendChild(input);
  try {
    input.focus({ preventScroll: true });
    input.select();
    input.setSelectionRange(0, value.length);
    if (typeof document.execCommand !== "function" || !document.execCommand("copy")) {
      throw new Error("复制失败，请手动选择地址复制。");
    }
  } finally {
    // 无论复制是否成功，都移除临时文本框并还原焦点和页面选区。
    input.remove();
    if (previousFocus instanceof HTMLElement) previousFocus.focus({ preventScroll: true });
    if (selection) {
      selection.removeAllRanges();
      ranges.forEach((range) => selection.addRange(range));
    }
  }
}
