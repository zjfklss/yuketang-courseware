"""用独立 user-data-dir 启动系统浏览器。不读取日常浏览器的 Cookie 库。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from yuketang_courseware.cdp import CdpConnection, CdpError
from yuketang_courseware.errors import ApiError, BrowserError, BrowserMissing, LoginRequired
from yuketang_courseware.names import redact

_BROWSERS = (
    "microsoft-edge",
    "microsoft-edge-stable",
    "google-chrome",
    "google-chrome-stable",
    "/opt/microsoft/msedge/msedge",
    "/usr/bin/microsoft-edge",
    "/opt/google/chrome/google-chrome",
    "/usr/bin/google-chrome",
)

_DEFAULT_PROFILE = Path.home() / ".local/share/yuketang-courseware/browser"

# 排查「怎么这么慢」用：YUKETANG_TIMING=1 起程序，每个请求花多久打到 stderr，
# 结束时给一份汇总（共几次请求、累计多少毫秒、最慢的是哪个）。
_TIMING = bool(os.environ.get("YUKETANG_TIMING"))
_TIMING_STATS = {"count": 0, "total_ms": 0.0, "slowest": "", "slowest_ms": 0.0}


def _timing(label: str, began: float) -> None:
    if not _TIMING:
        return
    import sys

    spent = (time.perf_counter() - began) * 1000
    _TIMING_STATS["count"] += 1
    _TIMING_STATS["total_ms"] += spent
    if spent > _TIMING_STATS["slowest_ms"]:
        _TIMING_STATS["slowest"] = label
        _TIMING_STATS["slowest_ms"] = spent
    print(f"[timing] {spent:7.1f}ms  {label[:70]}", file=sys.stderr, flush=True)


def timing_summary() -> str:
    """给界面/命令行看的一句话汇总。没开 YUKETANG_TIMING 时返回空串。"""
    if not _TIMING or not _TIMING_STATS["count"]:
        return ""
    count = _TIMING_STATS["count"]
    total = _TIMING_STATS["total_ms"]
    return (
        f"共 {count} 次接口请求，累计 {total / 1000:.1f} 秒"
        f"（平均 {total / count:.0f} 毫秒/次，最慢 {_TIMING_STATS['slowest_ms']:.0f} 毫秒："
        f"{_TIMING_STATS['slowest'][:60]}）"
    )


def find_browser() -> str | None:
    """启动时再找。先 Edge，后 Chrome。"""
    for candidate in _BROWSERS:
        if os.path.isabs(candidate):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
            continue
        found = shutil.which(candidate)
        if found:
            return found
    return None


class CoursewareBrowser:
    """只操作自己的配置目录。不会向日常 Edge / Chrome 发信号。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        self.data_dir = data_dir or _DEFAULT_PROFILE
        self.port: int | None = None
        self.proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._conn: CdpConnection | None = None
        self._conn_id: str | None = None
        # 页面按主机名缓存：一次登录要发几百个请求，每个都去列标签页太贵
        self._page_cache: dict[str, dict] = {}

    def page_urls(self) -> list[str]:
        """本工具浏览器里各页面的网址。只读，不返回 Cookie。"""
        try:
            pages = self._list_pages()
        except BrowserError:
            return []
        found = []
        for page in pages:
            url = str(page.get("url") or "")
            if url.startswith(("http://", "https://")):
                found.append(url)
        return found

    def alive(self) -> bool:
        """本工具的浏览器还在跑，且调试端口能应答。"""
        try:
            return self._discover_port() is not None
        except BrowserError:
            return False

    def open_url(self, url: str) -> None:
        # 只在「确保浏览器在跑」这段拿锁。_navigate 会走到 _connect，那里还要同一把
        # 锁；非重入锁套着调就是死锁——浏览器已经开着时点第二次「登录」必卡死。
        with self._lock:
            spawned = self._ensure_running(url)
        if not spawned:
            self._navigate(url)

    def fetch_json(
        self,
        origin: str,
        host: str,
        path: str,
        classroom_id: str | None,
        university_id: str | None,
        timeout: float = 90,
    ) -> dict:
        began = time.perf_counter()
        try:
            return self._fetch_json(
                origin, host, path, classroom_id, university_id, timeout
            )
        finally:
            _timing(path, began)

    def _fetch_json(
        self,
        origin: str,
        host: str,
        path: str,
        classroom_id: str | None,
        university_id: str | None,
        timeout: float = 90,
    ) -> dict:
        """在已打开的雨课堂页面里 fetch。Cookie 不离开浏览器。

        页面和调试连接都复用；连接本身按消息 id 分发，允许多个线程同时发请求，
        所以这条路可以被上游并发调用（整理讲次时并发取课件）。
        """
        page = self._cached_page(host)
        if page is None:
            # 缓存没命中才付代价：探端口、列标签页。正常情况下一次登录只做一回。
            if not self._devtools_alive():
                raise BrowserError("请先点「打开并登录」，在浏览器里完成登录后再读取。")
            page = self._find_page(host, wait=3)
            if page is None:
                # 窗口被关掉、但浏览器进程还在：把雨课堂页面开回来再试，登录态还在配置目录里
                self._open_page(origin)
                page = self._find_page(host, wait=15)
            if page is None:
                raise LoginRequired()
            self._remember_page(host, page)
        script = _js_fetch(origin, path, classroom_id, university_id)
        try:
            raw = self._evaluate(page, script, await_promise=True, timeout=timeout)
        except (CdpError, BrowserError) as exc:
            # 浏览器会把后台标签页冻结，或在窗口关掉后让旧连接失效。
            # 唤醒页面、丢掉旧连接，再试一次；还不行就明确报“页面没有响应”。
            if not self._recover(host):
                raise BrowserError(
                    "浏览器页面没有响应。请点「登录雨课堂」把这个窗口重新打开一次。"
                ) from exc
            page = self._find_page(host, wait=5)
            if page is None:
                raise LoginRequired() from None
            self._remember_page(host, page)
            raw = self._evaluate(page, script, await_promise=True, timeout=timeout)
        if not isinstance(raw, str):
            raise ApiError("接口没有返回数据")
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            raise ApiError("接口没有返回数据")
        status = envelope.get("status") if isinstance(envelope, dict) else None
        body = envelope.get("body") if isinstance(envelope, dict) else None
        if not isinstance(status, int) or not isinstance(body, str):
            raise ApiError("接口没有返回数据")
        return parse_api_body(status, body)

    def terminate_profile(self) -> None:
        """结束打开了这一配置目录的浏览器及其子进程。不碰其他配置目录。"""
        marker = str(self.data_dir)
        if not marker or marker in ("/", "/tmp", str(Path.home())):
            return
        roots = set(_profile_browser_pids(self.data_dir))
        pids = roots | _descendants(roots)
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        time.sleep(0.4)
        for pid in pids:
            try:
                os.kill(pid, 0)
            except OSError:
                continue
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _ensure_running(self, url: str) -> bool:
        if self._discover_port():
            return False
        if _profile_browser_pids(self.data_dir):
            raise BrowserError(
                "本工具的登录浏览器已在运行，但调试端口没有响应。"
                "请关掉本工具打开的那个窗口后重试。不要关闭平时使用的浏览器。"
            )
        exe = find_browser()
        if not exe:
            raise BrowserMissing("未找到 microsoft-edge 或 google-chrome。请先安装其中之一。")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        cmd = [
            exe,
            f"--user-data-dir={self.data_dir}",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            # 我们的窗口常被切到后台；不关掉这些节流/冻结，页面会冻住导致调试求值挂死
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--new-window",
            url,
        ]
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd="/tmp",
        )
        self.port = port
        self._wait_until_up()
        return True

    def _wait_until_up(self) -> None:
        deadline = time.time() + 20
        while time.time() < deadline:
            if self.proc and self.proc.poll() is not None:
                discovered = self._discover_port()
                if discovered and self._version_ok(discovered):
                    self.port = discovered
                    return
                raise BrowserError("浏览器进程已退出，没有打开独立窗口")
            if self.port and self._version_ok(self.port):
                return
            time.sleep(0.15)
        raise BrowserError("浏览器调试端口没有就绪")

    def _devtools_alive(self) -> bool:
        return self._discover_port() is not None

    def _version_ok(self, port: int) -> bool:
        try:
            info = self._http_json("/json/version", port)
        except BrowserError:
            return False
        if isinstance(info, dict) and info.get("webSocketDebuggerUrl"):
            self.port = port
            return True
        return False

    def _discover_port(self) -> int | None:
        for port in _listening_ports(self.data_dir):
            if self._version_ok(port):
                return port
        return None

    def _http_json(self, path: str, port: int | None = None):
        use = self.port if port is None else port
        if not use:
            raise BrowserError("浏览器还没有打开")
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{use}{path}", timeout=3
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise BrowserError("读不到浏览器调试端口")

    def _list_pages(self) -> list[dict]:
        data = self._http_json("/json/list")
        if not isinstance(data, list):
            return []
        pages = []
        for item in data:
            if (
                isinstance(item, dict)
                and item.get("type") == "page"
                and item.get("webSocketDebuggerUrl")
            ):
                pages.append(item)
        return pages

    def _cached_page(self, host: str) -> dict | None:
        """缓存里的页面，且调试连接还活着、还挂在同一个 target 上，才敢直接用。

        一次登录要发几百个请求；每个都去列一遍标签页、扫一遍 /proc 找端口，
        在真机上就是几十秒的纯等待。
        """
        page = self._page_cache.get(host)
        if not page:
            return None
        if self._conn is None or not self._conn.alive:
            return None
        if self._conn_id != str(page.get("id") or ""):
            return None
        return page

    def _remember_page(self, host: str, page: dict) -> None:
        with self._lock:
            self._page_cache[host] = page

    def _find_page(self, host: str, wait: float) -> dict | None:
        deadline = time.time() + wait
        want = host.lower()
        while True:
            try:
                pages = self._list_pages()
            except BrowserError:
                pages = []
            for page in pages:
                hostname = _hostname(str(page.get("url") or ""))
                if hostname == want:
                    return page
            if time.time() >= deadline:
                return None
            time.sleep(0.25)

    def _navigate(self, url: str) -> None:
        try:
            pages = self._list_pages()
        except BrowserError as exc:
            raise BrowserError("没能在独立浏览器里打开网址") from exc
        if not pages:
            self._create_page(url)
            return
        page = _pick_navigable(pages)
        conn = self._connect(page)
        try:
            conn.call("Page.navigate", {"url": url}, timeout=20)
        except CdpError:
            raise BrowserError("没能在独立浏览器里打开网址") from None

    def _open_page(self, url: str) -> None:
        """在浏览器里新开一个标签页，并等它真的能跑脚本。"""
        try:
            self._create_page(url)
        except (BrowserError, CdpError):
            return
        want = _hostname(url)
        deadline = time.time() + 20
        while time.time() < deadline:
            page = self._find_page(want, wait=1)
            if page is not None:
                try:
                    conn = self._connect(page)
                    reply = conn.call(
                        "Runtime.evaluate",
                        {"expression": "document.readyState", "returnByValue": True},
                        timeout=5,
                    )
                    value = (reply.get("result") or {}).get("value")
                    if value in ("interactive", "complete"):
                        return
                except (CdpError, BrowserError):
                    pass
            time.sleep(0.3)

    def _create_page(self, url: str) -> None:
        info = self._http_json("/json/version")
        ws_url = info.get("webSocketDebuggerUrl") if isinstance(info, dict) else None
        if not isinstance(ws_url, str):
            raise BrowserError("没能在独立浏览器里打开网址")
        conn = CdpConnection(ws_url)
        try:
            conn.call("Target.createTarget", {"url": url}, timeout=15)
        except CdpError:
            raise BrowserError("没能在独立浏览器里打开网址") from None
        finally:
            conn.close()

    def _connect(self, page: dict) -> CdpConnection:
        """只在建连接时加锁；连接建立后允许并发发命令。"""
        target = str(page.get("id") or "")
        ws_url = str(page.get("webSocketDebuggerUrl") or "")
        if not ws_url.startswith("ws://127.0.0.1") and not ws_url.startswith("ws://localhost"):
            raise BrowserError("浏览器调试地址无效")
        with self._lock:
            if self._conn is not None and self._conn_id == target and self._conn.alive:
                return self._conn
            if self._conn is not None and self._conn_id != target:
                self._conn.close()
                self._conn = None
            if self._conn is None:
                self._conn = CdpConnection(ws_url)
                self._conn_id = target
            return self._conn

    def _recover(self, host: str) -> bool:
        """唤醒被冻结的标签页并重建调试连接。返回是否还有可用的调试端口。"""
        page = self._find_page(host, wait=1)
        if page is not None:
            self._wake(page)
        self._drop_connection()
        return self._devtools_alive()

    def _wake(self, page: dict) -> None:
        """尽量把后台标签页叫醒：前置 + 生命周期设为 active。"""
        try:
            conn = self._connect(page)
        except (BrowserError, CdpError):
            return
        for method, params in (
            ("Page.bringToFront", {}),
            ("Page.setWebLifecycleState", {"state": "active"}),
        ):
            try:
                conn.call(method, params, timeout=5)
            except (CdpError, BrowserError):
                continue
        try:
            info = self._http_json("/json/version")
            ws_url = info.get("webSocketDebuggerUrl") if isinstance(info, dict) else None
            target = str(page.get("id") or "")
            if isinstance(ws_url, str) and target:
                browser_conn = CdpConnection(ws_url)
                try:
                    browser_conn.call("Target.activateTarget", {"targetId": target}, timeout=5)
                finally:
                    browser_conn.close()
        except (BrowserError, CdpError):
            pass

    def _drop_connection(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
            self._conn = None
            self._conn_id = None
            self._page_cache.clear()

    def _evaluate(self, page: dict, expression: str, await_promise: bool, timeout: float):
        conn = self._connect(page)  # CdpConnection.call 自身按 id 分发，可并发
        try:
            result = conn.call(
                "Runtime.evaluate",
                {
                    "expression": expression,
                    "awaitPromise": await_promise,
                    "returnByValue": True,
                    "timeout": int(timeout * 1000),
                },
                timeout=timeout + 5,
            )
        except CdpError:
            raise BrowserError("浏览器调试失败") from None
        details = result.get("exceptionDetails")
        if isinstance(details, dict):
            raise ApiError(_exception_text(details))
        remote = result.get("result")
        if not isinstance(remote, dict):
            raise ApiError("页面脚本失败")
        if remote.get("subtype") == "error":
            raise ApiError(redact(str(remote.get("description") or "页面脚本失败"))[:300])
        return remote.get("value")


def parse_api_body(status: int, body: str) -> dict:
    """只解析 JSON。HTML 登录页和正文都不进入错误信息。"""
    stripped = body.lstrip()
    lower = stripped[:20].lower()
    if stripped.startswith("<") or lower.startswith("<!doctype"):
        raise LoginRequired()
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        raise ApiError(f"接口返回无法解析（HTTP {status}）")
    if not isinstance(data, dict):
        raise ApiError(f"接口返回无法解析（HTTP {status}）")
    if status in (401, 403):
        raise LoginRequired()
    message = str(data.get("errmsg") or data.get("msg") or "")
    failed = False
    if "errcode" in data and data.get("errcode") not in (0, "0", None):
        failed = True
    if "code" in data and data.get("code") not in (0, "0", None):
        failed = True
    if failed:
        if any(word in message.lower() for word in ("登录", "登陆", "未登录", "login")):
            raise LoginRequired()
        raise ApiError((message or f"接口返回错误（HTTP {status}）")[:300])
    return data


def _js_fetch(
    origin: str,
    path: str,
    classroom_id: str | None,
    university_id: str | None,
) -> str:
    if not path.startswith("/"):
        path = "/" + path
    headers = {"xtbz": "ykt", "x-client": "web", "terminal-type": "web"}
    if classroom_id:
        headers["classroom-id"] = str(classroom_id)
        headers["classroom_id"] = str(classroom_id)
    if university_id:
        headers["university-id"] = str(university_id)
    url = origin + path
    # 令牌只在页面里读，随请求头发走，不作为返回值。
    # 页面刚打开时可能还停在空白页/错误页：那时读 document.cookie 会抛
    # SecurityError，fetch 也会失败。等到页面落到真实站点再发请求，别把
    # 一句原始 JS 异常甩到界面上。
    return (
        "(async () => {\n"
        f"  const headers = {json.dumps(headers, ensure_ascii=False)};\n"
        "  const sleep = (ms) => new Promise((done) => setTimeout(done, ms));\n"
        "  const deadline = Date.now() + 20000;\n"
        "  for (;;) {\n"
        "    let cookie = '';\n"
        "    try { cookie = document.cookie || ''; } catch (e) { cookie = ''; }\n"
        "    const sent = Object.assign({}, headers);\n"
        "    for (const part of cookie.split('; ')) {\n"
        "      if (part.startsWith('csrftoken=')) {\n"
        "        try { sent['X-CSRFToken'] = decodeURIComponent(part.slice(10)); } catch (e) {}\n"
        "        break;\n"
        "      }\n"
        "    }\n"
        f"    try {{\n"
        f"      const response = await fetch({json.dumps(url)}, {{ credentials: 'include', headers: sent }});\n"
        "      const body = await response.text();\n"
        "      return JSON.stringify({ status: response.status, body });\n"
        "    } catch (error) {\n"
        "      if (Date.now() > deadline) throw error;\n"
        "      await sleep(500);\n"
        "    }\n"
        "  }\n"
        "})()"
    )


def _exception_text(details: dict) -> str:
    exc = details.get("exception")
    if isinstance(exc, dict) and exc.get("description"):
        text = str(exc.get("description"))
    else:
        text = str(details.get("text") or "页面脚本失败")
    return redact(text)[:300]


def _hostname(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url).hostname or "").lower()


