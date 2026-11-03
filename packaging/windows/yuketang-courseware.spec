# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：一个目录里放 GUI 和命令行两个 exe。

GUI：双击启动，窗口程序（不带黑框）。
CLI：命令行用，带控制台，方便重定向输出。
两者共用同一份解释器和依赖，所以只 COLLECT 一次。
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent.parent  # noqa: F821 - PyInstaller 注入
ICON = Path(SPECPATH).resolve() / "app.ico"  # noqa: F821

a = Analysis(
    [str(ROOT / "yuketang_courseware" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[(str(ROOT / "docs" / "icon-128.png"), "docs")],
    hiddenimports=[
        "yuketang_courseware.app_qt",
        "yuketang_courseware.app_gtk",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "unittest", "test", "pydoc_data"],
    noarchive=False,
)

pyz = PYZ(a.pure)

gui = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yuketang-courseware",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON),
    version=str(Path(SPECPATH).resolve() / "version_info.txt"),  # noqa: F821
)

cli = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="yuketang-courseware-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=str(ICON),
    version=str(Path(SPECPATH).resolve() / "version_info.txt"),  # noqa: F821
)

coll = COLLECT(
    gui,
    cli,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="yuketang-courseware",
)
