"""鲁棒性压测：专挑畸形输入、极端环境和半成品数据下手。

离线运行，不联网，不启动真实浏览器，不碰用户的 ~ 目录（HOME 全部重定向到临时目录）。

    python3 -m unittest tests.test_robustness -v
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from yuketang_courseware import catalog, cli, library, names, pdf, slides
from yuketang_courseware.catalog import CourseItem, LessonItem, assign_folders
from yuketang_courseware.errors import ApiError, BrowserError, LoginRequired
from yuketang_courseware.pdf import Page, PdfError, page_from_bytes, write_pdf


def jpeg(width: int = 640, height: int = 360, color=(120, 120, 120)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    return buf.getvalue()


class IsolatedHome:
    """把 HOME / XDG_* 指到临时目录，测试和真实用户目录彻底隔离。"""

    def __init__(self, case: unittest.TestCase) -> None:
        self._case = case
        self._tmp = tempfile.TemporaryDirectory()
        self._saved: dict[str, str | None] = {}

    def __enter__(self) -> Path:
        root = Path(self._tmp.name)
        for key in ("HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
            self._saved[key] = os.environ.get(key)
        (root / "home").mkdir()
        os.environ["HOME"] = str(root / "home")
        os.environ["XDG_CACHE_HOME"] = str(root / "cache")
        os.environ["XDG_DATA_HOME"] = str(root / "data")
        os.environ["XDG_CONFIG_HOME"] = str(root / "config")
        # 模块里的 Path.home() 缓存路径重新算一遍
        import importlib

        importlib.reload(settings)
        return root

    def __exit__(self, *_args) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import importlib

        importlib.reload(settings)
        self._tmp.cleanup()


from yuketang_courseware import settings  # noqa: E402  供 IsolatedHome 重载


# --------------------------------------------------------------------------
# 1. 接口返回可以是任何形状：不得崩，只降级
# --------------------------------------------------------------------------


class MalformedPayloadTest(unittest.TestCase):
    """真实平台会返 null、空串、数组、HTML、半截 JSON。每一种都不能让程序崩。"""

    GARBAGE = (
        None,
        {},
        {"data": None},
        {"data": []},
        {"data": "字符串"},
        {"data": {"course_chapter": None}},
        {"data": {"course_chapter": {"section_leaf_list": {}}}},
        {"data": {"course_chapter": [None, 3, "x", {"section_leaf_list": [None, 5]}]}},
        {"data": {"activities": "not a list"}},
        {"data": {"activities": [None, 7, {"type": None, "title": None}]}},
        {"data": {"timelineList": {"list": "x"}}},
        {"data": {"slideList": [None, {"cover": 123}, {"cover": ""}, {"cover": None}]}},
        {"data": {"course_sign": None, "uv_id": {}}},
    )

    def _course(self) -> CourseItem:
        return CourseItem("101", "2026-1", "自动控制原理", "01班", "2874")

    def test_parse_courses_never_raises(self):
        for payload in self.GARBAGE + ([], "", [None, {"classroom_id": ""}], {"data": {"list": [1, 2]}}):
            with self.subTest(payload=repr(payload)[:40]):
                courses = catalog.parse_courses(payload, "2874")  # type: ignore[arg-type]
                self.assertIsInstance(courses, list)

    def test_activity_lessons_never_raises(self):
        for payload in self.GARBAGE:
            with self.subTest(payload=repr(payload)[:40]):
                fetch = lambda path, cid, uid: payload  # type: ignore[assignment]
                lessons, no_activity = catalog.fetch_activity_lessons(fetch, self._course())
                self.assertIsInstance(lessons, list)
                self.assertIsInstance(no_activity, bool)

    def test_leaf_lessons_never_raises(self):
        for payload in self.GARBAGE:
            with self.subTest(payload=repr(payload)[:40]):
                fetch = lambda path, cid, uid: payload  # type: ignore[assignment]
                lessons = catalog.fetch_leaf_lessons(fetch, self._course())
                self.assertIsInstance(lessons, list)

    def test_presentation_ids_never_raises(self):
        for payload in self.GARBAGE + ({"data": {"timelineList": [{"presentationId": "none"}]}},):
            with self.subTest(payload=repr(payload)[:40]):
                self.assertIsInstance(slides.presentation_ids(payload), list)  # type: ignore[arg-type]

    def test_slide_pages_never_raises_and_drops_bad_covers(self):
        payload = {
            "data": {
                "slideList": [
                    {"id": 1, "index": 1, "cover": "https://a.example/1.jpg"},
                    {"id": 2, "index": 2, "cover": "javascript:alert(1)"},
                    {"id": 3, "index": 3, "cover": ""},
                    {"id": 1, "index": 1, "cover": "https://a.example/1.jpg"},  # 重复 id
                    {"id": 4, "index": "不是数字", "cover": "https://a.example/4.jpg"},
                    None,
                    "字符串",
                ]
            }
        }
        got = slides.slide_pages(payload)
        # 只留 http(s) 开头的封面：javascript: 和空地址都不能拿去下载
        self.assertEqual([str(s.get("id")) for s in got], ["1", "4"])

    def test_load_lessons_survives_both_sources_failing(self):
        def fetch(path, cid, uid):
            raise ApiError("接口挂了")

        course = self._course()
        catalog.load_lessons(fetch, course)
        self.assertTrue(course.loaded)
        self.assertEqual(course.lessons, [])
        self.assertIn("接口挂了", course.error)

    def test_load_lessons_propagates_login_required(self):
        def fetch(path, cid, uid):
            raise LoginRequired()

        course = self._course()
        with self.assertRaises(LoginRequired):
            catalog.load_lessons(fetch, course)

    def test_activity_pagination_cannot_spin_forever(self):
        """has_more 一直为 True 但内容重复：必须在有限次内停。"""
        calls = {"n": 0}

        def fetch(path, cid, uid):
            calls["n"] += 1
            return {
                "data": {
                    "activities": [{"type": 14, "title": "第1讲", "courseware_id": "cw1"}],
                    "has_more": True,
                }
            }

        catalog.fetch_activity_lessons(fetch, self._course())
        self.assertLessEqual(calls["n"], 50)

    def test_huge_university_and_classroom_ids_are_quoted(self):
        """路径里的 id 不能直接拼进 URL，否则能被注入额外的查询参数。"""
        course = CourseItem("1 2&actype=9", "2026-1", "课", "班", "28 74")
        asked = []

        def fetch(path, cid, uid):
            asked.append(path)
            return {"data": {}}

        catalog.fetch_activity_lessons(fetch, course)
        self.assertTrue(asked)
        self.assertNotIn("&actype=9", asked[0].split("?", 1)[0])
        self.assertIn("%20", asked[0])


# --------------------------------------------------------------------------
# 2. 下载：坏页隔离、并发安全、中断可停
# --------------------------------------------------------------------------


class DownloadRobustnessTest(unittest.TestCase):
    def setUp(self):
        self._old = slides._fetch_cover
        slides._ATTEMPTS = 1
        slides._PAGE_WORKERS = 4

    def tearDown(self):
        slides._fetch_cover = self._old
        slides._ATTEMPTS = 2

    def _course(self) -> CourseItem:
        course = CourseItem("101", "2026-1", "自动控制原理", "01班", "2874")
        course.folder = "自动控制原理"
        return course

    def test_page_worker_raising_weird_error_does_not_kill_lesson(self):
        """页面回调里抛的不是 OSError，也必须只丢这一页。"""

        def cover(url, attempts=2):
            if url.endswith("/5"):
                raise ValueError("解码器炸了")  # 非 OSError
            return jpeg(320, 180)

        slides._fetch_cover = cover
        pages = [{"id": str(i), "index": i, "cover": f"https://x.invalid/{i}"} for i in range(8)]
        got, missing = slides.download_pages(pages)
        self.assertEqual(len(got), 7)
        self.assertEqual(missing, [5])

    def test_all_pages_bad_reports_failure_not_crash(self):
        slides._fetch_cover = lambda url, attempts=2: (_ for _ in ()).throw(OSError("挂了"))
        pages = [{"id": "1", "index": 1, "cover": "https://x.invalid/1"}]
        got, missing = slides.download_pages(pages)
        self.assertEqual(got, [])
        self.assertEqual(missing, [1])

    def test_oversized_page_rejected(self):
        slides._fetch_cover = lambda url, attempts=2: b"\xff\xd8\xff" + b"0" * 1000
        pages = [{"id": "1", "index": 1, "cover": "https://x.invalid/1"}]
        got, missing = slides.download_pages(pages)
        self.assertEqual(got, [])
        self.assertEqual(missing, [1])

    def test_on_page_never_exceeds_total(self):
        slides._fetch_cover = lambda url, attempts=2: jpeg(100, 100)
        seen = []
        pages = [{"id": str(i), "index": i, "cover": f"https://x.invalid/{i}"} for i in range(12)]
        slides.download_pages(pages, on_page=lambda done, total: seen.append((done, total)))
        self.assertTrue(seen)
        self.assertTrue(all(done <= total for done, total in seen))
        self.assertEqual(seen[-1], (12, 12))

    def test_duplicate_index_pages_are_all_kept(self):
        slides._fetch_cover = lambda url, attempts=2: jpeg(200, 100)
        pages = [
            {"id": "a", "index": 3, "cover": "https://x.invalid/a"},
            {"id": "b", "index": 3, "cover": "https://x.invalid/b"},
        ]
        got, missing = slides.download_pages(pages)
        self.assertEqual(len(got), 2)

    def test_stop_event_aborts_whole_run(self):
        course = self._course()
        jobs = [(course, LessonItem(14, f"第{i}讲", f"cw{i}", True)) for i in range(5)]
        stop = threading.Event()
        stop.set()
        with tempfile.TemporaryDirectory() as tmp:
            results = slides.download_selected(
                lambda *a: (_ for _ in ()).throw(AssertionError("不该请求接口")),
                jobs,
                Path(tmp),
                lambda *a: None,
                stop,
            )
        self.assertEqual(len(results), 5)
        self.assertTrue(all(r.status == "失败" for r in results))

    def test_lesson_title_with_slash_stays_inside_folder(self):
        """讲次名里带 / 不能逃出保存目录。"""
        slides._fetch_cover = lambda url, attempts=2: jpeg(200, 100)

        def fetch(path, cid, uid):
            if "review" in path:
                return {"data": {"timelineList": [{"presentationId": "p1"}]}}
            return {"data": {"slideList": [{"id": "1", "index": 1, "cover": "https://x.invalid/1"}]}}

        course = self._course()
        lesson = LessonItem(14, "../../../../etc/passwd", "cw1", True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = slides.download_lesson(fetch, course, lesson, root)
            self.assertEqual(results[0].status, "已下载")
            produced = list((root / "自动控制原理" / "课件").iterdir())
            self.assertEqual(len(produced), 1)
            self.assertTrue(produced[0].parent.samefile(root / "自动控制原理" / "课件"))

    def test_very_long_title_does_not_break_filesystem(self):
        slides._fetch_cover = lambda url, attempts=2: jpeg(200, 100)

        def fetch(path, cid, uid):
            if "review" in path:
                return {"data": {"timelineList": [{"presentationId": "p1"}]}}
            return {"data": {"slideList": [{"id": "1", "index": 1, "cover": "https://x.invalid/1"}]}}

        course = self._course()
        lesson = LessonItem(14, "讲" * 500, "cw1", True)
        with tempfile.TemporaryDirectory() as tmp:
            results = slides.download_lesson(fetch, course, lesson, Path(tmp))
            self.assertEqual(results[0].status, "已下载")

    def test_unusable_directory_reports_failure(self):
        """保存位置建不出目录（比如那已经是个文件）时，报失败而不是崩。"""
        slides._fetch_cover = lambda url, attempts=2: jpeg(200, 100)

        def fetch(path, cid, uid):
            if "review" in path:
                return {"data": {"timelineList": [{"presentationId": "p1"}]}}
            return {"data": {"slideList": [{"id": "1", "index": 1, "cover": "https://x.invalid/1"}]}}

        course = self._course()
        lesson = LessonItem(14, "第1讲", "cw1", True)
        with tempfile.TemporaryDirectory() as tmp:
            blocked = Path(tmp) / "挡路"
            blocked.write_text("我是一个文件", encoding="utf-8")  # root 也绕不过去
            results = slides.download_lesson(fetch, course, lesson, blocked)
            self.assertEqual(results[0].status, "失败")
            self.assertIn("无法", results[0].detail)

    def test_concurrent_lessons_do_not_clobber_each_other(self):
        """多讲并发下载同一个课程目录，每个文件都要落到自己的名字上。"""
        slides._fetch_cover = lambda url, attempts=2: jpeg(240, 135)

        def fetch(path, cid, uid):
            if "review" in path:
                return {"data": {"timelineList": [{"presentationId": "p1"}]}}
            return {
                "data": {
                    "slideList": [
                        {"id": str(i), "index": i, "cover": f"https://x.invalid/{i}"}
                        for i in range(3)
                    ]
                }
            }

        course = self._course()
        stop = threading.Event()
        errors = []

        def one(index):
            try:
                slides.download_lesson(
                    fetch, course, LessonItem(14, f"第{index}讲", f"cw{index}", True), root
                )
            except Exception as exc:  # 并发下的任何异常都算失败
                errors.append(exc)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workers = [threading.Thread(target=one, args=(i,)) for i in range(6)]
            for w in workers:
                w.start()
            for w in workers:
                w.join(60)
            self.assertFalse(errors)
            names_found = sorted(p.name for p in (root / "自动控制原理" / "课件").iterdir())
            self.assertEqual(len(names_found), 6)
            self.assertEqual(len(set(names_found)), 6)


# --------------------------------------------------------------------------
# 3. PDF 写入：极端页数、畸形图片
# --------------------------------------------------------------------------


class PdfRobustnessTest(unittest.TestCase):
    def test_single_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "one.pdf"
            write_pdf([page_from_bytes(jpeg(100, 100))], dest)
            self.assertEqual(pdf.count_pages(dest), 1)
            self.assertTrue(pdf.looks_complete(dest))

    def test_many_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "many.pdf"
            write_pdf([page_from_bytes(jpeg(60, 40)) for _ in range(120)], dest)
            self.assertEqual(pdf.count_pages(dest), 120)

    def test_cmyk_jpeg_uses_correct_colorspace(self):
        buf = io.BytesIO()
        Image.new("CMYK", (80, 60), (0, 0, 0, 0)).save(buf, format="JPEG")
        page = page_from_bytes(buf.getvalue())
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "cmyk.pdf"
            write_pdf([page], dest)
            self.assertIn(b"/DeviceCMYK", dest.read_bytes())

    def test_grayscale_jpeg(self):
        buf = io.BytesIO()
        Image.new("L", (80, 60), 128).save(buf, format="JPEG")
        page = page_from_bytes(buf.getvalue())
        self.assertEqual(page.components, 1)

    def test_corrupt_jpeg_header_rejected(self):
        with self.assertRaises(PdfError):
            page_from_bytes(b"\xff\xd8\xff" + b"\x00" * 200)

    def test_zero_byte_image_rejected(self):
        with self.assertRaises(PdfError):
            page_from_bytes(b"")

    def test_truncated_jpeg_stream_rejected(self):
        data = jpeg(200, 200)
        with self.assertRaises(PdfError):
            page_from_bytes(data[: len(data) // 3])

    def test_partial_file_is_left_behind_only_as_dot_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "x.pdf"
            write_pdf([page_from_bytes(jpeg(50, 50))], dest)
            leftovers = [p.name for p in Path(tmp).iterdir()]
            self.assertEqual(leftovers, ["x.pdf"])

    def test_odd_page_sizes_do_not_stretch(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "mix.pdf"
            write_pdf(
                [
                    page_from_bytes(jpeg(1000, 500)),
                    page_from_bytes(jpeg(500, 1000)),
                    page_from_bytes(jpeg(1000, 500)),
                ],
                dest,
            )
            blob = dest.read_bytes()
            self.assertIn(b"/MediaBox [0 0 1000 500]", blob)
            self.assertIn(b"0.5 0 0 0.5", blob)  # 等比缩放，不拉伸

    def test_pdf_opens_in_real_reader(self):
        """有 pdfinfo / gs 就用真实解析器验一遍，避免只过自家 count_pages。"""
        import shutil

        if not shutil.which("pdfinfo"):
            self.skipTest("没有 pdfinfo")
        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "real.pdf"
            write_pdf([page_from_bytes(jpeg(300, 200)) for _ in range(5)], dest)
            out = subprocess.run(
                ["pdfinfo", str(dest)],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env={**os.environ, "LC_ALL": "C"},
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("Pages:           5", out.stdout)


# --------------------------------------------------------------------------
# 4. 文件名 / 路径：用户怎么起名都不能崩，也不能写错地方
# --------------------------------------------------------------------------


class NamesRobustnessTest(unittest.TestCase):
    def test_control_characters_and_dots(self):
        self.assertEqual(names.safe_component("..\x00隐藏", "fallback"), "隐藏")
        self.assertEqual(names.safe_component("...", "f"), "f")
        self.assertEqual(names.safe_component("", ""), "未命名")

    def test_emoji_and_rtl_names(self):
        for text in ("课件🎉第一讲", "درس ١", "🎉", "​零宽"):
            with self.subTest(text=text):
                got = names.safe_component(text, "fallback")
                self.assertTrue(got)
                self.assertNotIn("/", got)
                self.assertNotIn("\x00", got)

    def test_only_punctuation_falls_back(self):
        self.assertEqual(names.safe_component("///", "备用名"), "备用名")

    def test_url_variants(self):
        cases = (
            "  https://ChangJiang.Yuketang.cn/v2/web/index?classroom_id=1&university_id=2  ",
            "changjiang.yuketang.cn/v2/web/index?classroom_id=1",
            "https://changjiang.yuketang.cn:8443/x?classroom_id=9",
            "https://changjiang.yuketang.cn.",
        )
        for text in cases:
            with self.subTest(text=text):
                parsed = names.parse_course_url(text)
                self.assertTrue(parsed.host.endswith("yuketang.cn"))

    def test_url_rejects_nonsense(self):
        for text in ("", "   ", "not a url", "https://evil.com/?x=1", "javascript:alert(1)", "https://yuketang.cn.evil.com/"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    names.parse_course_url(text)

    def test_redact_hides_secrets(self):
        text = "GET https://x.yuketang.cn/a?token=abc123&csrftoken=zzz failed"
        got = names.redact(text)
        self.assertNotIn("abc123", got)
        self.assertNotIn("zzz", got)
        self.assertIn("[已隐藏]", got)

    def test_error_message_never_leaks_token(self):
        from yuketang_courseware.errors import CoursewareError

        err = CoursewareError("下载失败 https://x.yuketang.cn/c?token=SECRET")
        self.assertNotIn("SECRET", str(err))

    def test_folder_names_unique_even_with_identical_courses(self):
        courses = [
            CourseItem(str(i), "2026-1", "同名课", "同名课", None) for i in range(4)
        ]
        assign_folders(courses)
        folders = [c.folder for c in courses]
        self.assertEqual(len(set(folders)), 4)
        self.assertTrue(all(f for f in folders))


# --------------------------------------------------------------------------
# 5. 目录扫描：坏文件、无权限、超量文件
# --------------------------------------------------------------------------


class LibraryRobustnessTest(unittest.TestCase):
    def _pdf(self, path: Path, pages: int = 2) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_pdf([page_from_bytes(jpeg(120, 80)) for _ in range(pages)], path)
        return path

    def test_unreadable_subdirectory_is_skipped(self):
        if os.geteuid() == 0:
            self.skipTest("root 无视权限位，测不出来")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "好" / "a.pdf")
            locked = root / "锁住"
            locked.mkdir()
            self._pdf(locked / "b.pdf")
            os.chmod(locked, 0o000)
            try:
                files = library.scan(root)
            finally:
                # 必须在 with 里恢复：3.12 起 TemporaryDirectory 走 finalize，
                # 目录在 with 退出时就删了，addCleanup 再 chmod 会 ENOENT。
                os.chmod(locked, 0o755)
            self.assertEqual([f.name for f in files], ["a.pdf"])

    def test_file_that_vanishes_mid_scan_is_skipped(self):
        """扫到一半文件被删掉 / 坏掉的符号链接：跳过，不崩。"""
        import errno
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "a.pdf")
            real_stat = Path.stat

            def vanishing(self, *args, **kwargs):
                # 只让这个临时目录里的 stat 失败。全局替换会把 tempfile 清理、
                # 测试框架自己的操作也一起弄崩。
                if str(self).startswith(str(root)):
                    raise FileNotFoundError(errno.ENOENT, "没了")
                return real_stat(self, *args, **kwargs)

            with mock.patch.object(Path, "stat", vanishing):
                self.assertEqual(library.scan(root), [])

    def test_scan_survives_odd_oserror_from_stat(self):
        """stat 抛的不是 ENOENT（挂载点掉线、EIO 之类）时，扫描也不能整个炸掉。

        `Path.is_dir()` 只吞 ENOENT/ENOTDIR 这类 errno，其余会重新抛出；
        Python 3.13 起才放宽。这个用例盯的就是老解释器上的行为。
        """
        import errno
        from unittest import mock

        boom = OSError(errno.EIO, "I/O error")
        with mock.patch.object(Path, "stat", side_effect=boom):
            self.assertEqual(library.scan(Path("/nonexistent-scope")), [])

    def test_zero_byte_and_tiny_pdfs_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "空.pdf").write_bytes(b"")
            (root / "小.pdf").write_bytes(b"%PDF-1.4")
            self._pdf(root / "真.pdf")
            self.assertEqual([f.name for f in library.scan(root)], ["真.pdf"])

    def test_broken_pdf_does_not_crash_page_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "坏.pdf"
            path.write_bytes(b"%PDF-1.4\n" + os.urandom(4096))
            library._PAGE_CACHE.clear()
            self.assertIsInstance(library.page_count(path), (int, type(None)))

    def test_scan_limit_is_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(7):
                self._pdf(root / f"f{i}.pdf")
            self.assertEqual(len(library.scan(root, limit=3)), 3)

    def test_missing_directory_gives_empty_result(self):
        self.assertEqual(library.scan(Path("/nonexistent/目录")), [])

    def test_symlink_loop_does_not_hang(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "子").mkdir()
            try:
                (root / "子" / "回指").symlink_to(root)
            except OSError:
                self.skipTest("这个文件系统不支持符号链接")
            self._pdf(root / "a.pdf")
            files = library.scan(root)
            self.assertTrue(any(f.name == "a.pdf" for f in files))

    def test_tidy_never_deletes_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = self._pdf(root / "散落" / "第1讲.pdf", pages=4)
            occupied = self._pdf(root / "自动控制原理" / "课件" / "第1讲.pdf", pages=9)
            before = occupied.read_bytes()
            course = CourseItem("1", "2026-1", "自动控制原理", "01班", None)
            course.folder = "自动控制原理"
            course.lessons = [LessonItem(14, "第1讲", "cw1", True)]
            plan = library.plan_tidy(root, [course], on_conflict="skip")
            done, failed = library.apply_tidy(plan)
            self.assertEqual(done, [])
            self.assertTrue(src.is_file())          # 没删
            self.assertEqual(occupied.read_bytes(), before)  # 没覆盖

    def test_tidy_with_no_courses_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = self._pdf(root / "a.pdf")
            plan = library.plan_tidy(root, [])
            self.assertEqual(plan.moves, [])
            library.apply_tidy(plan)
            self.assertTrue(src.is_file())

    def test_records_file_survives_corruption(self):
        with IsolatedHome(self) as root:
            target = library._records_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("{ this is not json", encoding="utf-8")
            self.assertEqual(library.load_records(), {})

    def test_leaf_cache_file_survives_corruption(self):
        with IsolatedHome(self) as root:
            target = catalog._cache_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("[]", encoding="utf-8")
            self.assertEqual(catalog._read_cache(), {})

    def test_settings_roundtrip(self):
        with IsolatedHome(self):
            settings.save("https://x.yuketang.cn/?classroom_id=1", "/tmp/课件")
            got = settings.load()
            self.assertEqual(got["url"], "https://x.yuketang.cn/?classroom_id=1")
            self.assertEqual(got["directory"], "/tmp/课件")

    def test_settings_survives_corruption(self):
        with IsolatedHome(self) as root:
            settings.PATH.parent.mkdir(parents=True, exist_ok=True)
            settings.PATH.write_text("not json", encoding="utf-8")
            self.assertEqual(settings.load(), {})


# --------------------------------------------------------------------------
# 6. 命令行：参数和环境不对时给明确提示，不吐栈
# --------------------------------------------------------------------------


class CliRobustnessTest(unittest.TestCase):
    def test_no_url(self):
        self.assertEqual(cli.main([]), 2)

    def test_bad_url(self):
        self.assertEqual(cli.main(["--url", "https://example.com/x"]), 2)

    def test_bad_jobs(self):
        self.assertEqual(
            cli.main(["--url", "https://x.yuketang.cn/a", "--jobs", "0"]), 2
        )

    def test_version_prints(self):
        import contextlib

        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            with self.assertRaises(SystemExit):
                cli.main(["--version"])
        self.assertIn("yuketang-courseware", out.getvalue())

    def test_relative_out_is_under_current_directory(self):
        """--out 课件 应当落在当前目录下面，不能悄悄写进家目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            self.addCleanup(os.chdir, cwd)
            self.assertEqual(names.resolve_directory("课件"), Path(tmp) / "课件")
            self.assertEqual(names.resolve_directory("."), Path(tmp))
            self.assertTrue(str(names.resolve_directory("~/课件")).startswith("/"))

    def test_help_lists_every_flag(self):
        text = cli.build_parser().format_help()
        for flag in ("--url", "--out", "--all", "--tidy", "--dry-run", "--source", "--jobs"):
            self.assertIn(flag, text)


