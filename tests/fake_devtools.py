"""假 DevTools 服务端：只实现 /json/version、/json/list 和 Runtime.evaluate。

用来量「我们自己的开销」——找端口、找页面、建连接、WebSocket 收发——
和平台网络快慢无关。不联网、不启动浏览器。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def server_frame(opcode: int, payload: bytes) -> bytes:
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header += struct.pack(">H", length)
    else:
        header.append(127)
        header += struct.pack(">Q", length)
    return bytes(header) + payload


def _read_exact(sock: socket.socket, count: int) -> bytes:
    data = bytearray()
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise ConnectionError("closed")
        data += chunk
    return bytes(data)


def read_client_frame(sock: socket.socket, buf: bytearray) -> tuple[int, bytes]:
    def need(count: int) -> None:
        while len(buf) < count:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            buf.extend(chunk)

    need(2)
    first, second = buf[0], buf[1]
    length = second & 0x7F
    offset = 2
    if length == 126:
        need(4)
        length = int.from_bytes(buf[2:4], "big")
        offset = 4
    elif length == 127:
        need(10)
        length = int.from_bytes(buf[2:10], "big")
        offset = 10
    masked = bool(second & 0x80)
    mask_len = 4 if masked else 0
    need(offset + mask_len + length)
    payload = bytes(buf[offset + mask_len : offset + mask_len + length])
    if masked:
        mask = buf[offset : offset + 4]
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    del buf[: offset + mask_len + length]
    return first & 0x0F, payload


class FakeDevTools:
    """起在 127.0.0.1 的随机端口上，模拟一个已登录的雨课堂页面。"""

    def __init__(self, host: str = "x.yuketang.cn", delay: float = 0.0, router=None) -> None:
        self.host = host
        self.delay = delay          # 模拟平台网络往返
        self.router = router        # path(+query) -> 接口返回的 dict
        self.requests = 0
        self.evaluations = 0
        self.connections = 0
        self.http_hits: list[str] = []
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def origin(self) -> str:
        return f"https://{self.host}"

    @property
    def ws_page(self) -> str:
        return f"ws://127.0.0.1:{self.port}/devtools/page/T1"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _route(self, params: dict) -> dict:
        """从页面脚本里抠出请求地址，交给 router 造返回体。"""
        if self.router is None:
            return {"data": {"ok": True}}
        expression = str(params.get("expression") or "")
        found = re.search(r'fetch\("([^"]+)"', expression)
        if not found:
            return {"data": {"ok": True}}
        path = urlparse(found.group(1)).path
        query = urlparse(found.group(1)).query
        return self.router(path + ("?" + query if query else ""))

    def counts(self) -> dict:
        with self._lock:
            return {
                "http": len(self.http_hits),
                "evaluate": self.evaluations,
                "connections": self.connections,
            }

    def _handler(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                with outer._lock:
                    outer.http_hits.append(path)
                if path == "/json/version":
                    body = json.dumps(
                        {
                            "Browser": "Fake/1.0",
                            "webSocketDebuggerUrl": f"ws://127.0.0.1:{outer.port}/devtools/browser/B1",
                        }
                    ).encode()
                    self._reply(200, body, "application/json")
                    return
                if path == "/json/list":
                    body = json.dumps(
                        [
                            {
                                "id": "T1",
                                "type": "page",
                                "url": f"https://{outer.host}/v2/web/index",
                                "webSocketDebuggerUrl": outer.ws_page,
                            }
                        ]
                    ).encode()
                    self._reply(200, body, "application/json")
                    return
                if path in ("/devtools/page/T1", "/devtools/browser/B1"):
                    self._websocket()
                    return
                self._reply(404, b"{}", "application/json")

            def _reply(self, status: int, body: bytes, kind: str) -> None:
                self.send_response(status)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _websocket(self) -> None:
                key = self.headers.get("Sec-WebSocket-Key", "")
                accept = base64.b64encode(
                    hashlib.sha1((key + GUID).encode("ascii")).digest()
                ).decode("ascii")
                self.send_response(101, "Switching Protocols")
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", accept)
                self.end_headers()
                self.wfile.flush()
                with outer._lock:
                    outer.connections += 1
                send_lock = threading.Lock()

                def send(message: dict) -> None:
                    with send_lock:
                        self.connection.sendall(
                            server_frame(1, json.dumps(message).encode("utf-8"))
                        )

                buf = bytearray()
                self.connection.settimeout(60)
                while True:
                    try:
                        opcode, payload = read_client_frame(self.connection, buf)
                    except (ConnectionError, OSError):
                        return
                    if opcode == 0x8:
                        return
                    if opcode == 0x9:
                        with send_lock:
                            self.connection.sendall(server_frame(0xA, payload))
                        continue
                    if opcode not in (1, 2):
                        continue
                    try:
                        message = json.loads(payload.decode("utf-8"))
                    except ValueError:
                        continue
                    # 浏览器是并发跑求值的：这里也必须并发，否则量出来的耗时是假的
                    threading.Thread(
                        target=self._answer, args=(message, send), daemon=True
                    ).start()

            def _answer(self, message: dict, send) -> None:
                if outer.delay:
                    time.sleep(outer.delay)
                with outer._lock:
                    outer.requests += 1
                method = message.get("method")
                if method == "Runtime.evaluate":
                    with outer._lock:
                        outer.evaluations += 1
                    body = json.dumps(outer._route(message.get("params") or {}))
                    value = json.dumps({"status": 200, "body": body})
                    send(
                        {
                            "id": message.get("id"),
                            "result": {"result": {"type": "string", "value": value}},
                        }
                    )
                else:
                    send({"id": message.get("id"), "result": {}})

        return Handler


def patch_as_browser(module, data_dir):
    """让程序以为本进程就是它自己的浏览器（跳过真实 /proc 进程匹配）。"""
    module._profile_browser_pids = lambda _dir: [os.getpid()]
