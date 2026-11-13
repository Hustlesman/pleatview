// A small WebSocket client (RFC 6455) on Node's tls, for protocol v2 (docs/protocol-v2.md):
// JSON in text frames, pings answered, messages up to 1 MiB, and the server's certificate
// either trusted by the system or pinned. Its frame functions serve the tests' server too.

import { createHash, randomBytes } from "node:crypto";
import { EventEmitter } from "node:events";
import { connect as tlsConnect } from "node:tls";

export const MAX_MESSAGE = 1 << 20;
const GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
export const OP = { CONTINUATION: 0x0, TEXT: 0x1, BINARY: 0x2, CLOSE: 0x8, PING: 0x9, PONG: 0xa };

export const acceptKey = (key) => createHash("sha1").update(key + GUID).digest("base64");

/** One final frame. Clients mask what they send; servers don't. */
export function encode(opcode, payload, { mask }) {
  const data = Buffer.isBuffer(payload) ? payload : Buffer.from(payload);
  const length = data.length;
  const extra = length < 126 ? 0 : length < 65536 ? 2 : 8;
  const head = Buffer.alloc(2 + extra + (mask ? 4 : 0));
  head[0] = 0x80 | opcode;
  head[1] = (mask ? 0x80 : 0) | (length < 126 ? length : length < 65536 ? 126 : 127);
  if (extra === 2) head.writeUInt16BE(length, 2);
  if (extra === 8) head.writeBigUInt64BE(BigInt(length), 2);
  if (!mask) return Buffer.concat([head, data]);
  const key = randomBytes(4);
  key.copy(head, 2 + extra);
  const out = Buffer.alloc(length);
  for (let i = 0; i < length; i++) out[i] = data[i] ^ key[i % 4];
  return Buffer.concat([head, out]);
}

/** Reads frames off a byte stream: feed() it data, take frames as {final, opcode, payload}. */
export class FrameReader {
  constructor() {
    this.buffer = Buffer.alloc(0);
  }

  feed(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
  }

  next() {
    const b = this.buffer;
    if (b.length < 2) return null;
    let length = b[1] & 0x7f;
    let offset = 2;
    if (length === 126) {
      if (b.length < 4) return null;
      length = b.readUInt16BE(2);
      offset = 4;
    } else if (length === 127) {
      if (b.length < 10) return null;
      length = Number(b.readBigUInt64BE(2));
      offset = 10;
    }
    if (length > MAX_MESSAGE) throw new Error("message too big");
    const masked = (b[1] & 0x80) !== 0;
    const total = offset + (masked ? 4 : 0) + length;
    if (b.length < total) return null;
    let payload = b.subarray(offset + (masked ? 4 : 0), total);
    if (masked) {
      const key = b.subarray(offset, offset + 4);
      payload = Buffer.from(payload.map((byte, i) => byte ^ key[i % 4]));
    }
    this.buffer = b.subarray(total);
    return { final: (b[0] & 0x80) !== 0, opcode: b[0] & 0x0f, payload: Buffer.from(payload) };
  }
}

/** An open connection: emits "message" (a JSON object) and "close" (code). */
export class WebSocket extends EventEmitter {
  constructor(socket, rest) {
    super();
    this.socket = socket;
    this.reader = new FrameReader();
    this.parts = [];
    this.closeCode = null;
    this.closed = false;
    socket.on("data", (chunk) => this.receive(chunk));
    socket.on("close", () => this.finish());
    socket.on("error", () => this.finish());
    if (rest.length) this.receive(rest);
  }

  send(value) {
    if (this.closed) throw new Error("closed");
    this.socket.write(encode(OP.TEXT, JSON.stringify(value), { mask: true }));
  }

  close(code = 1000) {
    if (this.closed) return;
    const payload = Buffer.alloc(2);
    payload.writeUInt16BE(code);
    try {
      this.socket.write(encode(OP.CLOSE, payload, { mask: true }));
    } catch {
      // gone already
    }
    this.socket.end();
    this.finish();
  }

  receive(chunk) {
    try {
      this.reader.feed(chunk);
      for (let frame = this.reader.next(); frame; frame = this.reader.next()) this.frame(frame);
    } catch {
      this.closeCode = 1009;
      this.close(1009);
    }
  }