# --------------------------------------------------------------------------
# 7. 浏览器缺失 / 无图形界面：降级而不是崩
# --------------------------------------------------------------------------


class BrowserRobustnessTest(unittest.TestCase):
    def test_missing_browser_gives_actionable_message(self):
        from unittest import mock

        from yuketang_courseware import browser as browser_mod
        from yuketang_courseware.errors import BrowserMissing

        with mock.patch.object(browser_mod, "find_browser", return_value=None):
            with tempfile.TemporaryDirectory() as tmp:
                b = browser_mod.CoursewareBrowser(data_dir=Path(tmp) / "profile")
                with self.assertRaises(BrowserMissing):
                    b.open_url("https://x.yuketang.cn/a")

    def test_fetch_without_browser_raises_browser_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            from yuketang_courseware.browser import CoursewareBrowser

            b = CoursewareBrowser(data_dir=Path(tmp) / "profile")
            with self.assertRaises(BrowserError):
                b.fetch_json("https://x.yuketang.cn", "x.yuketang.cn", "/a", None, None)

    def test_terminate_profile_refuses_dangerous_paths(self):
        from yuketang_courseware.browser import CoursewareBrowser

        b = CoursewareBrowser(data_dir=Path("/"))
        b.terminate_profile()  # 不能对根目录动手
        b2 = CoursewareBrowser(data_dir=Path.home())
        b2.terminate_profile()  # 不能对家目录动手

    def test_parse_api_body_variants(self):
        from yuketang_courseware.browser import parse_api_body

        with self.assertRaises(LoginRequired):
            parse_api_body(200, "<html><body>登录</body></html>")
        with self.assertRaises(LoginRequired):
            parse_api_body(401, json.dumps({"errcode": 1}))
        with self.assertRaises(LoginRequired):
            parse_api_body(200, json.dumps({"errcode": 1, "errmsg": "请先登录"}))
        with self.assertRaises(ApiError):
            parse_api_body(200, "not json at all")
        with self.assertRaises(ApiError):
            parse_api_body(200, json.dumps([1, 2, 3]))
        self.assertEqual(parse_api_body(200, json.dumps({"errcode": 0, "data": {}})), {"errcode": 0, "data": {}})

    def test_js_fetch_escapes_injected_ids(self):
        """classroom_id / university_id 来自用户粘的网址，进 JS 前必须转义。"""
        from yuketang_courseware.browser import _js_fetch

        script = _js_fetch("https://x.yuketang.cn", "/a", 'c"};alert(1);//', "1")
        self.assertNotIn('c"};alert(1);//"', script)   # 引号被转义了
        self.assertIn('c\\"};alert(1);//', script)
        # 换行也逃不出字符串
        script2 = _js_fetch("https://x.yuketang.cn", "/a", "1\n2", None)
        self.assertNotIn("\n", script2.split("const headers = ")[1].split(";")[0])


