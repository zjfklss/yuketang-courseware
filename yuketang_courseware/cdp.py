"""只连本机 127.0.0.1 的 Chrome DevTools WebSocket。不记录页面内容。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import threading
from urllib.parse import urlparse

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_WAITING = object()
_MAX_MESSAGE = 32 * 1024 * 1024


class CdpError(Exception):
    pass


def encode_client_frame(opcode: int, payload: bytes) -> bytes:
    """客户端帧必须带掩码。这是 WebSocket 协议要求，不是加密。"""
    mask = os.urandom(4)
    length = len(payload)
    header = bytearray([0x80 | (opcode & 0x0F)])
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header += length.to_bytes(2, "big")
    else:
        header.append(0x80 | 127)
        header += length.to_bytes(8, "big")
    header += mask
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes(header) + masked


class FrameParser:
    """解析服务端发来的未分片或分片 WebSocket 帧。"""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._frag = bytearray()
        self._frag_op: int | None = None

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf += data
        messages: list[tuple[int, bytes]] = []
        while True:
            frame = self._pop_frame()
            if frame is None:
                break
            fin, opcode, payload = frame
            if opcode in (0x8, 0x9, 0xA):
                messages.append((opcode, payload))
                continue
            if opcode == 0:
                if self._frag_op is None:
                    continue
                self._frag += payload
                current = self._frag_op
            elif opcode in (1, 2):
                self._frag = bytearray(payload)
                self._frag_op = opcode
                current = opcode
            else:
                continue
            if fin and current is not None:
                messages.append((current, bytes(self._frag)))
                self._frag = bytearray()
                self._frag_op = None
        return messages

    def _pop_frame(self):
        buf = self._buf
        if len(buf) < 2:
            return None
        first, second = buf[0], buf[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        offset = 2
        if length == 126:
            if len(buf) < 4:
                return None
            length = int.from_bytes(buf[2:4], "big")
            offset = 4
        elif length == 127:
            if len(buf) < 10:
                return None
            length = int.from_bytes(buf[2:10], "big")
            offset = 10
        if length > _MAX_MESSAGE:
            raise CdpError("调试消息过长")
        mask_len = 4 if masked else 0
        total = offset + mask_len + length
        if len(buf) < total:
            return None
        payload = bytes(buf[offset + mask_len:total])
        if masked:
            mask = bytes(buf[offset:offset + 4])
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        del buf[:total]
        return fin, opcode, payload


class CdpConnection:
    def __init__(self, ws_url: str) -> None:
        self._sock, initial = _handshake(ws_url)
        self._parser = FrameParser()
        self._initial = initial
        self._alive = True
        self._next = 0
        self._pending: dict[int, object] = {}
        self._cv = threading.Condition()
        self._send_lock = threading.Lock()
        self._reader = threading.Thread(target=self._read_loop, name="cdp", daemon=True)
        self._reader.start()

    @property
    def alive(self) -> bool:
        return self._alive

    def call(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        if not self._alive:
            raise CdpError("调试连接已关闭")
        with self._cv:
            self._next += 1
            message_id = self._next
            self._pending[message_id] = _WAITING
        payload = json.dumps(
            {"id": message_id, "method": method, "params": params or {}},
            ensure_ascii=False,
        )
        try:
            self._send(1, payload.encode("utf-8"))
        except OSError as exc:
            self._fail_all(CdpError("无法向浏览器发送调试命令"))
            raise CdpError("无法向浏览器发送调试命令") from exc
        deadline = _monotonic() + timeout
        with self._cv:
            while self._pending.get(message_id) is _WAITING:
                remaining = deadline - _monotonic()
                if remaining <= 0:
                    self._pending.pop(message_id, None)
                    raise CdpError("浏览器没有及时响应")
                self._cv.wait(remaining)
            result = self._pending.pop(message_id)
        if isinstance(result, Exception):
            raise result
        if not isinstance(result, dict):
            return {}
        return result

    def close(self) -> None:
        self._alive = False
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass

    def _send(self, opcode: int, payload: bytes) -> None:
        frame = encode_client_frame(opcode, payload)
        with self._send_lock:
            self._sock.sendall(frame)

    def _read_loop(self) -> None:
        try:
            if self._initial:
                self._on_messages(self._parser.feed(self._initial))
                self._initial = b""
            while self._alive:
                try:
                    data = self._sock.recv(65536)
                except OSError:
                    self._fail_all(CdpError("和浏览器的调试连接中断"))
                    return
                if not data:
                    self._fail_all(CdpError("和浏览器的调试连接中断"))
                    return
                self._on_messages(self._parser.feed(data))
        except Exception:
            self._fail_all(CdpError("和浏览器的调试连接中断"))

    def _on_messages(self, messages: list[tuple[int, bytes]]) -> None:
        for opcode, payload in messages:
            if opcode == 0x9:
                try:
                    self._send(0xA, payload)
                except OSError:
                    self._fail_all(CdpError("和浏览器的调试连接中断"))
                    return
                continue
            if opcode == 0x8:
                self._fail_all(CdpError("浏览器关闭了调试连接"))
                self.close()
                return
            if opcode not in (1, 2):
                continue
            self._dispatch(payload)

    def _dispatch(self, payload: bytes) -> None:
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        message_id = message.get("id")
        if not isinstance(message_id, int):
            return
        if "error" in message:
            err = message["error"]
            desc = err.get("message") if isinstance(err, dict) else str(err)
            result: object = CdpError(str(desc)[:300])
        else:
            result = message.get("result")
        with self._cv:
            if message_id not in self._pending:
                return
            self._pending[message_id] = result
            self._cv.notify_all()

    def _fail_all(self, exc: Exception) -> None:
        with self._cv:
            for message_id, value in list(self._pending.items()):
                if value is _WAITING:
                    self._pending[message_id] = exc
            self._alive = False
            self._cv.notify_all()


def _monotonic() -> float:
    return __import__("time").monotonic()


def _handshake(ws_url: str) -> tuple[socket.socket, bytes]:
    parsed = urlparse(ws_url)
    if parsed.scheme != "ws" or not parsed.hostname or not parsed.port:
        raise CdpError("调试地址无效")
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise CdpError("只连接本机浏览器")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    try:
        sock = socket.create_connection((parsed.hostname, parsed.port), timeout=10)
    except OSError as exc:
        raise CdpError("连不上浏览器调试端口") from exc
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    host = f"{parsed.hostname}:{parsed.port}"
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            sock.close()
            raise CdpError("浏览器拒绝调试连接")
        data += chunk
        if len(data) > 65536:
            sock.close()
            raise CdpError("浏览器拒绝调试连接")
    head, _, rest = data.partition(b"\r\n\r\n")
    first = head.split(b"\r\n", 1)[0]
    if b" 101 " not in first:
        sock.close()
        raise CdpError("浏览器拒绝调试连接")
    accept = base64.b64encode(hashlib.sha1((key + _GUID).encode("ascii")).digest()).decode("ascii")
    if accept.encode("ascii") not in head:
        sock.close()
        raise CdpError("浏览器拒绝调试连接")
    sock.settimeout(None)
    return sock, bytes(rest)
