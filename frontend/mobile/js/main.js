/**
 * 移动端入口：触摸手势 -> 远端鼠标/键盘事件。
 *
 * 手势约定（远程控制的常见映射）：
 * - 单指按下/拖动 -> 左键按下 + 移动，抬起 -> 左键释放
 * - 双指点按 -> 右键单击
 * - 双指上下拖动 -> 滚轮
 * - 「键盘」按钮 -> 弹出输入框，按文本注入（避开移动端键码差异）
 */
import { $, LogView } from '../../shared/format.js';
import {
  commandMessage,
  keyEvent,
  mouseButton,
  mouseMove,
  mouseWheel,
  textEvent,
  touchEvent,
} from '../../shared/protocol.js';
import { ControlTransport, defaultControlUrl } from '../../shared/transport.js';

const canvas = $('#screenCanvas');
const stage = $('#stage');
const placeholder = $('#placeholder');
const toastEl = $('#toast');
const statusText = $('#statusText');
const statusDot = $('#statusDot');
const statusPill = $('#statusWrap');
const hudSize = $('#hudSize');
const connectBtn = $('#connectBtn');
const injectBtn = $('#injectBtn');

const logView = new LogView(document.createElement('div'));

const viewer = {
  image: new Image(),
  decoding: false,
  pending: null,
  screenWidth: 0,
  screenHeight: 0,
  hasFrame: false,
};

const transport = new ControlTransport({
  url: defaultControlUrl(),
  onState: (state) => {
    statusText.textContent =
      { closed: '已断开', connecting: '连接中…', open: '已连接', reconnecting: '重连中…' }[state] ||
      state;
    statusDot.classList.toggle('open', state === 'open');
    statusDot.classList.toggle('error', state === 'closed' || state === 'reconnecting');
    if (statusPill) {
      statusPill.classList.toggle('open', state === 'open');
      statusPill.classList.toggle('reconnecting', state === 'reconnecting');
      statusPill.classList.toggle('closed', state === 'closed');
    }
    connectBtn.textContent = state === 'open' ? '断开' : '连接';
    if (state === 'open') transport.send(commandMessage('devices'));
  },
  onLog: (text) => logView.push(text),
  onMessage: handleMessage,
});

let injectEnabled = false;
let lastInjected = 0;

// ---------------------------------------------------------------- 画面
const context = canvas.getContext('2d', { alpha: false });
viewer.image.decoding = 'async';
viewer.image.onload = () => {
  canvas.width = viewer.image.naturalWidth;
  canvas.height = viewer.image.naturalHeight;
  context.drawImage(viewer.image, 0, 0);
  viewer.decoding = false;
  if (!viewer.hasFrame) {
    viewer.hasFrame = true;
    placeholder.classList.add('hidden');
    canvas.classList.add('visible');
  }
  if (viewer.pending) {
    const next = viewer.pending;
    viewer.pending = null;
    showFrame(next);
  }
};

function showFrame(args) {
  if (!args?.image) return;
  if (viewer.decoding) {
    viewer.pending = args; // 只留最新帧
    return;
  }
  viewer.decoding = true;
  viewer.screenWidth = args.screen_width || viewer.screenWidth || args.width;
  viewer.screenHeight = args.screen_height || viewer.screenHeight || args.height;
  hudSize.textContent = `${args.width}×${args.height}`;
  hudSize.hidden = false;
  viewer.image.src = `data:image/png;base64,${args.image}`;
}

function handleMessage(message) {
  if (message?.kind !== 'command') return;
  const args = message.args || {};
  if (message.name === 'frame') {
    showFrame(args);
    return;
  }
  if (message.name === 'hello') {
    toast(`服务端 ${args.platform} · 注入${args.allow_inject ? '可用' : '禁用'}`);
    return;
  }
  if (message.name === 'devices') {
    const displays = args.displays || [];
    toast(`显示器 ${displays.length} 台 · 设备 ${(args.items || []).length} 个`);
    return;
  }
  if (message.name === 'state') {
    if (args.inject_enabled !== undefined) {
      injectEnabled = Boolean(args.inject_enabled);
      injectBtn.classList.toggle('active', injectEnabled);
    }
    if (args.injected_events !== undefined) lastInjected = args.injected_events;
    return;
  }
  if (message.name === 'error') {
    toast(args.message || '服务端错误');
  }
}

