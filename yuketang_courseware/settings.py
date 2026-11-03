"""记住上次填的网址和保存目录。

只写在本机：Linux 是 `~/.local/share/yuketang-courseware/settings.json`，
Windows 是 `%APPDATA%\\yuketang-courseware\\settings.json`。程序里不写死学校 id，
网址是用户自己填的。
"""

from __future__ import annotations

import json
import os

from yuketang_courseware import platforms

PATH = platforms.data_root() / "settings.json"


def load() -> dict:
    try:
        data = json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(url: str, directory: str) -> None:
    data = {"url": url, "directory": directory}
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        part = PATH.with_suffix(".part")
        part.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(part, PATH)
    except OSError:
        pass
