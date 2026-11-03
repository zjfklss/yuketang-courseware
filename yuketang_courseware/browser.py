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

from yuketang_courseware import platforms
from yuketang_courseware.cdp import CdpConnection, CdpError
from yuketang_courseware.errors import ApiError, BrowserError, BrowserMissing, LoginRequired
from yuketang_courseware.names import redact

_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW：起浏览器时别弹一个黑框


def _extra_flags() -> list[str]:
    """排查用的附加启动参数：YUKETANG_BROWSER_FLAGS="--headless=new --no-sandbox"。

    正常使用不需要；某些受限环境（无桌面、容器、杀软拦截 Chromium 沙箱）里，
    靠它把浏览器跑起来。只按空格切分，不支持带空格的参数值。
    """
    raw = os.environ.get("YUKETANG_BROWSER_FLAGS", "").strip()
    return raw.split() if raw else []


def _browser_candidates() -> list[str]:
    """可能安装浏览器的地方。Windows 上 Edge 一定在，Chrome 看用户装没装。"""
    if platforms.IS_WINDOWS:
        found: list[str] = []
        for root in (
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("ProgramFiles"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if root:
                found.append(os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"))
        for root in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if root:
                found.append(os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"))
        # 有人把浏览器装进 PATH 或者用便携版
        found.extend(("msedge.exe", "chrome.exe"))
        return found
    return [
        "microsoft-edge",
        "microsoft-edge-stable",
        "google-chrome",
        "google-chrome-stable",
        "/opt/microsoft/msedge/msedge",
        "/usr/bin/microsoft-edge",
        "/opt/google/chrome/google-chrome",
        "/usr/bin/google-chrome",
    ]


_DEFAULT_PROFILE = platforms.browser_profile()


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
    """启动时再找。Windows 上 Edge 一定有，Chrome 看装没装。"""
    for candidate in _browser_candidates():
        if os.path.isabs(candidate):
            if os.path.isfile(candidate) and (
                platforms.IS_WINDOWS or os.access(candidate, os.X_OK)
            ):
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
        self._recover_lock = threading.Lock()
        # 限制同时塞进同一个 DevTools 连接的 evaluate 数，避免整理讲次时把 Edge 打挂
        self._eval_slots = threading.Semaphore(2)
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
            # 多门课并发时只允许一个人 recover，避免互相拆掉对方的连接像卡死。
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
        if not marker or marker.rstrip("\\/") in ("", "/tmp", str(Path.home())):
            return
        roots = set(_profile_browser_pids(self.data_dir))
        if platforms.IS_WINDOWS:
            # taskkill /T 会把渲染进程一起收掉，比逐个 kill 稳
            for pid in roots:
                _run_hidden(["taskkill", "/PID", str(pid), "/T", "/F"])
        else:
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
        try:
            (self.data_dir / "yuketang-port").unlink()
        except OSError:
            pass
        self.port = None

    def _ensure_running(self, url: str) -> bool:
        if self._discover_port():
            return False
        if _profile_browser_pids(self.data_dir):
            # 上一次起的浏览器还占着这个配置目录（窗口关了、后台残留），调试端口没了。
            # 这是本工具自己的隔离配置，不碰日常浏览器：收掉重开，登录态还在磁盘上。
            self.terminate_profile()
            if not _wait_pids_gone(self.data_dir, timeout=3.0):
                raise BrowserError(
                    "本工具的登录浏览器还在运行，调试端口没有响应。"
                    "请用右上角菜单的「关闭登录窗口」再试一次。"
                )
        exe = find_browser()
        if not exe:
            raise BrowserMissing(
                "未找到 Microsoft Edge 或 Google Chrome。Windows 10/11 自带 Edge，"
                "如果没有，请装一个再试。"
                if platforms.IS_WINDOWS
                else "未找到 microsoft-edge 或 google-chrome。请先安装其中之一。"
            )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        port = _free_port()
        extra = _extra_flags()
        # headless 模式不认 --new-window：带上它浏览器会直接退出（真机踩过）
        headless = any(flag.startswith("--headless") for flag in extra)
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
            *extra,
            *([] if headless else ["--new-window"]),
            url,
        ]
        self.proc = subprocess.Popen(cmd, **_spawn_kwargs(self.data_dir))
        self.port = port
        self._wait_until_up()
        return True

    def _wait_until_up(self) -> None:
        deadline = time.time() + 20
        saw_exit = False
        while time.time() < deadline:
            # 配置目录已被占用时，新起的 Edge 会马上退出并把 URL 交给已有进程。
            # 这时不能立刻报错，要继续找调试端口（可能是上一次留下的窗口）。
            if self.proc and self.proc.poll() is not None:
                saw_exit = True
                discovered = self._discover_port()
                if discovered and self._version_ok(discovered):
                    self.port = discovered
                    return
            elif self.port and self._version_ok(self.port):
                return
            time.sleep(0.15)
        if saw_exit:
            raise BrowserError("浏览器进程已退出，没有打开独立窗口")
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
            self._remember_port(port)
            return True
        return False

    def _remember_port(self, port: int) -> None:
        """把调试端口记到配置目录里：Edge 新版本不一定写 DevToolsActivePort，
        下次启动/另一个进程就靠这个文件接上还开着的登录窗口。"""
        try:
            (self.data_dir / "yuketang-port").write_text(str(port), encoding="utf-8")
        except OSError:
            pass

    def _discover_port(self) -> int | None:
        if platforms.IS_WINDOWS:
            known = self._windows_ports()
            for port in known:
                if self._version_ok(port):
                    return port
            # 端口文件没写（或程序重启过、self.port 丢了）：从浏览器命令行里把端口捞出来
            for _pid, port in _windows_browser_processes(self.data_dir):
                if port and port not in known and self._version_ok(port):
                    return port
            return None
        for port in _listening_ports(self.data_dir):
            if self._version_ok(port):
                return port
        return None

    def _windows_ports(self) -> list[int]:
        """Windows 没有 /proc：先看自己起的端口，再读浏览器/程序留下的端口文件。"""
        ports: list[int] = []
        if self.port:
            ports.append(self.port)
        for name in ("yuketang-port", "DevToolsActivePort"):
            try:
                lines = (self.data_dir / name).read_text(
                    encoding="utf-8", errors="ignore"
                ).splitlines()
            except OSError:
                continue
            if lines and lines[0].strip().isdigit():
                port = int(lines[0].strip())
                if port not in ports:
                    ports.append(port)
        return ports

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
        with self._recover_lock:
            # 别人刚恢复成功：端口和连接都还在，别再拆一次
            if self._conn is not None and self._conn.alive and self._devtools_alive():
                return True
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
        # 槽位卡住时别无限等：超时后让上层走 recover / 报错，避免「整理讲次」假死
        if not self._eval_slots.acquire(timeout=max(5.0, timeout)):
            raise BrowserError("浏览器调试排队超时")
        try:
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
        finally:
            self._eval_slots.release()


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
    name = Path(exe).name.lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return "msedge" in name or name in {
        "chrome",
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    }


def _spawn_kwargs(data_dir: Path) -> dict:
    """起浏览器用的进程参数。

    Windows 上要 CREATE_NO_WINDOW（别弹控制台窗口），也不认 start_new_session；
    cwd 指到配置目录，别落在程序自己的目录里把文件锁住。
    """
    options: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if platforms.IS_WINDOWS:
        options["creationflags"] = _NO_WINDOW
        options["cwd"] = str(data_dir)
    else:
        options["start_new_session"] = True
        options["cwd"] = "/tmp"
    return options


def _run_hidden(
    cmd: list[str], timeout: float = 25, env: dict | None = None
) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
            creationflags=_NO_WINDOW if platforms.IS_WINDOWS else 0,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _profile_browser_pids(data_dir: Path) -> list[int]:
    """exe 是浏览器，并且打开了这个配置目录里的文件。"""
    if platforms.IS_WINDOWS:
        return _windows_profile_browser_pids(data_dir)
    return _linux_profile_browser_pids(data_dir)


def _windows_browser_processes(data_dir: Path) -> list[tuple[int, int | None]]:
    """Windows 上的「我们自己的浏览器进程」。

    优先用 psutil（纯 Python，打包后也稳）；没有 psutil 时退回 PowerShell/WMI。
    """
    try:
        import psutil  # noqa: F401
    except ImportError:
        return _powershell_browser_processes(data_dir)
    return _psutil_browser_processes(data_dir)


def _psutil_browser_processes(data_dir: Path) -> list[tuple[int, int | None]]:
    import psutil

    marker = os.path.normcase(str(data_dir))
    if not marker:
        return []
    wanted = {"msedge.exe", "chrome.exe"}
    found: list[tuple[int, int | None]] = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            info = proc.info
            if (info.get("name") or "").lower() not in wanted:
                continue
            cmdline = info.get("cmdline") or []
            if marker not in os.path.normcase(" ".join(cmdline)):
                continue
            port = None
            for part in cmdline:
                if part.startswith("--remote-debugging-port="):
                    value = part.split("=", 1)[1].strip()
                    if value.isdigit():
                        port = int(value)
            found.append((int(info["pid"]), port))
        except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError, ValueError):
            continue
    return found


def _powershell_browser_processes(data_dir: Path) -> list[tuple[int, int | None]]:
    """PowerShell 后备实现：问 WMI 要命令行里带着我们配置目录的浏览器进程。

    返回 (pid, 命令行里的调试端口或 None)。端口只有主进程有。
    """
    marker = str(data_dir)
    if not marker:
        return []
    script = (
        "$p = $env:YKT_PROFILE; "
        "if (-not $p) { return }; "
        "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe' OR Name='chrome.exe'\" "
        "-ErrorAction SilentlyContinue | "
        "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($p) } | "
        "ForEach-Object { "
        "$m = [regex]::Match($_.CommandLine, '--remote-debugging-port=(\\d+)'); "
        "if ($m.Success) { \"$($_.ProcessId)|$($m.Groups[1].Value)\" } "
        "else { \"$($_.ProcessId)|\" } "
        "}"
    )
    completed = _run_hidden(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
        ],
        env={**os.environ, "YKT_PROFILE": marker},
    )
    if completed is None:
        return []
    found: list[tuple[int, int | None]] = []
    for line in completed.stdout.decode("ascii", "ignore").splitlines():
        pid_text, _, port_text = line.partition("|")
        pid_text = pid_text.strip()
        if not pid_text.isdigit():
            continue
        port_text = port_text.strip()
        found.append((int(pid_text), int(port_text) if port_text.isdigit() else None))
    return found


def _windows_profile_browser_pids(data_dir: Path) -> list[int]:
    return [pid for pid, _port in _windows_browser_processes(data_dir)]


def _wait_pids_gone(data_dir: Path, timeout: float) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while True:
        if not _profile_browser_pids(data_dir):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(0.2)


def _linux_profile_browser_pids(data_dir: Path) -> list[int]:
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
    """本配置目录的浏览器在 127.0.0.1 上监听的端口（Linux 专用）。"""
    if platforms.IS_WINDOWS:
        return []
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
    if platforms.IS_WINDOWS:
        return set()
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