class MatchingEdgeCaseTest(unittest.TestCase):
    """各人的目录习惯不一样：改名、挪位置、多份同名、只能靠页数核对。

    原则是「拿不准就重下」——宁可多下一次，也不能因为猜错而漏掉一讲。
    """

    def _pdf(self, path: Path, pages: int = 5, pad: int = 0) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_pdf([page_from_bytes(jpeg(160, 90)) for _ in range(pages)], path)
        if pad:  # 造出字节数不同、页数相同的文件（同名不同来源）
            with path.open("ab") as handle:
                handle.write(b"\n% pad\n" + b"x" * pad)
        return path

    def _course(self, folder: str = "自动控制原理", name: str = "自动控制原理") -> CourseItem:
        course = CourseItem("1", "2026-1", name, "01班", "2874")
        course.folder = folder
        return course

    def test_renamed_and_moved_file_is_still_recognized(self):
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = self._course()
            lesson = LessonItem(14, "第10讲 根轨迹", "cw1", True)
            course.lessons = [lesson]
            saved = self._pdf(root / "自动控制原理" / "课件" / "第10讲 根轨迹.pdf", pages=4)
            library.record_download(saved, "自动控制原理", "第10讲 根轨迹", 4)
            moved = root / "乱放" / "深层" / "随便改的名.pdf"
            moved.parent.mkdir(parents=True, exist_ok=True)
            saved.rename(moved)
            kept = library.Library(root).bind([course])
            found = kept.matched(course, lesson)
            self.assertIsNotNone(found)
            self.assertEqual(found.path, moved)

    def test_our_own_short_file_is_trusted(self):
        """真的一讲只有 2 页：自己下过就别每次都重下。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = self._course()
            lesson = LessonItem(14, "小讲次", "cw1", True)
            course.lessons = [lesson]
            saved = self._pdf(root / "自动控制原理" / "课件" / "小讲次.pdf", pages=2)
            library.record_download(saved, "自动控制原理", "小讲次", 2)
            kept = library.Library(root).bind([course])
            self.assertIsNotNone(kept.matched_ok(course, lesson))
            self.assertEqual(kept.suspect([course]), [])

    def test_foreign_short_file_is_still_suspect(self):
        """不是我们下出来的 2 页文件（多半是下一半），照样重下。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = self._course()
            lesson = LessonItem(14, "小讲次", "cw1", True)
            course.lessons = [lesson]
            self._pdf(root / "自动控制原理" / "课件" / "小讲次.pdf", pages=2)
            kept = library.Library(root).bind([course])
            self.assertIsNone(kept.matched_ok(course, lesson))
            self.assertEqual(len(kept.suspect([course])), 1)

    def test_same_page_count_unrelated_name_is_not_skipped(self):
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = self._course()
            lesson = LessonItem(14, "第01讲 绪论", "cw1", True)
            course.lessons = [lesson]
            self._pdf(root / "完全另一门课" / "期末复习提纲.pdf", pages=6)
            kept = library.Library(root).bind([course])
            self.assertIsNone(kept.content_match(course, lesson, 6))

    def test_two_candidates_same_pages_is_ambiguous(self):
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = self._course()
            # 讲次名跟两份文件都不完全一样，只能靠「相近 + 页数」判断
            lesson = LessonItem(14, "第04讲 第二章（3）(4)", "cw1", True)
            course.lessons = [lesson]
            self._pdf(root / "A" / "第04讲-第二章（3）.pdf", pages=7, pad=10)
            self._pdf(root / "B" / "第04讲-第二章（3）（2）.pdf", pages=7, pad=50)
            kept = library.Library(root).bind([course])
            self.assertIsNone(kept.matched(course, lesson))      # 名字没对上
            self.assertEqual(len(kept.near(lesson.title)), 2)    # 两份都相近
            self.assertIsNone(kept.content_match(course, lesson, 7))  # 有歧义 → 不认

    def test_one_file_never_claimed_by_two_lessons(self):
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = self._course()
            first = LessonItem(14, "第04讲 第二章", "cw1", True)
            second = LessonItem(14, "第04讲-第二章（3）", "cw2", True)
            course.lessons = [first, second]
            self._pdf(root / "自动控制原理" / "课件" / "第04讲 第二章.pdf", pages=7)
            kept = library.Library(root).bind([course])
            own = [kept.matched(course, l) for l in (first, second)]
            self.assertEqual(sum(1 for item in own if item is not None), 1)

    def test_content_match_prefers_course_folder(self):
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = self._course()
            lesson = LessonItem(14, "第04讲 第二章", "cw1", True)
            course.lessons = [lesson]
            self._pdf(root / "别的课" / "第04讲 第二章（1）.pdf", pages=7, pad=10)
            mine = self._pdf(root / "自动控制原理" / "课件" / "第04讲 第二章（2）.pdf", pages=7, pad=50)
            kept = library.Library(root).bind([course])
            got = kept.content_match(course, lesson, 7)
            self.assertIsNotNone(got)
            self.assertEqual(got.path, mine)

    def test_tiny_page_count_is_not_evidence(self):
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = self._course()
            lesson = LessonItem(14, "第01讲 绪论", "cw1", True)
            course.lessons = [lesson]
            self._pdf(root / "第01讲 绪论（旧）.pdf", pages=2)
            kept = library.Library(root).bind([course])
            self.assertIsNone(kept.content_match(course, lesson, 2))

    def test_big_directory_does_not_hash_every_file(self):
        """一堆文件时不能给每个文件都算 sha256——那会把界面卡死。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            course = self._course()
            course.lessons = [LessonItem(14, f"第{i}讲", f"cw{i}", True) for i in range(5)]
            for i in range(120):
                self._pdf(root / "散" / f"x{i}.pdf", pages=3)
            hashed = {"n": 0}
            real = library.sha256

            def counting(path):
                hashed["n"] += 1
                return real(path)

            library.sha256 = counting
            self.addCleanup(setattr, library, "sha256", real)
            library.Library(root).bind([course])
            self.assertLess(hashed["n"], 10)

    def test_scan_reports_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(5):
                self._pdf(root / f"f{i}.pdf")
            stats: dict = {}
            library.scan(root, limit=3, stats=stats)
            self.assertTrue(stats["truncated"])
            self.assertEqual(stats["scanned"], 3)
            full: dict = {}
            library.scan(root, stats=full)
            self.assertFalse(full["truncated"])


class SmartTidyTest(unittest.TestCase):
    """整理要认人的命名习惯：中文数字、全角、自己简写的目录、一讲多套幻灯片。"""

    def _pdf(self, path: Path, pages: int = 4, pad: int = 0) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_pdf([page_from_bytes(jpeg(160, 90)) for _ in range(pages)], path)
        if pad:
            with path.open("ab") as handle:
                handle.write(b"\n% pad\n" + b"x" * pad)
        return path

    def _course(self, titles, folder="自动控制原理", name="自动控制原理") -> CourseItem:
        course = CourseItem("1", "2026-1", name, "01班", "2874")
        course.folder = folder
        course.lessons = [
            LessonItem(14, title, f"cw{index}", True) for index, title in enumerate(titles)
        ]
        return course

    def test_chinese_numerals_match_arabic(self):
        self.assertEqual(library.normalize("第十二讲"), library.normalize("第12讲"))
        self.assertEqual(library.normalize("第二章 绪论"), library.normalize("第2章 绪论"))
        self.assertEqual(library.normalize("第一百零三页"), library.normalize("第103页"))
        self.assertEqual(library.normalize("二〇二六"), library.normalize("2026"))

    def test_fullwidth_and_spacing_variants(self):
        self.assertEqual(library.normalize("第１２讲"), library.normalize("第12讲"))
        self.assertEqual(library.normalize("第12讲（1）"), library.normalize("第12讲 1"))
        self.assertEqual(library.normalize("第三章-回归分析"), library.normalize("第三章 回归分析"))

    def test_follows_users_own_folder_name(self):
        """用户自己管这门课叫「自控」：别在同一个保存目录里再建一个「自动控制原理」。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("第一讲 绪论", "第二讲 传递函数", "第三讲 频域"):
                self._pdf(root / "自控" / "课件" / f"{name}.pdf")
            course = self._course(["第1讲 绪论", "第2讲 传递函数", "第3讲 频域"])
            plan = library.plan_tidy(root, [course])
            targets = sorted(str(move.target.relative_to(root)) for move in plan.moves)
            self.assertTrue(all(t.startswith("自控/课件/") for t in targets), targets)
            self.assertFalse((root / "自动控制原理").exists())
            self.assertEqual(len(plan.moves), 3)

    def test_keeps_flat_layout_when_user_has_no_subfolder(self):
        """用户习惯直接放在课程目录下，就别硬加一层「课件」。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "自控" / "第1讲 绪论.pdf")
            self._pdf(root / "自控" / "第2讲 传递函数.pdf")
            course = self._course(["第1讲 绪论", "第2讲 传递函数", "第3讲 频域"])
            self._pdf(root / "散落" / "第3讲 频域.pdf")
            plan = library.plan_tidy(root, [course])
            got = [str(move.target.relative_to(root)) for move in plan.moves]
            self.assertEqual(got, ["自控/第3讲 频域.pdf"])

    def test_all_presentations_of_one_lesson_are_moved(self):
        """一讲两套幻灯片（标题.pdf + 标题-1.pdf）都要归位，不能只搬一个。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "散" / "第1讲 绪论.pdf", pages=3, pad=1)
            self._pdf(root / "散" / "第1讲 绪论-1.pdf", pages=5, pad=9)
            course = self._course(["第1讲 绪论"])
            plan = library.plan_tidy(root, [course])
            got = sorted(str(move.target.relative_to(root)) for move in plan.moves)
            self.assertEqual(
                got,
                ["自动控制原理/课件/第1讲 绪论-1.pdf", "自动控制原理/课件/第1讲 绪论.pdf"],
            )
            done, failed = library.apply_tidy(plan)
            self.assertEqual(len(done), 2)
            self.assertFalse(failed)

    def test_same_name_same_content_is_a_duplicate_not_a_conflict(self):
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = self._pdf(root / "自动控制原理" / "课件" / "第1讲 绪论.pdf", pages=4)
            copy = root / "别处" / "第1讲 绪论.pdf"
            copy.parent.mkdir(parents=True)
            copy.write_bytes(target.read_bytes())
            course = self._course(["第1讲 绪论"])
            plan = library.plan_tidy(root, [course])
            self.assertFalse(plan.conflicts)
            self.assertTrue(plan.duplicates)
            self.assertFalse(plan.moves)
            self.assertTrue(copy.is_file())     # 重复的也不删

    def test_same_name_different_content_keeps_both(self):
        """照 aria2 / Syncthing 的规矩：绝不覆盖，来的这份另存，两份都留。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            existing = self._pdf(root / "自动控制原理" / "课件" / "第1讲 绪论.pdf", pages=4)
            before = existing.read_bytes()
            incoming = self._pdf(root / "别处" / "第1讲 绪论.pdf", pages=9)
            course = self._course(["第1讲 绪论"])
            plan = library.plan_tidy(root, [course])
            self.assertEqual(len(plan.moves), 1)
            target = plan.moves[0].target
            self.assertIn("另一版本", target.name)
            done, failed = library.apply_tidy(plan)
            self.assertEqual(len(done), 1)
            self.assertFalse(failed)
            self.assertEqual(existing.read_bytes(), before)   # 原位那份没被动
            self.assertTrue(target.is_file())                 # 新来的也在
            self.assertFalse(incoming.exists())
            self.assertNotEqual(existing.stat().st_size, target.stat().st_size)

    def test_skip_mode_reports_conflict_without_moving(self):
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "自动控制原理" / "课件" / "第1讲 绪论.pdf", pages=4)
            loose = self._pdf(root / "别处" / "第1讲 绪论.pdf", pages=9)
            course = self._course(["第1讲 绪论"])
            plan = library.plan_tidy(root, [course], on_conflict="skip")
            self.assertTrue(plan.conflicts)
            self.assertFalse(plan.moves)
            note = plan.conflicts[0].note()
            self.assertIn("9 页", note)
            self.assertIn("4 页", note)
            library.apply_tidy(plan)
            self.assertTrue(loose.is_file())

    def test_conflict_copies_are_capped_and_not_reported_as_unmatched(self):
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "自动控制原理" / "课件"
            self._pdf(folder / "第1讲 绪论.pdf", pages=3)
            for index in range(library._CONFLICT_CAP + 3):
                self._pdf(root / "散" / f"v{index}.pdf", pages=4, pad=index)
                (root / "散" / f"v{index}.pdf").rename(
                    root / "散" / f"第1讲 绪论-{index + 1}.pdf"
                )
            course = self._course(["第1讲 绪论"])
            plan = library.plan_tidy(root, [course])
            moved = [m for m in plan.moves if "另一版本" in m.target.name]
            self.assertLessEqual(len(moved), library._CONFLICT_CAP)
            self.assertFalse(
                [item for item in plan.unmatched if "另一版本" in item.name]
            )

    def test_unrecognised_files_are_reported_not_touched(self):
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stray = self._pdf(root / "考研英语真题.pdf")
            course = self._course(["第1讲 绪论"])
            plan = library.plan_tidy(root, [course])
            self.assertEqual([item.name for item in plan.unmatched], ["考研英语真题.pdf"])
            self.assertIn("没认出来", library.summarize(plan))
            library.apply_tidy(plan)
            self.assertTrue(stray.is_file())

    def test_cross_device_move_falls_back_to_copy(self):
        """保存目录在 U 盘/另一个分区时 os.replace 会 EXDEV，不能整个失败。"""
        import errno
        from unittest import mock

        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._pdf(root / "散" / "第1讲 绪论.pdf")
            course = self._course(["第1讲 绪论"])
            plan = library.plan_tidy(root, [course])
            real_replace = os.replace

            def flaky(src, dst, *args, **kwargs):
                if str(dst).endswith("第1讲 绪论.pdf"):
                    raise OSError(errno.EXDEV, "跨设备")
                return real_replace(src, dst, *args, **kwargs)

            with mock.patch("os.replace", side_effect=flaky):
                done, failed = library.apply_tidy(plan)
            self.assertEqual(len(done), 1)
            self.assertFalse(failed)
            self.assertFalse(source.exists())
            self.assertTrue((root / "自动控制原理" / "课件" / "第1讲 绪论.pdf").is_file())

    def test_two_courses_never_share_one_folder(self):
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._course(["第1讲 甲"])
            second = CourseItem("2", "2026-1", "自动控制原理", "02班", "2874")
            second.folder = "自动控制原理"
            second.lessons = [LessonItem(14, "第1讲 甲", "cwB", True)]
            plan = library.plan_tidy(root, [first, second])
            targets = {str(move.target.relative_to(root)).split("/")[0] for move in plan.moves}
            self.assertEqual(len(targets), len(plan.moves))


    def test_decks_renumbered_without_fake_conflicts(self):
        """真实场景：`-1`、`-2` 两套幻灯片，改名时前者还占着 `-1` 这个名字。

        以前会把 `-2` 误判成「目标已存在、内容不同」，硬塞成「另一版本」。
        """
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._pdf(root / "课件" / "第3讲 频域-1.pdf", pages=3, pad=1)
            second = self._pdf(root / "课件" / "第3讲 频域-2.pdf", pages=5, pad=9)
            course = self._course(["第3讲 频域"])
            plan = library.plan_tidy(root, [course])
            self.assertFalse(plan.conflicts)
            self.assertFalse([m for m in plan.moves if "另一版本" in m.target.name])
            got = sorted((m.source.name, m.target.name) for m in plan.moves)
            self.assertEqual(
                got,
                [("第3讲 频域-1.pdf", "第3讲 频域.pdf"), ("第3讲 频域-2.pdf", "第3讲 频域-1.pdf")],
            )
            done, failed = library.apply_tidy(plan)
            self.assertEqual(len(done), 2)
            self.assertFalse(failed)
            folder = root / "自动控制原理" / "课件"
            self.assertEqual(sorted(p.name for p in folder.iterdir()),
                             ["第3讲 频域-1.pdf", "第3讲 频域.pdf"])
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

    def test_unmatched_ignores_other_courses_folders(self):
        """只整理一门课时，别的课程的文件夹不该刷屏说「没认出来」。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "另一门课" / "课件" / "随便.pdf")
            stray = self._pdf(root / "根目录里的.pdf")
            course = self._course(["第1讲 绪论"])
            plan = library.plan_tidy(root, [course])
            names = [item.name for item in plan.unmatched]
            self.assertIn(stray.name, names)
            self.assertNotIn("随便.pdf", names)


