"""模拟一次「登录后读完整学期」：数请求、量耗时。

    python3 tests/bench_load.py [课程数] [每门讲次数] [网络往返毫秒]

假服务端里有真实的接口形状（活动、课件区、leaf 映射），网络延迟可调。
用来回答「为什么登录后很久才出列表」——请求数 × 往返时间就是主因。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_devtools import FakeDevTools, patch_as_browser  # noqa: E402

from yuketang_courseware import browser as browser_mod  # noqa: E402
from yuketang_courseware import catalog  # noqa: E402
from yuketang_courseware.catalog import assign_folders, fetch_courses, load_many  # noqa: E402


def make_router(courses: int, lessons: int):
    def router(path: str) -> dict:
        query = path.split("?", 1)[1] if "?" in path else ""
        if path.startswith("/v2/api/web/courses/list"):
            return {
                "errcode": 0,
                "data": {
                    "list": [
                        {
                            "classroom_id": 100 + index,
                            "name": f"{index}班",
                            "term": "2026-1",
                            "course": {"name": f"课程{index}", "university_id": 2874},
                        }
                        for index in range(courses)
                    ]
                },
            }
        if "/logs/learn/" in path:
            classroom = int(path.split("/logs/learn/")[1].split("?")[0])
            return {
                "data": {
                    "activities": [
                        {
                            "type": 14,
                            "title": f"第{position}讲",
                            "courseware_id": f"cw{classroom}-{position}",
                        }
                        for position in range(lessons)
                    ],
                    "has_more": False,
                }
            }
        if path.startswith("/v2/api/web/classrooms/"):
            return {"data": {"course_sign": "sign", "uv_id": 2874}}
        if path.startswith("/mooc-api"):
            classroom = int(query.split("cid=")[1].split("&")[0])
            return {
                "data": {
                    "course_chapter": [
                        {
                            "section_leaf_list": [
                                {"id": classroom * 100 + p, "name": f"第{p}讲", "leaf_type": 8}
                                for p in range(lessons)
                            ]
                        }
                    ]
                }
            }
        if path.startswith("/edu_admin/leaf_level_info/"):
            return {"data": {"courseware_id": "leaf"}}
        return {"data": {}}

    return router


def run(courses: int, lessons: int, rtt_ms: float, legacy: bool) -> dict:
    fake = FakeDevTools(delay=rtt_ms / 1000.0, router=make_router(courses, lessons))
    patch_as_browser(browser_mod, Path("/tmp/fake-profile"))
    api = browser_mod.CoursewareBrowser(data_dir=Path("/tmp/fake-profile"))

    def fetch(path, classroom_id, university_id):
        return api.fetch_json(fake.origin, fake.host, path, classroom_id, university_id)

    old = catalog.fetch_leaf_lessons
    if legacy:  # 模拟旧行为：不去看课堂活动里有没有同一讲
        catalog.fetch_leaf_lessons = lambda f, c, workers=6, known_titles=None: old(f, c, workers)
    try:
        began = time.perf_counter()
        got = fetch_courses(fetch, "2874", None)
        assign_folders(got)
        load_many(fetch, got, workers=3)
        spent = time.perf_counter() - began
        counts = fake.counts()
    finally:
        catalog.fetch_leaf_lessons = old
        fake.close()
    return {
        "seconds": spent,
        "requests": counts["evaluate"],
        "lessons": sum(len(c.lessons) for c in got),
        "courses": len(got),
    }


if __name__ == "__main__":
    import os
    import tempfile

    courses = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    lessons = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    rtt = float(sys.argv[3]) if len(sys.argv) > 3 else 60.0
    for legacy in (True, False):
        label = "旧（不去重）" if legacy else "新（活动已有的不再问）"
        for mode in ("冷启动", "第二次"):
            sock = tempfile.TemporaryDirectory()
            os.environ["XDG_CACHE_HOME"] = sock.name
            got = run(courses, lessons, rtt, legacy)
            if mode == "第二次":       # 同一份缓存再跑一遍
                got = run(courses, lessons, rtt, legacy)
            print(
                f"{label} {mode}: {got['courses']} 门 / {got['lessons']} 讲  "
                f"{got['requests']} 次请求  {got['seconds']:.2f} 秒  （往返 {rtt:.0f}ms）"
            )
            sock.cleanup()
