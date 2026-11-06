/**
 * 控制协议帧（与 backend/control_channel/protocol.py 严格一致）。
 *
 *   0        1        2        3        4..7
 *   magic    version  type     reserved length(大端 uint32)
 *
 * magic=0xE1、version=1；type: 1=JSON 2=BINARY 3=PING 4=PONG。
 * 负载是 UTF-8 JSON，其中 `kind` 字段区分 input / command。
 */

export const MAGIC = 0xe1;
export const VERSION = 1;
export const HEADER_SIZE = 8;

export const FrameType = Object.freeze({
  JSON: 1,
  BINARY: 2,
  PING: 3,
  PONG: 4,
});

export const EventType = Object.freeze({
  MOUSE_MOVE: 'mouse_move',
  MOUSE_BUTTON: 'mouse_button',
  MOUSE_WHEEL: 'mouse_wheel',
  KEY: 'key',
  TEXT: 'text',
  TOUCH: 'touch',
});

/** 把一条 JSON 消息编码成协议帧。 */
export function encodeFrame(message, frameType = FrameType.JSON) {
  const payload = new TextEncoder().encode(JSON.stringify(message));
  const frame = new Uint8Array(HEADER_SIZE + payload.length);
  frame[0] = MAGIC;
  frame[1] = VERSION;
  frame[2] = frameType;
  frame[3] = 0;
  new DataView(frame.buffer).setUint32(4, payload.length, false);
  frame.set(payload, HEADER_SIZE);
  return frame;
}

/** 流式解帧：WS 的 message 边界不保证等于协议帧边界，必须自己攒。 */
export class FrameDecoder {
  constructor() {
    this.buffer = new Uint8Array(0);
  }

  push(chunk) {
    const merged = new Uint8Array(this.buffer.length + chunk.length);
    merged.set(this.buffer, 0);
    merged.set(chunk, this.buffer.length);
    this.buffer = merged;

    const messages = [];
    while (this.buffer.length >= HEADER_SIZE) {
      if (this.buffer[0] !== MAGIC || this.buffer[1] !== VERSION) {
        // 失步：丢 1 字节重新找魔数，而不是清空（可能只是前面有脏数据）
        this.buffer = this.buffer.slice(1);
        continue;
      }

      const view = new DataView(
        this.buffer.buffer,
        this.buffer.byteOffset,
        this.buffer.byteLength,
      );
      const frameType = this.buffer[2];
      const length = view.getUint32(4, false);
      if (this.buffer.length < HEADER_SIZE + length) break;

      const payload = this.buffer.slice(HEADER_SIZE, HEADER_SIZE + length);
      this.buffer = this.buffer.slice(HEADER_SIZE + length);
      messages.push({ frameType, payload });
    }
    return messages;
  }

  reset() {
    this.buffer = new Uint8Array(0);
  }
}

/** 解析 payload（JSON 帧）。 */
export function decodeJson(payload) {
  const text = new TextDecoder().decode(payload);
  return JSON.parse(text);
}

export function inputMessage(eventType, fields = {}) {
  return { kind: 'input', event_type: eventType, ...fields };
}

export function commandMessage(name, args = {}) {
  return { kind: 'command', name, args };
}

export function mouseMove(x, y) {
  return inputMessage(EventType.MOUSE_MOVE, { x: Math.round(x), y: Math.round(y) });
}

export function mouseButton(x, y, button, pressed) {
  return inputMessage(EventType.MOUSE_BUTTON, {
    x: Math.round(x),
    y: Math.round(y),
    button,
    pressed,
  });
}

export function mouseWheel(x, y, delta) {
  return inputMessage(EventType.MOUSE_WHEEL, {
    x: Math.round(x),
    y: Math.round(y),
    delta,
  });
}

export function keyEvent(keyCode, pressed, modifiers = 0) {
  return inputMessage(EventType.KEY, { key_code: keyCode, pressed, modifiers });
}

export function textEvent(text) {
  return inputMessage(EventType.TEXT, { text });
}

export function touchEvent(x, y, pressed) {
  return inputMessage(EventType.TOUCH, { x: Math.round(x), y: Math.round(y), pressed });
}
