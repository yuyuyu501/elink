/** 前端公用小工具（DOM 查询、单位换算、日志）。 */

export const $ = (selector, root = document) => root.querySelector(selector);
export const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

export function formatBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

export function formatClock(date = new Date()) {
  return date.toLocaleTimeString('zh-CN', { hour12: false });
}

/** 滚动日志容器：只保留最近 N 条，避免长会话把 DOM 拖垮。 */
export class LogView {
  constructor(container, { limit = 200 } = {}) {
    this.container = container;
    this.limit = limit;
  }

  push(text, level = 'info') {
    if (!this.container) return;
    const line = document.createElement('div');
    line.className = `log-line log-${level}`;
    line.textContent = `[${formatClock()}] ${text}`;
    this.container.prepend(line);
    while (this.container.childElementCount > this.limit) {
      this.container.lastElementChild?.remove();
    }
  }

  clear() {
    if (this.container) this.container.innerHTML = '';
  }
}
