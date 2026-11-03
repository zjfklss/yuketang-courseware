"""图形界面入口：Windows 用 Qt，Linux 用 GTK4。

两个后端共用同一套 catalog / library / slides 逻辑，只是窗口壳不同：
`app_qt.py`（PySide6，Windows 及没装 GTK 的环境）和 `app_gtk.py`
（GTK4 + libadwaita，Linux 原生外观，走系统包管理装依赖）。
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    if os.name == "nt":
        return _qt()
    try:
        from yuketang_courseware.app_gtk import main as gtk_main
    except ImportError:
        # 没装 GTK 的 Linux 上，装了 Qt 也能开窗口
        return _qt()
    return gtk_main()


def _qt() -> int:
    try:
        from yuketang_courseware.app_qt import main as qt_main
    except ImportError as exc:
        print(
            "图形界面缺 PySide6，装一下：pip install PySide6-Essentials\n"
            "（只跑命令行的话不用装：yuketang-courseware --help）",
            file=sys.stderr,
        )
        print(f"具体错误：{exc}", file=sys.stderr)
        return 1
    return qt_main()


if __name__ == "__main__":  # pragma: no cover - 手动调试用
    raise SystemExit(main())
