"""取数性能与并发安全：每次请求不该重复做「找端口、列标签页」。

用本地假 DevTools 服务端量，不联网、不起浏览器。判据是「每次请求的纯开销」和
「每次请求发出的 HTTP 数」——真机上后者对应扫一遍 /proc，几百个进程时代价很高。
"""

from __future__ import annotations

import statistics
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_devtools import FakeDevTools  # noqa: E402

from yuketang_courseware import browser as browser_mod  # noqa: E402
from yuketang_courseware import catalog  # noqa: E402
from yuketang_courseware.catalog import CourseItem, load_lessons  # noqa: E402

COURSE_PATH = "/v2/api/web/courses/list?identity=2"


class FetchOverheadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = FakeDevTools()
        self.addCleanup(self.fake.close)
        self.scans = {"n": 0}
        real = browser_mod._profile_browser_pids

        def counting(data_dir):
            self.scans["n"] += 1
            return [__import__("os").getpid()]

        browser_mod._profile_browser_pids = counting
        self.addCleanup(setattr, browser_mod, "_profile_browser_pids", real)
        self.api = browser_mod.CoursewareBrowser(data_dir=Path("/tmp/fake-profile"))
        # 预热：第一次必须做完整发现
        self.api.fetch_json(self.fake.origin, self.fake.host, COURSE_PATH, None, None)

    def test_repeat_fetch_hits_no_http_and_no_proc_scan(self) -> None:
        scans_before = self.scans["n"]
        http_before = self.fake.counts()["http"]
        for _ in range(25):
            self.api.fetch_json(self.fake.origin, self.fake.host, COURSE_PATH, None, None)
        self.assertEqual(self.fake.counts()["http"] - http_before, 0, "重复请求不该再问调试端口")
        self.assertEqual(self.scans["n"] - scans_before, 0, "重复请求不该再扫 /proc")

    def test_per_call_overhead_stays_small(self) -> None:
        samples = []
        for _ in range(30):
            began = time.perf_counter()
            self.api.fetch_json(self.fake.origin, self.fake.host, COURSE_PATH, None, None)
            samples.append((time.perf_counter() - began) * 1000)
        median = statistics.median(samples)
        self.assertLess(median, 3.0, f"每次请求自耗 {median:.2f}ms，说明又在重复找页面")

    def test_second_open_url_does_not_deadlock(self) -> None:
        """浏览器已经开着时再点一次「登录」：以前会卡在非重入锁上。"""
        done = threading.Event()
        error: list[BaseException] = []

        def run() -> None:
            try:
                self.api.open_url(f"{self.fake.origin}/v2/web/index")
            except BaseException as exc:  # noqa: BLE001 - 线程里要把异常带出来
                error.append(exc)
            finally:
                done.set()

        threading.Thread(target=run, daemon=True).start()
        self.assertTrue(done.wait(15), "open_url 卡住了（锁没释放）")
        self.assertFalse(error, f"open_url 报错：{error}")

    def test_connection_reuse_across_threads(self) -> None:
        """并发取数时复用同一条调试连接，不重复建连。"""
        before = self.fake.counts()["connections"]
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(5):
                    self.api.fetch_json(
                        self.fake.origin, self.fake.host, COURSE_PATH, None, None
                    )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertFalse(errors, f"并发取数出错：{errors[:2]}")
        self.assertEqual(self.fake.counts()["connections"] - before, 0, "不该重复建连接")


