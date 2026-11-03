"""没有参数就开窗口；带参数走命令行。

命令行不依赖图形界面库，只在真正开窗口时才导入 app。
"""

import sys

# 版本太老时，连 import 都会在类型注解上炸掉，先给一句人话再退出。
_MIN_PYTHON = (3, 8)
if sys.version_info < _MIN_PYTHON:
    _need = ".".join(str(part) for part in _MIN_PYTHON)
    _got = ".".join(str(part) for part in sys.version_info[:3])
    _how = (
        "到 https://www.python.org/downloads/ 装一个，安装时勾上 Add Python to PATH。\n"
        if sys.platform == "win32"
        else "Debian / Ubuntu 上装一个：sudo apt install python3\n"
    )
    sys.stderr.write(
        f"这个程序需要 Python {_need} 或更新版本，当前是 {_got}。\n"
        + _how
        + "（只跑命令行的话，装完就能用；图形界面还要 PySide6（Windows）"
        "或 Gtk4 + libadwaita（Linux）。）\n"
        "看清楚缺什么：yuketang-courseware --doctor\n"
    )
    raise SystemExit(1)

from yuketang_courseware.cli import main as cli_main  # noqa: E402
from yuketang_courseware.platforms import configure_console  # noqa: E402


def _ensure_console() -> None:
    """打包成窗口程序后 stdout/stderr 是 None；带参数跑命令行时接一个控制台回来。

    从 cmd / PowerShell 启动就接到父控制台；双击带参数则新开一个。
    接不上就退回空设备，至少不因为 print 崩掉。
    """
    if sys.platform != "win32" or sys.stdout is not None and sys.stderr is not None:
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        attached = kernel32.AttachConsole(-1) or kernel32.AllocConsole()
        if attached:
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    except Exception:
        pass
    if sys.stdout is None:
        import os

        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        import os

        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115


if len(sys.argv) > 1:
    _ensure_console()
configure_console()


def main() -> int:
    argv = sys.argv[1:]
    if argv:
        return cli_main(argv)
    from yuketang_courseware.app import main as gui_main

    return gui_main()


raise SystemExit(main())
