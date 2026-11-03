#!/usr/bin/env python3
"""把 docs/icon-128.png 转成 Windows 的 .ico（exe 和窗口图标都用它）。

构建时跑：python packaging/windows/make-ico.py
需要 Pillow；生成结果在 packaging/windows/app.ico。
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent.parent
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "icon-128.png"
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(__file__).parent / "app.ico"
    if not source.exists():
        sys.stderr.write(f"icon source not found: {source}\n")
        return 1
    image = Image.open(source).convert("RGBA")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Pillow 会按 sizes 生成多分辨率 ico；没有 256 的源图就放大，边缘略软但够用
    image.save(target, format="ICO", sizes=SIZES)
    # CI Windows 默认控制台是 cp1252，中文 print 会直接炸，尽管 ico 已写完
    print(f"wrote {target} ({', '.join(f'{w}x{h}' for w, h in SIZES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
