/**
 * 控制通道传输层（浏览器 WebSocket）。
 *
 * - 断线自动重连（指数退避 + 抖动，和 backend/session/reconnect.py 的策略一致）；
 * - 收发都用 backend 的协议帧，页面之间无需知道帧长细节；
 * - `send()` 在未连接时直接丢弃输入事件（输入有时效性，攒着补发反而有害）。
 */
import { FrameDecoder, decodeJson, encodeFrame, FrameType } from './protocol.js';

export const TransportState = Object.freeze({
  CLOSED: 'closed',
  CONNECTING: 'connecting',
  OPEN: 'open',
  RECONNECTING: 'reconnecting',
});

export class ControlTransport {
  constructor({ url, onMessage, onState, onLog }) {
    this.url = url;
    this.onMessage = onMessage || (() => {});
    this.onState = onState || (() => {});
    this.onLog = onLog || (() => {});

    this.state = TransportState.CLOSED;
    this.attempts = 0;
    this.decoder = new FrameDecoder();
    this.socket = null;
    this._manualClose = false;
    this._reconnectTimer = null;
    this.sentMessages = 0;
    this.receivedMessages = 0;
  }

  get connected() {
    return this.state === TransportState.OPEN;
  }

  connect() {
    this._manualClose = false;
    this._setState(this.attempts > 0 ? TransportState.RECONNECTING : TransportState.CONNECTING);
    this.onLog(`连接 ${this.url} …`);

    let socket;
    try {
      socket = new WebSocket(this.url);
    } catch (error) {
      this.onLog(`创建 WebSocket 失败: ${error}`);
      this._scheduleReconnect();
      return;
    }
    socket.binaryType = 'arraybuffer';
    this.socket = socket;

    socket.onopen = () => {
      this.attempts = 0;
      this.decoder.reset();
      this._setState(TransportState.OPEN);
      this.onLog('已连接');
    };

    socket.onmessage = (event) => {
      if (!(event.data instanceof ArrayBuffer)) {
        this.onLog('忽略非二进制消息（协议要求二进制帧）');
        return;
      }
      const chunk = new Uint8Array(event.data);
      for (const frame of this.decoder.push(chunk)) {
        if (frame.frameType === FrameType.PING) {
          this._sendRaw(encodeFrame({ kind: 'pong' }, FrameType.PONG));
          continue;
        }
        if (frame.frameType !== FrameType.JSON) continue;
        try {
          const message = decodeJson(frame.payload);
          this.receivedMessages += 1;
          this.onMessage(message);
        } catch (error) {
          this.onLog(`消息解析失败: ${error}`);
        }
      }
    };

    socket.onerror = () => {
      this.onLog('WebSocket 出错');
    };

    socket.onclose = () => {
      this.socket = null;
      if (this._manualClose) {
        this._setState(TransportState.CLOSED);
        return;
      }
      this._scheduleReconnect();
    };
  }

  disconnect() {
    this._manualClose = true;
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
    if (this.socket) {
      this.socket.close();
      this.socket = null;
    }
    this._setState(TransportState.CLOSED);
  }

  /** 发送一条消息（自动编成协议帧）。 */
  send(message) {
    if (!this.connected) return false;
    return this._sendRaw(encodeFrame(message));
  }

  _sendRaw(frame) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) return false;
    this.socket.send(frame);
    this.sentMessages += 1;
    return true;
  }

  _scheduleReconnect() {
    this._setState(TransportState.RECONNECTING);
    this.attempts += 1;
    const base = Math.min(15000, 500 * Math.pow(1.8, this.attempts - 1));
    const jitter = base * 0.3 * (Math.random() * 2 - 1);
    const delay = Math.max(200, base + jitter);
    this.onLog(`连接断开，${(delay / 1000).toFixed(1)}s 后重连（第 ${this.attempts} 次）`);
    this._reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  _setState(state) {
    this.state = state;
    this.onState(state);
  }
}

/** 由页面地址推导默认控制通道地址（同源 /control）。 */
export function defaultControlUrl() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${location.host}/control`;
}
