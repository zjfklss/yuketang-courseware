"""把一讲的幻灯片收成 PDF。

顺序：review 的 timelineList 收集不重复的 presentationId，再按 presentationId 取
ppt 的 slideList，逐页下载 cover，按页号排好，直接嵌入 PDF。

封面地址自带 token，只在本进程里请求，不写进状态、不写进文件名。页面不落临时
文件，全部在内存里拼装，写完就释放。某一页失败只少这一页，其余页照常合成。
"""

from __future__ import annotations

import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from yuketang_courseware import library
from yuketang_courseware.catalog import CourseItem, LessonItem
from yuketang_courseware.errors import ApiError, BrowserError, LoginRequired
from yuketang_courseware.names import api_path, safe_component
from yuketang_courseware.pdf import Page, PdfError, looks_complete, page_from_bytes, write_pdf

_MIN_IMAGE = 400
_MAX_IMAGE = 30 * 1024 * 1024
_TIMEOUT = 15
_ATTEMPTS = 2
_PAGE_WORKERS = 6
from yuketang_courseware import __version__

_USER_AGENT = f"yuketang-courseware/{__version__}"


@dataclass
class FileResult:
    status: str
    course: str
    filename: str
    detail: str = ""
    folder: str = ""


def presentation_ids(review) -> list[str]:
    """接口返回可能是 null、字符串、数组——一律当成「没有幻灯片」，不抛异常。"""
    if not isinstance(review, dict):
        return []
    data = review.get("data")
    if not isinstance(data, dict):
        return []
    timeline = data.get("timelineList") or []
    if isinstance(timeline, dict):
        timeline = timeline.get("list") or []
    if not isinstance(timeline, list):
        return []
    found: list[str] = []
    for item in timeline:
        if not isinstance(item, dict):
            continue
        raw = item.get("presentationId") or item.get("pres") or ""
        pid = str(raw).strip()
        if not pid or pid.lower() == "none" or pid in found:
            continue
        found.append(pid)
    return found


def slide_pages(ppt) -> list[dict]:
    """只留能下载、能排序的页。

    排序按 index；index 不是数字（有的课给的是空值或文字）时按它在列表里的原始
    位置排，别让它跳到第 0 页去把整讲的页码顺序打乱。
    """
    if not isinstance(ppt, dict):
        return []
    data = ppt.get("data")
    if not isinstance(data, dict):
        return []
    slides = data.get("slideList") or []
    if not isinstance(slides, list):
        return []
    chosen: list[tuple[int, dict]] = []
    seen: set[str] = set()
    for position, slide in enumerate(slides):
        if not isinstance(slide, dict):
            continue
        cover = slide.get("cover")
        if not isinstance(cover, str) or not cover.startswith(("http://", "https://")):
            continue
        key = str(slide.get("id") or "") or cover
        if key in seen:
            continue
        seen.add(key)
        chosen.append((position, slide))

    def sort_key(pair: tuple[int, dict]) -> tuple:
        position, slide = pair
        try:
            index = int(slide.get("index"))
        except (TypeError, ValueError):
            return (1, position, str(slide.get("id") or ""))
        return (0, index, str(slide.get("id") or ""))

    chosen.sort(key=sort_key)
    return [slide for _position, slide in chosen]


def download_selected(
    fetch,
    jobs: list[tuple[CourseItem, LessonItem]],
    save_root: Path,
    on_progress,
    stop,
    existing: library.Library | None = None,
    on_page=None,
) -> list[FileResult]:
    results: list[FileResult] = []
    total = len(jobs)
    for index, (course, lesson) in enumerate(jobs, start=1):
        if stop.is_set():
            results.append(
                FileResult("失败", course.course_name, lesson.title, "已停止", course.folder)
            )
            continue
        on_progress(index - 1, total, f"正在下载：{course.course_name} / {lesson.title}")
        try:
            results.extend(
                download_lesson(
                    fetch, course, lesson, save_root, existing=existing, on_page=on_page
                )
            )
        except (LoginRequired, BrowserError) as exc:
            results.append(
                FileResult("失败", course.course_name, lesson.title, str(exc), course.folder)
            )
            stop_reason = str(exc)
            results.append(
                FileResult(
                    "失败",
                    course.course_name,
                    "（其余讲次）",
                    f"已中止：{stop_reason}",
                    course.folder,
                )
            )
            break
        on_progress(index, total, f"已处理 {index}/{total}")
    return results


