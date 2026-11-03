#!/usr/bin/env python3
"""生成应用图标：一份几何，输出 SVG（进安装包）和 PNG 预览（验收用）。

    python3 packaging/make-icon.py                     # 只写 SVG
    python3 packaging/make-icon.py --preview 128 48    # 顺便出 PNG 预览（要 Pillow）

设计约束：
- 128 网格里只留大块形状，32×32 的菜单/任务栏尺寸下仍认得出；
- 配色跟窗口一致（青绿 accent），不跟界面打架；
- 徽标是「下载」箭头，不是播放键——这个程序是收课件，不是放视频。
"""

from __future__ import annotations

import argparse
from pathlib import Path

SIZE = 128
ACCENT_TOP = "#12a594"
ACCENT_BOTTOM = "#0b6b61"
INK = "#0a4f48"
PAPER = "#ffffff"
RULE = "#0f9d8f"
LINE = "#b7ebe4"

# 共享几何：(x, y, w, h, r)
BG = (6, 6, 116, 116, 26)
PAGE = (26, 20, 76, 86, 10)
HEADER = (38, 34, 52, 7, 3.5)
LINES = ((38, 50, 46, 5, 2.5), (38, 61, 52, 5, 2.5), (38, 72, 34, 5, 2.5))
BADGE_RING = (87, 89, 20)     # cx, cy, r
BADGE = (87, 89, 16.5)
SHAFT = (84.5, 78, 5, 10, 2.5)
HEAD = ((78, 87), (96, 87), (87, 98))


def svg() -> str:
    lines = []
    for x, y, w, h, r in LINES:
        lines.append(
            f'  <rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{r}" fill="{LINE}"/>'
        )
    head = " ".join(f"{x},{y}" for x, y in HEAD)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" height="{SIZE}" viewBox="0 0 {SIZE} {SIZE}">
  <title>雨课堂课件</title>
  <defs>
    <linearGradient id="bg" x1="0" y1="0" x2="0.4" y2="1">
      <stop offset="0" stop-color="{ACCENT_TOP}"/>
      <stop offset="1" stop-color="{ACCENT_BOTTOM}"/>
    </linearGradient>
  </defs>
  <rect x="{BG[0]}" y="{BG[1]}" width="{BG[2]}" height="{BG[3]}" rx="{BG[4]}" fill="url(#bg)"/>
  <rect x="{PAGE[0]}" y="{PAGE[1]}" width="{PAGE[2]}" height="{PAGE[3]}" rx="{PAGE[4]}" fill="{PAPER}"/>
  <rect x="{HEADER[0]}" y="{HEADER[1]}" width="{HEADER[2]}" height="{HEADER[3]}" rx="{HEADER[4]}" fill="{RULE}"/>
{chr(10).join(lines)}
  <circle cx="{BADGE_RING[0]}" cy="{BADGE_RING[1]}" r="{BADGE_RING[2]}" fill="{PAPER}"/>
  <circle cx="{BADGE[0]}" cy="{BADGE[1]}" r="{BADGE[2]}" fill="{INK}"/>
  <rect x="{SHAFT[0]}" y="{SHAFT[1]}" width="{SHAFT[2]}" height="{SHAFT[3]}" rx="{SHAFT[4]}" fill="{PAPER}"/>
  <polygon points="{head}" fill="{PAPER}"/>
</svg>
"""


def preview(path: Path, size: int) -> None:
    """用 Pillow 按同一套坐标画一张预览图（只为肉眼验收，不进安装包）。"""
    from PIL import Image, ImageDraw

    scale = size / SIZE
    big = 4  # 先放大画再缩，边缘不锯齿

    def box(rect):
        x, y, w, h, _r = rect
        return [round(v * scale * big) for v in (x, y, x + w, y + h)]

    image = Image.new("RGBA", (size * big, size * big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    # 渐变底：逐行插值
    bg = Image.new("RGBA", image.size, (0, 0, 0, 0))
    bg_draw = ImageDraw.Draw(bg)
    top = tuple(int(ACCENT_TOP[i : i + 2], 16) for i in (1, 3, 5))
    bottom = tuple(int(ACCENT_BOTTOM[i : i + 2], 16) for i in (1, 3, 5))
    bg_draw.rounded_rectangle(box(BG), radius=BG[4] * scale * big, fill=top)
    shade = Image.new("RGBA", image.size, (0, 0, 0, 0))
    shade_draw = ImageDraw.Draw(shade)
    shade_draw.rounded_rectangle(box(BG), radius=BG[4] * scale * big, fill=bottom)
    mask = Image.linear_gradient("L").resize(bg.size)
    image.paste(bg, (0, 0))
    image.paste(shade, (0, 0), mask)
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(box(PAGE), radius=PAGE[4] * scale * big, fill=PAPER)
    draw.rounded_rectangle(box(HEADER), radius=HEADER[4] * scale * big, fill=RULE)
    for line in LINES:
        draw.rounded_rectangle(box(line), radius=line[4] * scale * big, fill=LINE)
    cx, cy, r = BADGE_RING
    draw.ellipse(
        [(cx - r) * scale * big, (cy - r) * scale * big,
         (cx + r) * scale * big, (cy + r) * scale * big],
        fill=PAPER,
    )
    cx, cy, r = BADGE
    draw.ellipse(
        [(cx - r) * scale * big, (cy - r) * scale * big,
         (cx + r) * scale * big, (cy + r) * scale * big],
        fill=INK,
    )
    draw.rounded_rectangle(box(SHAFT), radius=SHAFT[4] * scale * big, fill=PAPER)
    draw.polygon([(x * scale * big, y * scale * big) for x, y in HEAD], fill=PAPER)
    image.resize((size, size), Image.LANCZOS).save(path)
    print(f"{path} {size}x{size}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None, help="SVG 输出路径")
    parser.add_argument("--preview", nargs="*", type=int, default=[], help="额外出这些尺寸的 PNG")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    target = Path(args.out) if args.out else root / "yuketang-courseware.svg"
    target.write_text(svg(), encoding="utf-8")
    print(target)
    for size in args.preview:
        preview(root.parent / "docs" / f"icon-{size}.png", size)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