  frame({ final, opcode, payload }) {
    if (opcode === OP.PING) {
      if (!this.closed) this.socket.write(encode(OP.PONG, payload, { mask: true }));
      return;
    }
    if (opcode === OP.PONG) return;
    if (opcode === OP.CLOSE) {
      this.closeCode = payload.length >= 2 ? payload.readUInt16BE(0) : null;
      this.close(this.closeCode ?? 1000);
      return;
    }
    this.parts.push(payload);
    if (this.parts.reduce((n, p) => n + p.length, 0) > MAX_MESSAGE) throw new Error("too big");
    if (!final) return;
    const text = Buffer.concat(this.parts).toString("utf8");
    this.parts = [];
    let message;
    try {
      message = JSON.parse(text);
    } catch {
      message = null;
    }
    if (message && typeof message === "object" && !Array.isArray(message)) this.emit("message", message);
  }

  finish() {
    if (this.closed) return;
    this.closed = true;
    this.socket.destroy();
    this.emit("close", this.closeCode);
  }
}

/**
 * Opens a wss:// connection. With `pin` (hex of the SHA-256 of the server's certificate), only
 * that certificate is accepted; otherwise the system's authorities decide.
 */
export function connect(address, { subprotocol, pin = null, timeoutMs = 15000 }) {
  const url = new URL(address);
  if (url.protocol !== "wss:") return Promise.reject(new Error("only wss:// addresses"));
  const host = url.hostname.replace(/^\[|\]$/g, "");
  return new Promise((resolve, reject) => {
    // The system's authorities and the host name decide, unless the controller's own certificate
    // is pinned: then its SHA-256 is checked below, before a single byte is sent, which a
    // self-signed certificate can't pass any other way in Node.
    const options = {
      host,
      port: Number(url.port) || 443,
      minVersion: "TLSv1.2",
      ALPNProtocols: ["http/1.1"],
      rejectUnauthorized: !pin,
      ...(pin ? { checkServerIdentity: () => undefined } : {}),
    };
    if (!/^[\d.:]+$/.test(host)) options.servername = host;
    const socket = tlsConnect(options);
    const fail = (error) => {
      socket.destroy();
      reject(error);
    };
    const timer = setTimeout(() => fail(new Error("timed out")), timeoutMs);
    socket.once("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    socket.once("secureConnect", () => {
      if (pin) {
        const der = socket.getPeerX509Certificate()?.raw;
        if (!der || createHash("sha256").update(der).digest("hex") !== pin.toLowerCase()) {
          clearTimeout(timer);
          return fail(new Error("the server's certificate isn't the one pinned"));
        }
      }
      const key = randomBytes(16).toString("base64");
      socket.write(`GET ${url.pathname || "/"} HTTP/1.1\r\nHost: ${url.host}\r\nUpgrade: websocket\r\n` +
        `Connection: Upgrade\r\nSec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n` +
        `Sec-WebSocket-Protocol: ${subprotocol}\r\n\r\n`);
      let head = Buffer.alloc(0);
      const onData = (chunk) => {
        head = Buffer.concat([head, chunk]);
        const end = head.indexOf("\r\n\r\n");
        if (end < 0) {
          if (head.length > 16384) fail(new Error("not a WebSocket"));
          return;
        }
        socket.off("data", onData);
        clearTimeout(timer);
        const lines = head.subarray(0, end).toString("latin1").split("\r\n");
        const headers = Object.fromEntries(lines.slice(1).map((line) => {
          const at = line.indexOf(":");
          return [line.slice(0, at).trim().toLowerCase(), line.slice(at + 1).trim()];
        }));
        if (!/^HTTP\/1\.1 101\b/.test(lines[0]) || headers["sec-websocket-accept"] !== acceptKey(key)) {
          return fail(new Error(`not a WebSocket: ${lines[0]}`));
        }
        if (headers["sec-websocket-protocol"] !== subprotocol) return fail(new Error(`the server doesn't speak ${subprotocol}`));
        socket.removeAllListeners("error");
        resolve(new WebSocket(socket, head.subarray(end + 4)));
      };
      socket.on("data", onData);
    });
  });
}