def download_lesson(
    fetch,
    course: CourseItem,
    lesson: LessonItem,
    save_root: Path,
    existing: library.Library | None = None,
    on_page=None,
) -> list[FileResult]:
    title = safe_component(lesson.title, lesson.courseware_id or "未命名")
    folder = course.folder or safe_component(course.course_name, course.classroom_id)
    if existing is not None:
        found = existing.matched_ok(course, lesson)
        if found is not None:
            return [
                FileResult("已有跳过", course.course_name, found.relative, "目录里已有", found.folder)
            ]
    try:
        review = fetch(
            api_path(
                "/api/v3/classroom-report/student/review",
                lesson_id=lesson.courseware_id,
                front_time=_now_ms(),
            ),
            course.classroom_id,
            course.university_id,
        )
    except LoginRequired:
        raise
    except ApiError as exc:
        return [FileResult("失败", course.course_name, title, str(exc), folder)]
    pids = presentation_ids(review)
    if not pids:
        return [FileResult("无幻灯片", course.course_name, title, "", folder)]
    dest_dir = save_root / folder / library.SUB_FOLDER
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return [FileResult("失败", course.course_name, title, "无法创建目录", folder)]
    results: list[FileResult] = []
    multiple = len(pids) > 1
    for index, pid in enumerate(pids, start=1):
        filename = f"{title}-{index}.pdf" if multiple else f"{title}.pdf"
        dest = dest_dir / filename
        if looks_complete(dest):
            pages_now = library.page_count(dest)
            if pages_now is None or pages_now >= library._MIN_PLAUSIBLE_PAGES:
                results.append(FileResult("已有跳过", course.course_name, filename, "", folder))
                continue
        try:
            ppt = fetch(
                api_path(
                    "/api/v3/classroom-report/student/ppt",
                    lesson_id=lesson.courseware_id,
                    presentationId=pid,
                    front_time=_now_ms(),
                ),
                course.classroom_id,
                course.university_id,
            )
        except LoginRequired:
            raise
        except ApiError as exc:
            results.append(FileResult("失败", course.course_name, filename, str(exc), folder))
            continue
        slides = slide_pages(ppt)
        if not slides:
            results.append(FileResult("无幻灯片", course.course_name, filename, "", folder))
            continue
        if existing is not None:
            # 名字不完全一样，但页数对得上 → 按内容认定是同一讲，不重复下载。
            # 有歧义时 content_match 返回 None，宁可重下一次也不漏内容。
            twin = existing.content_match(course, lesson, len(slides))
            if twin is not None:
                results.append(
                    FileResult(
                        "已有跳过",
                        course.course_name,
                        twin.relative,
                        f"内容一致（{len(slides)} 页）",
                        twin.folder,
                    )
                )
                continue
        if on_page is not None:
            def page_progress(done: int, total: int, _title: str = title) -> None:
                on_page(_title, done, total)

            progress = page_progress
        else:
            progress = None
        pages, missing = download_pages(slides, on_page=progress)
        if not pages:
            detail = "全部页面下载失败"
            if missing:
                detail += "（缺页 " + _page_list(missing) + "）"
            results.append(FileResult("失败", course.course_name, filename, detail, folder))
            continue
        try:
            write_pdf(pages, dest)
            library.record_download(dest, course.course_name, lesson.title, len(pages))
        except PdfError as exc:
            results.append(
                FileResult("失败", course.course_name, filename, str(exc), folder)
            )
            continue
        detail = f"{len(pages)} 页"
        if missing:
            detail += "，缺页 " + _page_list(missing)
        results.append(FileResult("已下载", course.course_name, filename, detail, folder))
    return results