def _pick_navigable(pages: list[dict]) -> dict:
    for page in pages:
        url = str(page.get("url") or "")
        if url.startswith(("devtools://", "chrome://", "edge://", "chrome-extension://")):
            continue
        return page
    return pages[0]


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_browser_exe(exe: str) -> bool:
    name = Path(exe).name
    return "msedge" in name or name in {
        "chrome",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    }


def _profile_browser_pids(data_dir: Path) -> list[int]:
    """exe 是浏览器，并且打开了这个配置目录里的文件。"""
    marker = str(data_dir)
    found = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return found
    for pid_dir in entries:
        if not pid_dir.name.isdigit():
            continue
        try:
            exe = os.readlink(pid_dir / "exe")
        except OSError:
            continue
        if not _is_browser_exe(exe):
            continue
        if _has_fd_in(pid_dir, marker):
            found.append(int(pid_dir.name))
    return found


def _has_fd_in(pid_dir: Path, marker: str) -> bool:
    try:
        fds = list((pid_dir / "fd").iterdir())
    except OSError:
        return False
    for fd in fds:
        try:
            target = os.readlink(fd)
        except OSError:
            continue
        if target == marker or target.startswith(marker + os.sep):
            return True
    return False


def _listening_ports(data_dir: Path) -> list[int]:
    """本配置目录的浏览器在 127.0.0.1 上监听的端口。"""
    listeners = _local_listeners()
    ports: list[int] = []
    for pid in _profile_browser_pids(data_dir):
        try:
            fds = list((Path("/proc") / str(pid) / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if not (target.startswith("socket:[") and target.endswith("]")):
                continue
            port = listeners.get(target[8:-1])
            if port and port not in ports:
                ports.append(port)
    return ports


def _local_listeners() -> dict[str, int]:
    found: dict[str, int] = {}
    specs = (
        ("tcp", {"0100007F"}),
        ("tcp6", {"0000000000000000000000000100007F", "0000000000000000FFFF00000100007F"}),
    )
    for name, loopback in specs:
        try:
            lines = Path(f"/proc/net/{name}").read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "0A":
                continue
            address, port_hex = parts[1].split(":")
            if address not in loopback:
                continue
            found[parts[9]] = int(port_hex, 16)
    return found


def _descendants(roots: set[int]) -> set[int]:
    children: dict[int, list[int]] = {}
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return set()
    for pid_dir in entries:
        if not pid_dir.name.isdigit():
            continue
        try:
            stat = (pid_dir / "stat").read_text()
        except OSError:
            continue
        tail = stat.rsplit(")", 1)
        if len(tail) != 2:
            continue
        fields = tail[1].split()
        if len(fields) < 2:
            continue
        children.setdefault(int(fields[1]), []).append(int(pid_dir.name))
    found: set[int] = set()
    stack = list(roots)
    while stack:
        pid = stack.pop()
        for child in children.get(pid, []):
            if child in found or child in roots:
                continue
            found.add(child)
            stack.append(child)
    return found
