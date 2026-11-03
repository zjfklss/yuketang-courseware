"""环境自检：`yuketang-courseware --doctor`。

换一台电脑跑不起来，八成是这几件事之一：Python 太旧、缺图形界面依赖、
没装浏览器、目录不可写。这里一条条查清楚并给出人话提示，不用看堆栈。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from yuketang_courseware import platforms

MIN_PYTHON = (3, 8)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def _python() -> Check:
    version = sys.version_info
    text = f"{version.major}.{version.minor}.{version.micro}（{sys.executable}）"
    if version[:2] < MIN_PYTHON:
        need = ".".join(str(part) for part in MIN_PYTHON)
        return Check("Python", False, f"{text} 太旧，需要 {need} 或更新", True)
    return Check("Python", True, text)


def _qt() -> list[Check]:
    try:
        import PySide6
        from PySide6 import QtWidgets  # noqa: F401
    except ImportError:
        return [Check("PySide6", False, "没装（pip install PySide6-Essentials）")]
    return [Check("PySide6", True, getattr(PySide6, "__version__", ""))]


def _gtk() -> list[Check]:
    try:
        import gi
    except ImportError:
        return [
            Check("PyGObject", False, "没装（sudo apt install python3-gi）"),
            Check("Gtk 4", False, "装不了（缺 python3-gi）"),
            Check("libadwaita", False, "装不了（缺 python3-gi）"),
        ]
    checks = [Check("PyGObject", True, getattr(gi, "__version__", ""))]
    try:
        gi.require_version("Gtk", "4.0")
        from gi.repository import Gtk

        checks.append(
            Check("Gtk 4", True, f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}")
        )
    except (ImportError, ValueError):
        checks.append(Check("Gtk 4", False, "没装（sudo apt install gir1.2-gtk-4.0）"))
    try:
        gi.require_version("Adw", "1")
        from gi.repository import Adw

        checks.append(
            Check(
                "libadwaita",
                True,
                f"{Adw.get_major_version()}.{Adw.get_minor_version()}",
            )
        )
    except (ImportError, ValueError):
        checks.append(
            Check("libadwaita", False, "没装（sudo apt install gir1.2-adw-1）")
        )
    return checks


def _gui() -> list[Check]:
    """Windows 走 Qt，Linux 走 GTK4。"""
    return _qt() if platforms.IS_WINDOWS else _gtk()


def _pillow() -> Check:
    try:
        from PIL import Image  # noqa: F401
    except ImportError:
        return Check("Pillow", False, "没装，只在遇到非 JPEG 页面时才需要", False)
    return Check("Pillow", True, "已安装")


def _browser() -> Check:
    from yuketang_courseware.browser import find_browser

    found = find_browser()
    if not found:
        hint = (
            "没找到 Edge / Chrome（装一个才能登录）"
            if not platforms.IS_WINDOWS
            else "没找到 Microsoft Edge / Google Chrome（装一个才能登录）"
        )
        return Check("浏览器", False, hint, True)
    return Check("浏览器", True, found)


def _writable(name: str, path: Path) -> Check:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        return Check(name, False, f"{path} 不可写")
    return Check(name, True, str(path))


def run() -> list[Check]:
    from yuketang_courseware import __version__, settings

    platforms.configure_console()
    checks = [_python()]
    checks.extend(_gui())
    checks.append(_pillow())
    checks.append(_browser())
    checks.append(_writable("配置目录", settings.PATH.parent))
    checks.append(_writable("缓存目录", platforms.cache_root()))
    print(f"雨课堂课件 {__version__} 环境自检（{platforms.platform_label()}）\n")
    for item in checks:
        mark = "✓" if item.ok else ("×" if item.required else "!")
        level = "" if item.ok else ("（必需）" if item.required else "（可选）")
        print(f"  {mark} {item.name:<12} {item.detail}{level}")
    bad = [item for item in checks if not item.ok and item.required]
    optional = [item for item in checks if not item.ok and not item.required]
    print()
    if bad:
        print("有必需项没通过，按上面括号里的命令补齐再试。")
        return checks
    if optional:
        print("必需项都通过。带 ! 的可选项缺了也能跑，只是遇到对应情况会跳过。")
    else:
        print("全部通过，可以正常使用。")
    return checks


def ok(checks: list[Check]) -> bool:
    return not [item for item in checks if not item.ok and item.required]