// ---------------------------------------------------------------- 坐标
function toRemote(touch) {
  const rect = canvas.getBoundingClientRect();
  if (!rect.width || !rect.height) return null;
  const scaleX = (viewer.screenWidth || canvas.width) / rect.width;
  const scaleY = (viewer.screenHeight || canvas.height) / rect.height;
  const x = (touch.clientX - rect.left) * scaleX;
  const y = (touch.clientY - rect.top) * scaleY;
  return {
    x: Math.max(0, Math.min(viewer.screenWidth || canvas.width, x)),
    y: Math.max(0, Math.min(viewer.screenHeight || canvas.height, y)),
  };
}

function send(message) {
  if (!transport.connected) return false;
  return transport.send(message);
}

// ---------------------------------------------------------------- 手势
let lastTouch = null;
let twoFingerStartY = 0;
let twoFingerMoved = false;

stage.addEventListener(
  'touchstart',
  (event) => {
    event.preventDefault();
    if (event.touches.length === 1) {
      const point = toRemote(event.touches[0]);
      if (!point) return;
      lastTouch = point;
      send(mouseMove(point.x, point.y));
      send(mouseButton(point.x, point.y, 1, true));
      return;
    }

    if (event.touches.length === 2) {
      twoFingerMoved = false;
      twoFingerStartY = (event.touches[0].clientY + event.touches[1].clientY) / 2;
    }
  },
  { passive: false },
);

stage.addEventListener(
  'touchmove',
  (event) => {
    event.preventDefault();

    if (event.touches.length === 1 && lastTouch) {
      const point = toRemote(event.touches[0]);
      if (!point) return;
      lastTouch = point;
      send(mouseMove(point.x, point.y));
      return;
    }

    if (event.touches.length === 2 && lastTouch) {
      const currentY = (event.touches[0].clientY + event.touches[1].clientY) / 2;
      const delta = twoFingerStartY - currentY;
      if (Math.abs(delta) > 12) {
        send(mouseWheel(lastTouch.x, lastTouch.y, delta > 0 ? 120 : -120));
        twoFingerStartY = currentY;
        twoFingerMoved = true;
      }
    }
  },
  { passive: false },
);

stage.addEventListener(
  'touchend',
  (event) => {
    event.preventDefault();
    if (event.touches.length === 0) {
      if (lastTouch) {
        if (event.changedTouches.length >= 2 || twoFingerMoved) {
          // 双指点按：右键单击；双指拖动后不再补点击
          if (!twoFingerMoved) {
            send(mouseButton(lastTouch.x, lastTouch.y, 2, true));
            send(mouseButton(lastTouch.x, lastTouch.y, 2, false));
          }
        } else {
          send(mouseButton(lastTouch.x, lastTouch.y, 1, false));
        }
      }
      lastTouch = null;
      twoFingerMoved = false;
      return;
    }

    if (event.touches.length === 1 && lastTouch) {
      // 从双指回到单指：视为一次右键点击结束
      send(mouseButton(lastTouch.x, lastTouch.y, 2, false));
    }
  },
  { passive: false },
);

// ---------------------------------------------------------------- 操作栏
connectBtn.addEventListener('click', () => {
  if (transport.connected) {
    transport.disconnect();
  } else {
    transport.connect();
  }
});

injectBtn.addEventListener('click', () => {
  const next = !injectEnabled;
  send(commandMessage('toggle_inject', { enabled: next }));
  if (!next) toast('已关闭注入（仅预览）');
});

$('#keyboardBtn').addEventListener('click', () => {
  const text = window.prompt('输入要发送到远端的文字');
  if (text) {
    send(textEvent(text));
    toast(`已发送 ${text.length} 个字符`);
  }
});

$('#rightClickBtn').addEventListener('click', () => {
  if (!lastTouch) {
    toast('请先在画面上点一下定位');
    return;
  }
  send(mouseButton(lastTouch.x, lastTouch.y, 2, true));
  send(mouseButton(lastTouch.x, lastTouch.y, 2, false));
});

$('#scrollBtn').addEventListener('click', () => {
  if (!lastTouch) {
    toast('请先在画面上点一下定位');
    return;
  }
  send(mouseWheel(lastTouch.x, lastTouch.y, 120));
});

$('#devicesBtn').addEventListener('click', () => {
  send(commandMessage('devices'));
});

for (const key of ['Enter', 'Backspace', 'Escape']) {
  document.addEventListener('keydown', (event) => {
    if (event.key === key) send(keyEvent(event.keyCode || 0, true));
  });
}

function toast(text) {
  toastEl.textContent = text;
  toastEl.classList.add('show');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => toastEl.classList.remove('show'), 2200);
}

window.addEventListener('beforeunload', () => transport.disconnect());
