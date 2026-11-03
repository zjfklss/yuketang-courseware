"""没有参数就开窗口；带参数走命令行。

命令行不依赖 GTK，只在真正开窗口时才导入 app。
"""

import sys

# 版本太老时，连 import 都会在类型注解上炸掉，先给一句人话再退出。
_MIN_PYTHON = (3, 8)
if sys.version_info < _MIN_PYTHON:
    _need = ".".join(str(part) for part in _MIN_PYTHON)
    _got = ".".join(str(part) for part in sys.version_info[:3])
    sys.stderr.write(
        f"这个程序需要 Python {_need} 或更新版本，当前是 {_got}。\n"
        "Debian / Ubuntu 上装一个：sudo apt install python3\n"
        "（只跑命令行的话，装完就能用；图形界面还要 Gtk4 和 libadwaita。）\n"
        "看清楚缺什么：yuketang-courseware --doctor\n"
    )
    raise SystemExit(1)

from yuketang_courseware.cli import main as cli_main  # noqa: E402


def main() -> int:
    argv = sys.argv[1:]
    if argv:
        return cli_main(argv)
    from yuketang_courseware.app import main as gui_main

    return gui_main()


raise SystemExit(main())
