"""平台差异集中在这里：配置/缓存目录、打开文件夹、打包后的资源路径。

以前这些散在各处（`~/.local/share`、`XDG_CACHE_HOME`、`xdg-open`），
Windows 上要么不存在，要么指向错的地方。其他模块只从这里取路径。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "yuketang-courseware"
IS_WINDOWS = os.name == "nt"


def _home() -> Path:
    return Path.home()


def data_root() -> Path:
    """设置文件和独立浏览器配置的根目录。

    显式设了 `XDG_DATA_HOME` 就听它的（Linux 惯例，测试也靠这个隔离）；
    否则 Windows 用 `%APPDATA%`，其他系统用 `~/.local/share`。
    """
    explicit = os.environ.get("XDG_DATA_HOME")
    if explicit:
        return Path(explicit) / APP_NAME
    if IS_WINDOWS:
        base = os.environ.get("APPDATA")
        return (Path(base) if base else _home() / "AppData" / "Roaming") / APP_NAME
    return _home() / ".local" / "share" / APP_NAME


def cache_root() -> Path:
    """可随时删掉的缓存（讲次映射、下载指纹）。"""
    explicit = os.environ.get("XDG_CACHE_HOME")
    if explicit:
        return Path(explicit) / APP_NAME
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA")
        return (Path(base) if base else _home() / "AppData" / "Local") / APP_NAME / "cache"
    return _home() / ".cache" / APP_NAME


def browser_profile() -> Path:
    """本工具独立浏览器配置目录，和日常浏览器互不影响。"""
    return data_root() / "browser"


def open_folder(path) -> None:
    """在文件管理器里打开一个目录。打不开就抛 OSError。"""
    target = str(path)
    if IS_WINDOWS:
        os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606 - 只开目录
        return
    subprocess.Popen(
        ["xdg-open", target],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def resource_path(name: str) -> Path:
    """随程序分发的资源（图标等）：PyInstaller 解包目录优先，其次源码目录。"""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        candidate = Path(bundled) / name
        if candidate.exists():
            return candidate
    return Path(__file__).resolve().parent.parent / name


def configure_console() -> None:
    """Windows 控制台默认 GBK，打 ✓ 之类的符号会 UnicodeEncodeError。"""
    if not IS_WINDOWS:
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def platform_label() -> str:
    """给 `--doctor` 和关于对话框看的一行系统名。"""
    if IS_WINDOWS:
        version = sys.getwindowsversion()
        return f"Windows {version.major}.{version.minor} (build {version.build})"
    try:
        import platform

        return f"{platform.system()} {platform.release()}"
    except Exception:  # pragma: no cover - 极老的系统
        return os.name