def download_pages(slides: list[dict], on_page=None) -> tuple[list[Page], list[int]]:
    """并发拉页面，逐页失败隔离。第一轮快的先拉，缺的页再补一轮。"""
    total = len(slides)
    saved: list[tuple[int, Page] | None] = [None] * total
    numbers = [_page_number(slide, index + 1) for index, slide in enumerate(slides)]
    done = 0
    lock = threading.Lock()

    def one(index: int, attempts: int):
        number = numbers[index]
        try:
            return index, number, _fetch_cover(str(slides[index].get("cover") or ""), attempts)
        except Exception:
            # 页面下载/解码可能抛任何东西（OSError、解码器异常、内存错误）。
            # 一页出问题只丢这一页，绝不牵连整讲。
            return index, number, None

    def run(batch: list[int], attempts: int) -> None:
        nonlocal done
        if not batch:
            return
        workers = min(_PAGE_WORKERS, len(batch)) or 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {index: pool.submit(one, index, attempts) for index in batch}
            for _index, future in futures.items():
                try:
                    got_index, number, data = future.result()
                except Exception:
                    continue
                with lock:
                    done += 1
                    at = min(done, total)
                    if on_page is not None:
                        on_page(at, total)
                if data is None:
                    continue
                try:
                    saved[got_index] = (number, page_from_bytes(data))
                except PdfError:
                    saved[got_index] = None

    run(list(range(total)), _ATTEMPTS)
    missing_index = [index for index in range(total) if saved[index] is None]
    if missing_index:
        run(missing_index, _ATTEMPTS + 1)

    ordered = [item for item in saved if item is not None]
    missing = [numbers[index] for index in range(total) if saved[index] is None]
    return [page for _number, page in ordered], missing


def format_report(results: list[FileResult]) -> tuple[str, str]:
    groups = ("已下载", "已有跳过", "无幻灯片", "失败")
    lines: list[str] = []
    counts: list[str] = []
    for name in groups:
        items = [item for item in results if item.status == name]
        counts.append(f"{name} {len(items)}")
        lines.append(name)
        if not items:
            lines.append("（无）")
        else:
            for item in items:
                place = item.folder or item.course
                extra = f"（{item.detail}）" if item.detail else ""
                lines.append(f"- {place} / {item.filename}{extra}")
        lines.append("")
    return "，".join(counts), "\n".join(lines).rstrip() + "\n"


def _fetch_cover(url: str, attempts: int = _ATTEMPTS) -> bytes:
    """下载一页封面。失败重试，间隔递增；超过上限按失败处理。"""
    if not url.startswith(("http://", "https://")):
        raise OSError("没有页面地址")
    last: Exception | None = None
    for attempt in range(max(1, attempts)):
        try:
            return _fetch_once(url)
        except OSError as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.0 + attempt)
    raise OSError("页面下载失败") from last


def _fetch_once(url: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": _USER_AGENT},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            data = bytearray()
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                data += chunk
                if len(data) > _MAX_IMAGE:
                    raise OSError("页面过大")
    except OSError:
        raise OSError("页面下载失败") from None
    if len(data) < _MIN_IMAGE:
        raise OSError("页面过小")
    return bytes(data)


def _page_number(slide: dict, position: int) -> int:
    try:
        return int(slide.get("index"))
    except (TypeError, ValueError):
        return position


def _page_list(numbers: list[int]) -> str:
    ordered: list[str] = []
    for number in numbers:
        text = str(number)
        if text not in ordered:
            ordered.append(text)
    return "、".join(ordered)


def _now_ms() -> int:
    return int(time.time() * 1000)