class LeafRequestTest(unittest.TestCase):
    """课件区的 leaf 是一个一个换 courseware_id 的，能省就要省。"""

    def setUp(self) -> None:
        # 缓存写到临时目录，别被上一次跑（或真实使用）留下的 leaf-map 影响
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        old = catalog._cache_path
        catalog._cache_path = lambda: Path(self._tmp.name) / "leaf-map.json"
        self.addCleanup(setattr, catalog, "_cache_path", old)

    def _course(self) -> CourseItem:
        return CourseItem("101", "2026-1", "自动控制原理", "01班", "2874")

    def test_leaves_already_in_activities_are_not_resolved(self) -> None:
        asked: list[str] = []

        def fetch(path, cid, uid):
            asked.append(path)
            if "/logs/learn/" in path:
                return {
                    "data": {
                        "activities": [
                            {"type": 14, "title": "第1讲 绪论", "courseware_id": "cw1"},
                            {"type": 14, "title": "第2讲 传递函数", "courseware_id": "cw2"},
                        ],
                        "has_more": False,
                    }
                }
            if path.startswith("/v2/api/web/classrooms/"):
                return {"data": {"course_sign": "sign1", "uv_id": 2874}}
            if path.startswith("/mooc-api"):
                return {
                    "data": {
                        "course_chapter": [
                            {
                                "section_leaf_list": [
                                    {"id": 11, "name": "第1讲 绪论", "leaf_type": 8},
                                    {"id": 12, "name": "第2讲 传递函数", "leaf_type": 8},
                                    {"id": 13, "name": "第3讲 频域分析", "leaf_type": 8},
                                ]
                            }
                        ]
                    }
                }
            if path.startswith("/edu_admin/leaf_level_info/"):
                return {"data": {"courseware_id": "leaf-" + path.split("leaf_level_id=")[1][:2]}}
            raise AssertionError(path)

        course = self._course()
        load_lessons(fetch, course)
        resolved = [p for p in asked if "/edu_admin/leaf_level_info/" in p]
        self.assertEqual(len(resolved), 1, "只有活动里没有的那一讲才该去换 courseware_id")
        self.assertIn("leaf_level_id=13", resolved[0])
        self.assertEqual([l.title for l in course.lessons][:2], ["第1讲 绪论", "第2讲 传递函数"])

    def test_without_activities_all_leaves_are_resolved(self) -> None:
        asked: list[str] = []

        def fetch(path, cid, uid):
            asked.append(path)
            if "/logs/learn/" in path:
                return {"data": {"activities": [], "has_more": False}}
            if path.startswith("/v2/api/web/classrooms/"):
                return {"data": {"course_sign": "s", "uv_id": 1}}
            if path.startswith("/mooc-api"):
                return {
                    "data": {
                        "course_chapter": [
                            {
                                "section_leaf_list": [
                                    {"id": 21, "name": "甲", "leaf_type": 8},
                                    {"id": 22, "name": "乙", "leaf_type": 8},
                                ]
                            }
                        ]
                    }
                }
            return {"data": {"courseware_id": "cw"}}

        course = self._course()
        load_lessons(fetch, course)
        resolved = [p for p in asked if "/edu_admin/leaf_level_info/" in p]
        self.assertEqual(len(resolved), 2)

    def test_cached_leaf_map_avoids_second_round(self) -> None:
        """同一门课第二次打开（换学期/切筛选）不该再问一遍 leaf。"""
        asked: list[str] = []

        def fetch(path, cid, uid):
            asked.append(path)
            if "/logs/learn/" in path:
                return {"data": {"activities": [], "has_more": False}}
            if path.startswith("/v2/api/web/classrooms/"):
                return {"data": {"course_sign": "s", "uv_id": 1}}
            if path.startswith("/mooc-api"):
                return {
                    "data": {
                        "course_chapter": [
                            {"section_leaf_list": [{"id": 31, "name": "甲", "leaf_type": 8}]}
                        ]
                    }
                }
            return {"data": {"courseware_id": "cw"}}

        course = self._course()
        load_lessons(fetch, course)
        first = len([p for p in asked if "/edu_admin/" in p])
        load_lessons(fetch, course)
        second = len([p for p in asked if "/edu_admin/" in p]) - first
        self.assertEqual(first, 1)
        self.assertEqual(second, 0)


if __name__ == "__main__":
    unittest.main()
