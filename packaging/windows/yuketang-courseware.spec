# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包：单文件 exe，Release 可直接下载双击。

GUI：yuketang-courseware.exe（无黑框）
CLI：yuketang-courseware-cli.exe（带控制台）
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent.parent  # noqa: F821 - PyInstaller 注入
ICON = Path(SPECPATH).resolve() / "app.ico"  # noqa: F821
VERSION = str(Path(SPECPATH).resolve() / "version_info.txt")  # noqa: F821

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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="yuketang-courseware",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON),
    version=VERSION,
)

cli = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="yuketang-courseware-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    icon=str(ICON),
    version=VERSION,
)