class TidyNoiseTest(unittest.TestCase):
    """整理别自作主张：不复活归档、不搬空用户自己的子目录、不堆「另一版本」副本。

    这几条都是真机撞出来的：点一次「整理」，弹窗里冒出 19 条移动、3 个副本、
    62 条「没认出来」，绝大多数是笔记 PDF 和 `_archive` 里的旧课件。
    """

    def _pdf(self, path: Path, pages: int = 4, pad: int = 0) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        write_pdf([page_from_bytes(jpeg(160, 90)) for _ in range(pages)], path)
        if pad:
            with path.open("ab") as handle:
                handle.write(b"\n% pad\n" + b"x" * pad)
        return path

    def _course(self, titles, folder="自动控制原理", name="自动控制原理") -> CourseItem:
        course = CourseItem("1", "2026-1", name, "01班", "2874")
        course.folder = folder
        course.lessons = [
            LessonItem(14, title, f"cw{index}", True) for index, title in enumerate(titles)
        ]
        return course

    def test_archive_is_not_scanned(self):
        """`_archive/` 里作废的旧课件不能被当成「另一讲版本」搬出来复活。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "自控" / "自动控制课件" / "第1讲 绪论.pdf", pages=6)
            self._pdf(root / "自控" / "习题" / "_archive" / "第7讲-第四章（2）.pdf", pages=51)
            course = self._course(["第7讲-第四章（2）"])
            files = library.scan(root)
            self.assertEqual([item.name for item in files], ["第1讲 绪论.pdf"])
            plan = library.plan_tidy(root, [course], files)
            self.assertFalse(plan.moves)

    def test_adopts_users_own_subfolder_instead_of_creating_one(self):
        """已经有 `自控/自动控制课件/` 就不要在旁边再建一个空 `课件/` 把文件搬走。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "自控" / "自动控制课件" / "第1讲 绪论.pdf", pages=5)
            self._pdf(root / "自控" / "自动控制课件" / "第2讲 传递函数.pdf", pages=5)
            self._pdf(root / "散" / "第3讲 频域.pdf", pages=5)
            course = self._course(["第1讲 绪论", "第2讲 传递函数", "第3讲 频域"])
            plan = library.plan_tidy(root, [course])
            targets = [str(move.target.relative_to(root)) for move in plan.moves]
            self.assertEqual(targets, ["自控/自动控制课件/第3讲 频域.pdf"])
            self.assertFalse((root / "自控" / "课件").exists())

    def test_lesson_names_that_only_differ_by_parens_do_not_make_copies(self):
        """平台两讲叫「第二章 基本数据结构及其运算」和「… （2）」：都在课件目录里就别造副本。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "软件技术基础" / "课件"
            self._pdf(folder / "第二章 基本数据结构及其运算-1.pdf", pages=40, pad=1)
            self._pdf(folder / "第二章 基本数据结构及其运算-2.pdf", pages=48, pad=2)
            self._pdf(folder / "第二章 基本数据结构及其运算 （2）.pdf", pages=52, pad=3)
            course = self._course(
                ["第二章  基本数据结构及其运算", "第二章  基本数据结构及其运算 （2）"],
                folder="软件技术基础",
                name="软件技术基础",
            )
            plan = library.plan_tidy(root, [course])
            self.assertFalse(
                [move for move in plan.moves if "另一版本" in move.target.name],
                [move.target.name for move in plan.moves],
            )
            # 三份都在课件目录里、名字也都说得通：一份都别动。
            self.assertFalse(plan.moves)
            self.assertEqual(
                {path.name for path in folder.iterdir()},
                {
                    "第二章 基本数据结构及其运算-1.pdf",
                    "第二章 基本数据结构及其运算-2.pdf",
                    "第二章 基本数据结构及其运算 （2）.pdf",
                },
            )

    def test_truncated_file_is_reported_not_parked_as_another_version(self):
        """2 页的残片不是「另一个版本」，另存进去只是多一个坏文件。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "脑科学" / "课件" / "03神经系统的大体解剖.pdf", pages=104)
            junk = self._pdf(root / "脑科学" / "03神经系统的大体解剖.pdf", pages=2)
            course = self._course(["03神经系统的大体解剖"], folder="脑科学", name="脑科学基础")
            plan = library.plan_tidy(root, [course])
            self.assertFalse([move for move in plan.moves if "另一版本" in move.target.name])
            self.assertTrue(plan.conflicts)
            note = plan.conflicts[0].note()
            self.assertIn("2 页", note)
            self.assertIn("104 页", note)
            self.assertTrue(junk.exists())

    def test_notes_and_homework_are_counted_not_listed(self):
        """笔记、习题、提交里的 PDF 不是课件：只给一个计数，不逐条刷屏。"""
        with IsolatedHome(self), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._pdf(root / "自控" / "课件" / "第1讲 绪论.pdf", pages=5)
            self._pdf(root / "自控" / "笔记" / "第1讲-知识库.pdf", pages=30)
            self._pdf(root / "自控" / "习题" / "提交" / "第1讲-习题解答.pdf", pages=6)
            self._pdf(root / "自控" / "figs" / "fig1.pdf", pages=2)
            course = self._course(["第1讲 绪论"], folder="自控")
            plan = library.plan_tidy(root, [course])
            self.assertFalse(plan.unmatched)
            self.assertEqual(plan.elsewhere, 3)
            summary = library.summarize(plan)
            self.assertIn("另有 3 个不在课件目录里", summary)
            self.assertNotIn("没认出来", summary)


if __name__ == "__main__":
    unittest.main()
