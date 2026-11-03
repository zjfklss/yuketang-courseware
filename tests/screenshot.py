"""把窗口渲染成 PNG，用来核对界面（需要图形界面，不做网络请求）。

    python3 tests/screenshot.py --state fresh     --out docs/screenshots/fresh.png
    python3 tests/screenshot.py --state populated --out docs/screenshots/populated.png

`fresh` 是首次启动（网址、保存目录都空着，也不去探测已登录的浏览器）；
`populated` 用假课程数据填充，看课程行和按钮的样子。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Graphene, Gsk, Gtk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yuketang_courseware.app import MainWindow
from yuketang_courseware.catalog import CourseItem, LessonItem

ARGS = argparse.ArgumentParser()
ARGS.add_argument("--state", choices=("fresh", "populated"), default="fresh")
ARGS.add_argument("--out", default="screenshot.png")
ARGS.add_argument("--width", type=int, default=900)
ARGS.add_argument("--height", type=int, default=800)
OPTIONS = ARGS.parse_args()

app = Adw.Application(application_id="local.yuketang.Screenshot")


def fake_courses() -> list[CourseItem]:
    rows = [
        ("自动控制原理", "示例班级", ["第12讲-第7章（1）", "第11讲-第6章（2）"]),
        ("计算机网络", "示例班级", ["第9讲 运输层 -UDP&TCP协议"]),
        ("计算方法", "示例班级", ["第三章 回归分析-2"]),
    ]
    courses: list[CourseItem] = []
    for index, (name, klass, titles) in enumerate(rows):
        course = CourseItem(str(100 + index), "202601", name, klass, "2874")
        course.lessons = [
            LessonItem(14, title, f"cw{index}{position}", True)
            for position, title in enumerate(titles)
        ]
        course.lessons.append(
            LessonItem(2, "网页预习", "", False, "网页预习，无幻灯片")
        )
        courses.append(course)
    return courses


def save(win: MainWindow) -> bool:
    child = win.get_child()
    width, height = child.get_width(), child.get_height()
    if width <= 0 or height <= 0:
        print("窗口还没分配尺寸", file=sys.stderr)
        app.quit()
        return False
    paintable = Gtk.WidgetPaintable.new(child)
    snapshot = Gtk.Snapshot()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    renderer = Gsk.CairoRenderer()
    native = win.get_native()
    renderer.realize(native.get_surface() if native is not None else None)
    texture = renderer.render_texture(node, Graphene.Rect().init(0, 0, width, height))
    out = Path(OPTIONS.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    texture.save_to_png(str(out))
    print(f"已保存 {out}（{width}x{height}）")
    renderer.unrealize()
    app.quit()
    return False


def populate(win: MainWindow) -> bool:
    courses = fake_courses()
    win._all_courses = courses
    win._install_terms(["202601"], "202601")
    win._render()
    win._update_selection()
    GLib.timeout_add_seconds(1, lambda: save(win))
    return False


def on_activate(application) -> None:
    win = MainWindow(application)
    win.set_default_size(OPTIONS.width, OPTIONS.height)
    win.present()
    if OPTIONS.state == "fresh":
        GLib.timeout_add_seconds(1, lambda: save(win))
    else:
        GLib.timeout_add(200, lambda: populate(win))


app.connect("activate", on_activate)
raise SystemExit(app.run(["screenshot"]))
