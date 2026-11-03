"""离线单测：不联网，用假 fetch 和假封面字节。

在 tools/courseware 里跑：
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from yuketang_courseware import catalog, library, slides
from yuketang_courseware.catalog import (
    CourseItem,
    LessonItem,
    assign_folders,
    fetch_leaf_lessons,
    lessons_from_activities,
    load_lessons,
    merge_lessons,
    parse_courses,
)
from yuketang_courseware.errors import ApiError
from yuketang_courseware.names import api_path, parse_course_url, safe_component, term_sort_key
from yuketang_courseware.pdf import (
    Page,
    PdfError,
    count_pages,
    jpeg_size,
    looks_complete,
    page_from_bytes,
    write_pdf,
)


def jpeg_bytes(width: int = 640, height: int = 360, color=(200, 30, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="JPEG")
    return buffer.getvalue()


class PdfTest(unittest.TestCase):
    def test_write_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "a.pdf"
            pages = [page_from_bytes(jpeg_bytes(960, 540)) for _ in range(4)]
            self.assertEqual(write_pdf(pages, dest), 4)
            self.assertEqual(count_pages(dest), 4)
            self.assertTrue(looks_complete(dest))
            self.assertTrue(dest.read_bytes().startswith(b"%PDF-"))

    def test_uneven_sizes_are_scaled_not_stretched(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "b.pdf"
            write_pdf(
                [page_from_bytes(jpeg_bytes(960, 540)), page_from_bytes(jpeg_bytes(400, 400))],
                dest,
            )
            text = dest.read_bytes()
            self.assertIn(b"/MediaBox [0 0 960 540]", text)
            self.assertIn(b"/Width 400 /Height 400", text)

    def test_png_page_converted(self):
        buffer = io.BytesIO()
        Image.new("RGBA", (320, 200), (0, 0, 0, 0)).save(buffer, format="PNG")
        page = page_from_bytes(buffer.getvalue())
        self.assertTrue(page.data.startswith(b"\xff\xd8\xff"))
        self.assertEqual((page.width, page.height), (320, 200))

    def test_garbage_rejected(self):
        with self.assertRaises(PdfError):
            page_from_bytes(b"not an image")

    def test_empty_pages_rejected(self):
        with self.assertRaises(PdfError):
            write_pdf([], Path("/tmp/never.pdf"))

    def test_truncated_pdf_is_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.pdf"
            write_pdf([page_from_bytes(jpeg_bytes())], path)
            data = path.read_bytes()
            path.write_bytes(data[: len(data) // 2])
            self.assertFalse(looks_complete(path))

    def test_jpeg_size_reads_sof(self):
        width, height, components = jpeg_size(jpeg_bytes(1234, 567))
        self.assertEqual((width, height, components), (1234, 567, 3))


class CatalogTest(unittest.TestCase):
    def setUp(self):
        # 课件映射缓存写到临时目录，别污染真实的 ~/.cache
        self._tmp = tempfile.TemporaryDirectory()
        self._old_cache = catalog._cache_path
        catalog._cache_path = lambda: Path(self._tmp.name) / "leaf-map.json"

    def tearDown(self):
        catalog._cache_path = self._old_cache
        self._tmp.cleanup()

    def test_parse_courses(self):
        payload = {
            "errcode": 0,
            "data": {
                "list": [
                    {
                        "classroom_id": 101,
                        "name": "01班",
                        "term": "2026-1",
                        "course": {"name": "自动控制原理", "university_id": 2874},
                    },
                    {"classroom_id": 101, "name": "重复", "course": {"name": "x"}},
                    {"classroom_id": 202, "name": "02班", "term": "2025-2", "course": {"name": "计算机网络"}},
                ]
            },
        }
        courses = parse_courses(payload, None)
        self.assertEqual([c.classroom_id for c in courses], ["101", "202"])
        self.assertEqual(courses[0].course_name, "自动控制原理")
        self.assertEqual(courses[0].university_id, "2874")
        self.assertEqual(courses[1].university_id, None)

    def test_lessons_from_activities(self):
        activities = [
            {"type": 14, "title": "第10讲", "courseware_id": "cw1"},
            {"type": 3, "title": "第11讲", "courseware_id": "cw2"},
            {"type": 2, "title": "网页预习", "courseware_id": ""},
            {"type": 5, "title": "投票", "courseware_id": "x"},
            {"type": 14, "title": "没有课件", "courseware_id": None},
        ]
        lessons = lessons_from_activities(activities)
        self.assertEqual([lesson.title for lesson in lessons], ["第10讲", "第11讲", "网页预习"])
        self.assertFalse(lessons[2].selectable)

    def test_merge_dedupes_by_courseware_and_title(self):
        activities = [LessonItem(14, "第10讲", "cw1", True)]
        leaves = [
            LessonItem(8, "第10讲", "cw1", True, source="课件"),
            LessonItem(8, "章节一 绪论", "cw9", True, source="课件"),
            LessonItem(8, "章节一绪论", "cw10", True, source="课件"),
        ]
        merged = merge_lessons(activities, leaves)
        self.assertEqual([(l.title, l.courseware_id) for l in merged], [("第10讲", "cw1"), ("章节一 绪论", "cw9")])

    def test_assign_folders_dedupes_same_name(self):
        courses = [
            CourseItem("1", "2026-1", "自动控制原理", "01班", None),
            CourseItem("2", "2026-1", "自动控制原理", "02班", None),
            CourseItem("3", "2026-1", "计算机网络", "01班", None),
        ]
        assign_folders(courses)
        folders = [course.folder for course in courses]
        self.assertEqual(len(set(folders)), 3)
        self.assertEqual(folders[2], "计算机网络")

    def test_leaf_lessons_and_merge_with_activities(self):
        calls = []

        def fetch(path, classroom_id, university_id):
            calls.append(path)
            if path.startswith("/v2/api/web/classrooms/"):
                return {"data": {"course_sign": "sign1", "uv_id": 2874}}
            if path.startswith("/mooc-api/v1/lms/learn/course/chapter"):
                return {
                    "data": {
                        "course_chapter": [
                            {
                                "section_leaf_list": [
                                    {"id": 11, "name": "第一章 绪论", "leaf_type": 8},
                                    {"id": 12, "name": "视频", "leaf_type": 4},
                                    {"id": 13, "name": "没有课件", "leaf_type": 8},
                                ]
                            }
                        ]
                    }
                }
            if path.startswith("/edu_admin/leaf_level_info/"):
                if "leaf_level_id=11" in path:
                    return {"data": {"courseware_id": 555}}
                return {"data": {"courseware_id": None}}
            raise AssertionError(path)

        course = CourseItem("101", "2026-1", "自动控制原理", "01班", "2874")
        leaves = fetch_leaf_lessons(fetch, course)
        self.assertEqual([(l.title, l.courseware_id, l.selectable) for l in leaves],
                         [("第一章 绪论", "555", True), ("没有课件", "", False)])
        asked = " ".join(calls)
        self.assertIn("leaf_level_id=11", asked)
        self.assertIn("leaf_level_id=13", asked)

    def test_leaf_cache_skips_second_round(self):
        calls = []

        def fetch(path, classroom_id, university_id):
            calls.append(path)
            if path.startswith("/v2/api/web/classrooms/"):
                return {"data": {"course_sign": "sign1", "uv_id": 2874}}
            if path.startswith("/mooc-api"):
                return {
                    "data": {
                        "course_chapter": [
                            {"section_leaf_list": [{"id": 21, "name": "课件甲", "leaf_type": 8}]}
                        ]
                    }
                }
            return {"data": {"courseware_id": 777}}

        course = CourseItem("303", "2026-1", "计算方法", "01班", "2874")
        first = fetch_leaf_lessons(fetch, course)
        asked_first = len(calls)
        second = fetch_leaf_lessons(fetch, course)
        self.assertEqual([l.courseware_id for l in first], ["777"])
        self.assertEqual([l.courseware_id for l in second], ["777"])
        self.assertLess(len(calls) - asked_first, asked_first)  # 第二轮少问了一次 leaf
        self.assertNotIn("leaf_level_id=21", " ".join(calls[asked_first:]))

    def test_load_lessons_keeps_activity_when_leaves_fail(self):
        def fetch(path, classroom_id, university_id):
            if "/logs/learn/" in path:
                if "page=0" in path:
                    return {
                        "data": {
                            "activities": [{"type": 14, "title": "第1讲", "courseware_id": "cw1"}],
                            "has_more": False,
                        }
                    }
                return {"data": {}}
            raise ApiError("课件区接口失败")

        course = CourseItem("101", "2026-1", "自动控制原理", "01班", "2874")
        load_lessons(fetch, course)
        self.assertEqual([l.title for l in course.lessons], ["第1讲"])
        self.assertEqual(course.error, "")

    def test_activity_pagination(self):
        def fetch(path, classroom_id, university_id):
            if "page=0" in path:
                return {
                    "data": {
                        "activities": [{"type": 14, "title": "第1讲", "courseware_id": "cw1"}],
                        "has_more": True,
                    }
                }
            if "page=1" in path:
                return {
                    "data": {
                        "activities": [{"type": 14, "title": "第2讲", "courseware_id": "cw2"}],
                        "has_more": False,
                    }
                }
            return {"data": {}}

        course = CourseItem("101", "2026-1", "自动控制原理", "01班", "2874")
        load_lessons(fetch, course)
        self.assertEqual([l.title for l in course.lessons], ["第1讲", "第2讲"])
        self.assertFalse(course.no_activity)


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.covers = [jpeg_bytes(960, 540, (i * 20 % 255, 10, 10)) for i in range(3)]
        self.call = {"n": 0}
        slides._fetch_cover = self._fake_cover  # type: ignore[assignment]
        slides._ATTEMPTS = 1

    def tearDown(self):
        slides._fetch_cover = _real_fetch_cover  # type: ignore[assignment]
        slides._ATTEMPTS = 3

    def _fake_cover(self, url: str, attempts: int = 2) -> bytes:
        if "broken" in url:
            raise OSError("页面下载失败")
        index = int(url.rsplit("/", 1)[-1])
        return self.covers[index % len(self.covers)]

    def _fetch(self, path, classroom_id, university_id):
        self.call["n"] += 1
        if path.startswith("/api/v3/classroom-report/student/review"):
            return {"data": {"timelineList": [{"presentationId": "p1"}]}}
        if path.startswith("/api/v3/classroom-report/student/ppt"):
            slides_list = [
                {"id": str(index), "index": index, "cover": f"https://example.invalid/cover/{index}"}
                for index in range(3)
            ]
            if "broken" in self.call.get("mode", ""):
                slides_list.append({"id": "9", "index": 9, "cover": "https://example.invalid/cover/broken"})
            return {"data": {"slideList": slides_list}}
        raise AssertionError(path)

    def test_download_lesson_writes_pdf(self):
        course = CourseItem("101", "2026-1", "自动控制原理", "01班", "2874")
        course.folder = "自动控制原理"
        lesson = LessonItem(14, "第10讲", "cw1", True)
        with tempfile.TemporaryDirectory() as tmp:
            results = slides.download_lesson(self._fetch, course, lesson, Path(tmp))
            self.assertEqual([r.status for r in results], ["已下载"])
            saved = Path(tmp) / "自动控制原理" / "课件" / "第10讲.pdf"
            self.assertEqual(count_pages(saved), 3)

    def test_second_run_is_skipped(self):
        course = CourseItem("101", "2026-1", "自动控制原理", "01班", "2874")
        course.folder = "自动控制原理"
        lesson = LessonItem(14, "第10讲", "cw1", True)
        with tempfile.TemporaryDirectory() as tmp:
            slides.download_lesson(self._fetch, course, lesson, Path(tmp))
            results = slides.download_lesson(self._fetch, course, lesson, Path(tmp))
            self.assertEqual([r.status for r in results], ["已有跳过"])

    def test_missing_page_kept_as_partial(self):
        self.call["mode"] = "broken"
        course = CourseItem("101", "2026-1", "自动控制原理", "01班", "2874")
        course.folder = "自动控制原理"
        lesson = LessonItem(14, "第10讲", "cw1", True)
        with tempfile.TemporaryDirectory() as tmp:
            results = slides.download_lesson(self._fetch, course, lesson, Path(tmp))
            self.assertEqual(results[0].status, "已下载")
            self.assertIn("缺页 9", results[0].detail)

    def test_no_slides(self):
        course = CourseItem("101", "2026-1", "自动控制原理", "01班", "2874")
        course.folder = "自动控制原理"
        lesson = LessonItem(14, "第10讲", "cw1", True)

        def fetch(path, classroom_id, university_id):
            return {"data": {"timelineList": []}}

        with tempfile.TemporaryDirectory() as tmp:
            results = slides.download_lesson(fetch, course, lesson, Path(tmp))
            self.assertEqual(results[0].status, "无幻灯片")

    def test_download_selected_reports_counts(self):
        course = CourseItem("101", "2026-1", "自动控制原理", "01班", "2874")
        course.folder = "自动控制原理"
        jobs = [(course, LessonItem(14, "第10讲", "cw1", True)), (course, LessonItem(14, "第11讲", "cw2", True))]
        progress = []
        with tempfile.TemporaryDirectory() as tmp:
            results = slides.download_selected(
                self._fetch, jobs, Path(tmp), lambda d, t, text: progress.append((d, t)), threading.Event()
            )
            self.assertEqual(len(results), 2)
            self.assertTrue(all(item.status == "已下载" for item in results))
            summary, detail = slides.format_report(results)
            self.assertIn("已下载 2", summary)
            self.assertIn("第11讲.pdf", detail)
            self.assertEqual(progress[-1][:2], (2, 2))


class LibraryTest(unittest.TestCase):
    def _pdf(self, path: Path, pages: int = 2) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        pages_list = [page_from_bytes(jpeg_bytes(320, 180)) for _ in range(pages)]
        write_pdf(pages_list, path)
        return path

    def test_normalize_ignores_punct_and_case(self):
        self.assertEqual(
            library.normalize("第04讲-第二章（3）(3).PDF"),
            library.normalize("第04讲 第二章 3 3"),
        )

    def test_lesson_keys_cover_multi_presentation(self):
        keys = library.lesson_keys("第10讲 网络层 -UDP&TCP协议")
        self.assertIn(library.normalize("第10讲 网络层 -UDP&TCP协议.pdf"), keys)
        self.assertIn(library.normalize("第10讲 网络层 -UDP&TCP协议-2.pdf"), keys)

    def test_scan_is_recursive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "自动控制原理" / "第01讲 第1章绪论.pdf")
            self._pdf(root / "随便放" / "更深" / "第02讲-第二章（1）.pdf")
            (root / "空.txt").write_text("x", encoding="utf-8")
            files = library.scan(root)
            self.assertEqual(len(files), 2)
            self.assertTrue(all(item.path.suffix == ".pdf" for item in files))

    def test_find_existing_prefers_course_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._pdf(root / "别处" / "第01讲 绪论.pdf")
            second = self._pdf(root / "自动控制原理" / "第01讲 绪论.pdf")
            files = library.scan(root)
            index = library.build_index(files)
            found = library.find_existing(index, "第01讲 绪论", "自动控制原理")
            self.assertIsNotNone(found)
            self.assertEqual(found.path, second)
            self.assertNotEqual(found.path, first)

    def test_plan_tidy_files_matched_into_course_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "第01讲 第1章绪论.pdf")
            course = CourseItem("1", "202601", "自动控制原理", "24班", "2874")
            course.folder = "自动控制原理"
            course.lessons = [LessonItem(14, "第01讲 第1章绪论", "cw1", True)]
            plan = library.plan_tidy(root, [course])
            self.assertEqual(len(plan.moves), 1)
            self.assertEqual(
                plan.moves[0].target,
                root / "自动控制原理" / "课件" / "第01讲 第1章绪论.pdf",
            )
            done, failed = library.apply_tidy(plan)
            self.assertEqual(len(done), 1)
            self.assertFalse(failed)
            self.assertTrue((root / "自动控制原理" / "课件" / "第01讲 第1章绪论.pdf").is_file())
            self.assertFalse((root / "第01讲 第1章绪论.pdf").exists())

    def test_plan_tidy_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "第01讲 绪论.pdf", pages=2)
            in_place = self._pdf(root / "自动控制原理" / "课件" / "第01讲 绪论.pdf", pages=2)
            course = CourseItem("1", "202601", "自动控制原理", "24班", "2874")
            course.folder = "自动控制原理"
            course.lessons = [LessonItem(14, "第01讲 绪论", "cw1", True)]
            plan = library.plan_tidy(root, [course])
            # 已经在目标位置的那份算「在位」，散在外面的同名文件只报告不动
            self.assertEqual(len(plan.moves), 0)
            self.assertTrue(plan.kept or plan.duplicates)
            before = in_place.stat().st_mtime
            done, _failed = library.apply_tidy(plan)
            self.assertEqual(done, [])
            self.assertTrue(in_place.is_file())
            self.assertEqual(in_place.stat().st_mtime, before)

    def test_plan_tidy_reports_conflict_when_content_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "第01讲 绪论.pdf", pages=2)
            self._pdf(root / "自动控制原理" / "课件" / "第01讲 绪论.pdf", pages=5)
            course = CourseItem("1", "202601", "自动控制原理", "24班", "2874")
            course.folder = "自动控制原理"
            course.lessons = [LessonItem(14, "第01讲 绪论", "cw1", True)]
            plan = library.plan_tidy(root, [course], on_conflict="skip")
            self.assertEqual(plan.moves, [])
            self.assertTrue(plan.conflicts)

    def test_download_skips_file_found_elsewhere(self):
        slides._fetch_cover = lambda url, attempts=2: jpeg_bytes(640, 360)
        self.addCleanup(setattr, slides, "_fetch_cover", _real_fetch_cover)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = self._pdf(root / "旧目录" / "第10讲 根轨迹.pdf", pages=3)
            course = CourseItem("1", "202601", "自动控制原理", "24班", "2874")
            course.folder = "自动控制原理"
            lesson = LessonItem(14, "第10讲 根轨迹", "cw1", True)
            course.lessons = [lesson]
            index = library.Library(root).bind([course])

            def fetch(path, classroom_id, university_id):
                raise AssertionError("既然已经有了，就不该再请求接口")

            results = slides.download_lesson(fetch, course, lesson, root, existing=index)
            self.assertEqual(results[0].status, "已有跳过")
            self.assertIn("旧目录", results[0].filename)
            self.assertTrue(existing.is_file())


class ContentCheckTest(unittest.TestCase):
    def _pdf(self, path: Path, pages: int) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_pdf([page_from_bytes(jpeg_bytes(320, 180)) for _ in range(pages)], path)
        return path

    def test_page_count_and_similar(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "第04讲-第二章（3）.pdf", pages=7)
            self.assertEqual(library.page_count(root / "第04讲-第二章（3）.pdf"), 7)
            files = library.scan(root)
            near = library.similar_names(files, "第04讲-第二章（3）（2）")
            self.assertEqual([item.name for item in near], ["第04讲-第二章（3）.pdf"])

    def test_page_count_falls_back_when_header_unreadable(self):
        import shutil
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            path = self._pdf(Path(tmp) / "weird.pdf", pages=4)
            library._PAGE_CACHE.clear()
            with mock.patch.object(library, "count_pages", return_value=None):
                got = library.page_count(path)
            if shutil.which("pdfinfo"):
                self.assertEqual(got, 4)     # 退回 pdfinfo
            else:
                self.assertIsNone(got)

    def test_sha256_same_content_same_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._pdf(root / "a.pdf", pages=2)
            second = root / "换个名字.pdf"
            second.write_bytes(first.read_bytes())
            self.assertEqual(library.sha256(first), library.sha256(second))
            third = self._pdf(root / "b.pdf", pages=3)
            self.assertNotEqual(library.sha256(first), library.sha256(third))

    def test_recorded_download_recognized_after_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old_path = library._records_path
            library._records_path = lambda: root / "records.json"
            self.addCleanup(setattr, library, "_records_path", old_path)
            saved = self._pdf(root / "第10讲 根轨迹.pdf", pages=3)
            library.record_download(saved, "自动控制原理", "第10讲 根轨迹", 3)
            moved = root / "随便改的名字.pdf"
            saved.rename(moved)

            course = CourseItem("1", "202601", "自动控制原理", "24班", "2874")
            course.folder = "自动控制原理"
            lesson = LessonItem(14, "第10讲 根轨迹", "cw1", True)
            course.lessons = [lesson]
            kept = library.Library(root).bind([course])
            found = kept.matched(course, lesson)
            self.assertIsNotNone(found)
            self.assertEqual(found.path, moved)

    def test_same_name_different_content_prefers_course_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "课件" / "03神经系统的大体解剖.pdf", pages=10)
            self._pdf(root / "03神经系统的大体解剖.pdf", pages=7)
            course = CourseItem("1", "202601", "脑科学基础", "24班", "2874")
            course.folder = "课件"
            lesson = LessonItem(8, "03神经系统的大体解剖", "cw1", True, source="课件")
            course.lessons = [lesson]
            kept = library.Library(root).bind([course])
            found = kept.matched(course, lesson)
            self.assertIsNotNone(found)
            self.assertEqual(found.path, root / "课件" / "03神经系统的大体解剖.pdf")

    def test_same_name_different_content_outside_course_folder_is_left_for_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "A" / "第2章平面连杆机构.pdf", pages=94)
            self._pdf(root / "B" / "第2章平面连杆机构.pdf", pages=83)
            course = CourseItem("1", "202601", "机械设计基础", "24班", "2874")
            course.folder = "机械设计基础"
            lesson = LessonItem(14, "第2章平面连杆机构", "cw1", True)
            course.lessons = [lesson]
            kept = library.Library(root).bind([course])
            # 课程目录里没有，两份内容又不同 → 不自动认领，等下载时按页数核对
            self.assertIsNone(kept.matched(course, lesson))
            self.assertEqual(len(kept.near(lesson.title)), 2)

    def test_truncated_file_with_exact_name_is_not_trusted(self):
        slides._fetch_cover = lambda url, attempts=2: jpeg_bytes(640, 360)
        self.addCleanup(setattr, slides, "_fetch_cover", _real_fetch_cover)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 名字完全对得上，但只有 2 页 —— 典型的下一半的文件
            self._pdf(root / "脑科学基础" / "03神经系统的大体解剖.pdf", pages=2)
            course = CourseItem("1", "202601", "脑科学基础", "24班", "2874")
            course.folder = "脑科学基础"
            lesson = LessonItem(8, "03神经系统的大体解剖", "cw1", True, source="课件")
            course.lessons = [lesson]
            kept = library.Library(root).bind([course])
            self.assertIsNotNone(kept.matched(course, lesson))       # 名字确实对上了
            self.assertIsNone(kept.matched_ok(course, lesson))       # 但不采信
            self.assertEqual(len(kept.suspect([course])), 1)

            def fetch(path, classroom_id, university_id):
                if "review" in path:
                    return {"data": {"timelineList": [{"presentationId": "p1"}]}}
                return {
                    "data": {
                        "slideList": [
                            {"id": str(i), "index": i, "cover": f"https://example.invalid/{i}"}
                            for i in range(5)
                        ]
                    }
                }

            results = slides.download_lesson(fetch, course, lesson, root, existing=kept)
            self.assertEqual(results[0].status, "已下载")

    def test_content_match_skips_download_with_different_name(self):
        slides._fetch_cover = lambda url, attempts=2: jpeg_bytes(640, 360)
        self.addCleanup(setattr, slides, "_fetch_cover", _real_fetch_cover)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "旧名字" / "第04讲-第二章（3）.pdf", pages=3)
            course = CourseItem("1", "202601", "自动控制原理", "24班", "2874")
            course.folder = "自动控制原理"
            lesson = LessonItem(14, "第04讲-第二章（3）（2）", "cw1", True)
            course.lessons = [lesson]
            kept = library.Library(root).bind([course])
            self.assertIsNone(kept.matched(course, lesson))   # 名字对不上
            self.assertTrue(kept.near(lesson.title))          # 但名字相近

            def fetch(path, classroom_id, university_id):
                if "review" in path:
                    return {"data": {"timelineList": [{"presentationId": "p1"}]}}
                return {
                    "data": {
                        "slideList": [
                            {"id": str(i), "index": i, "cover": f"https://example.invalid/{i}"}
                            for i in range(3)
                        ]
                    }
                }

            results = slides.download_lesson(fetch, course, lesson, root, existing=kept)
            self.assertEqual(results[0].status, "已有跳过")
            self.assertIn("内容一致", results[0].detail)


class RetryTest(unittest.TestCase):
    def test_missing_page_retried_and_recovered(self):
        calls = {"cover/3": 0}

        def cover(url, attempts=2):
            if url.endswith("/3"):
                calls["cover/3"] += 1
                if calls["cover/3"] == 1:
                    raise OSError("第一次失败")
            index = int(url.rsplit("/", 1)[-1])
            return jpeg_bytes(480, 270, (index * 30 % 255, 0, 0))

        old = slides._fetch_cover
        slides._fetch_cover = cover
        self.addCleanup(setattr, slides, "_fetch_cover", old)
        pages = [
            {"id": str(i), "index": i, "cover": f"https://example.invalid/{i}"}
            for i in range(3, 6)
        ]
        got, missing = slides.download_pages(pages)
        self.assertEqual(missing, [])
        self.assertEqual(len(got), 3)

    def test_browser_gone_aborts_remaining_jobs(self):
        from yuketang_courseware.errors import BrowserError

        course = CourseItem("1", "202601", "自动控制原理", "24班", "2874")
        course.folder = "自动控制原理"
        jobs = [
            (course, LessonItem(14, "第1讲", "cw1", True)),
            (course, LessonItem(14, "第2讲", "cw2", True)),
            (course, LessonItem(14, "第3讲", "cw3", True)),
        ]
        asked = []

        def fetch(path, classroom_id, university_id):
            asked.append(path)
            raise BrowserError("浏览器调试端口不在了")

        with tempfile.TemporaryDirectory() as tmp:
            results = slides.download_selected(
                fetch, jobs, Path(tmp), lambda *args: None, threading.Event()
            )
        self.assertEqual(len(asked), 1)          # 只试了第一讲
        self.assertTrue(any("已中止" in item.detail for item in results))
        self.assertEqual(sum(1 for item in results if item.status == "失败"), 2)


class NamesTest(unittest.TestCase):
    def test_parse_url_reads_ids_from_query(self):
        url = parse_course_url(
            "https://changjiang.yuketang.cn/v2/web/studentLog?classroom_id=456&university_id=2874"
        )
        self.assertEqual(url.host, "changjiang.yuketang.cn")
        self.assertEqual(url.classroom_id, "456")
        self.assertEqual(url.university_id, "2874")
        self.assertEqual(url.origin, "https://changjiang.yuketang.cn")

    def test_parse_url_rejects_other_host(self):
        for text in ("", "https://example.com/x", "ftp://yuketang.cn/"):
            with self.assertRaises(ValueError):
                parse_course_url(text)

    def test_safe_component(self):
        self.assertEqual(safe_component('a/b:c*?"<>|d', "fallback"), "abcd")
        self.assertEqual(safe_component("", ""), "未命名")
        self.assertLessEqual(len(safe_component("长" * 200, "")), 51)

    def test_term_sort_key(self):
        self.assertEqual(
            sorted(["2025-2", "2026-1", "2024-2", "2026-2", "未知"], key=term_sort_key),
            ["2026-2", "2026-1", "2025-2", "2024-2", "未知"],
        )
        self.assertEqual(sorted(["2025", "2026"], key=term_sort_key), ["2026", "2025"])

    def test_api_path(self):
        self.assertEqual(api_path("/x", a=1, b=None), "/x?a=1")
        self.assertEqual(api_path("/x"), "/x")


_real_fetch_cover = slides._fetch_cover

if __name__ == "__main__":
    unittest.main()
