"""鎶婄獥鍙ｆ覆鏌撴垚 PNG锛岀敤鏉ユ牳瀵圭晫闈紙闇€瑕佸浘褰㈢晫闈紝涓嶅仛缃戠粶璇锋眰锛夈€?
    python3 tests/screenshot.py --state fresh     --out docs/screenshots/fresh.png
    python3 tests/screenshot.py --state populated --out docs/screenshots/populated.png

`fresh` 鏄娆″惎鍔紙缃戝潃銆佷繚瀛樼洰褰曢兘绌虹潃锛屼篃涓嶅幓鎺㈡祴宸茬櫥褰曠殑娴忚鍣級锛?`populated` 鐢ㄥ亣璇剧▼鏁版嵁濉厖锛岀湅璇剧▼琛屽拰鎸夐挳鐨勬牱瀛愩€?"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Graphene, Gsk, Gtk

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yuketang_courseware.app_gtk import MainWindow
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
        ("鑷姩鎺у埗鍘熺悊", "绀轰緥鐝骇", ["绗?2璁?绗?绔狅紙1锛?, "绗?1璁?绗?绔狅紙2锛?]),
        ("璁＄畻鏈虹綉缁?, "绀轰緥鐝骇", ["绗?璁?杩愯緭灞?-UDP&TCP鍗忚"]),
        ("璁＄畻鏂规硶", "绀轰緥鐝骇", ["绗笁绔?鍥炲綊鍒嗘瀽-2"]),
    ]
    courses: list[CourseItem] = []
    for index, (name, klass, titles) in enumerate(rows):
        course = CourseItem(str(100 + index), "202601", name, klass, "2874")
        course.lessons = [
            LessonItem(14, title, f"cw{index}{position}", True)
            for position, title in enumerate(titles)
        ]
        course.lessons.append(
            LessonItem(2, "缃戦〉棰勪範", "", False, "缃戦〉棰勪範锛屾棤骞荤伅鐗?)
        )
        courses.append(course)
    return courses


def save(win: MainWindow) -> bool:
    child = win.get_child()
    width, height = child.get_width(), child.get_height()
    if width <= 0 or height <= 0:
        print("绐楀彛杩樻病鍒嗛厤灏哄", file=sys.stderr)
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
    print(f"宸蹭繚瀛?{out}锛坽width}x{height}锛?)
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
