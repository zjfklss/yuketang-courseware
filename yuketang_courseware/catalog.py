"""课程列表和课件清单。学期、学校 id 都来自接口或网址，不写死。

两条来源都收：
1. 课堂活动 `/v2/api/web/logs/learn/{classroom_id}`，type 14 / 3 带 courseware_id；
2. 课件区（线上学习）`/mooc-api/.../course/chapter`，只留 leaf_type 8，再按
   `leaf_level_info` 换成 courseware_id。
很多课的幻灯片只在课件区里，不出现在课堂活动里，所以两条都要。
"""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import quote

from yuketang_courseware.errors import ApiError, BrowserError, LoginRequired
from yuketang_courseware.names import api_path, safe_component

Fetch = Callable[[str, str | None, str | None], dict]

SOURCE_ACTIVITY = "课堂"
SOURCE_LEAF = "课件"

# 课件区的 leaf → courseware_id 是固定的，重复跑同一个课没必要每次都问一遍。
_CACHE_TTL = 7 * 24 * 3600
# 同一个 Edge 调试口上叠太多 leaf 请求会把页面主线程堵死，看起来像「整理讲次」卡死。
_LEAF_WORKERS = 1
_CACHE_LOCK = threading.Lock()


def _cache_path() -> Path:
    from yuketang_courseware import platforms

    return platforms.cache_root() / "leaf-map.json"


def _read_cache() -> dict[str, list]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(data: dict[str, list]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        part = path.with_suffix(".part")
        part.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(part, path)
    except OSError:
        pass


def _cache_get(cache: dict, key: str) -> str | None:
    entry = cache.get(key)
    if not isinstance(entry, list) or len(entry) != 2:
        return None
    courseware, saved = entry
    try:
        if time.time() - float(saved) > _CACHE_TTL:
            return None
    except (TypeError, ValueError):
        return None
    return str(courseware) if courseware else None


@dataclass
class LessonItem:
    type: int
    title: str
    courseware_id: str
    selectable: bool
    note: str = ""
    source: str = SOURCE_ACTIVITY


@dataclass
class CourseItem:
    classroom_id: str
    term: str
    course_name: str
    class_name: str
    university_id: str | None
    lessons: list[LessonItem] = field(default_factory=list)
    no_activity: bool = False
    loaded: bool = False
    error: str = ""
    folder: str = ""


def fetch_courses(
    fetch: Fetch,
    university_id: str | None,
    classroom_id: str | None,
) -> list[CourseItem]:
    """identity=2 是学生身份的「我听的课」。"""
    payload = fetch("/v2/api/web/courses/list?identity=2", classroom_id, university_id)
    return parse_courses(payload, university_id)


def fetch_activity_lessons(fetch: Fetch, course: CourseItem) -> tuple[list[LessonItem], bool]:
    """按 has_more 翻页。返回（可显示的讲次，是否没有任何活动）。"""
    activities: list[dict] = []
    seen: set[tuple] = set()
    classroom = quote(course.classroom_id, safe="")
    for page in range(50):
        path = api_path(
            f"/v2/api/web/logs/learn/{classroom}",
            actype=-1,
            page=page,
            offset=100,
            sort=-1,
        )
        payload = fetch(path, course.classroom_id, course.university_id)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            break
        batch = data.get("activities") or []
        if not isinstance(batch, list):
            break
        fresh = 0
        for item in batch:
            if not isinstance(item, dict):
                continue
            key = (item.get("id"), item.get("type"), item.get("courseware_id"))
            if key in seen:
                continue
            seen.add(key)
            activities.append(item)
            fresh += 1
        if not data.get("has_more") or fresh == 0:
            break
    return lessons_from_activities(activities), len(activities) == 0


def fetch_leaf_lessons(
    fetch: Fetch,
    course: CourseItem,
    workers: int = _LEAF_WORKERS,
    known_titles: set[str] | None = None,
) -> list[LessonItem]:
    """课件区：课程详情拿 course_sign 和 uv_id，chapter 拿 leaf，再换 courseware_id。

    换 courseware_id 是逐 leaf 一次请求，量最大，所以并发取；结果按 leaf 记在
    本机缓存里（7 天），同一个课再跑就不重复问了。

    known_titles 是该课课堂活动里已经有的讲次（规范化后的标题）。这些 leaf 反正
    会在合并时被课堂活动挤掉，干脆不去换 courseware_id——一个 leaf 一个请求，
    一门课省下十几个往返，登录后的第一屏能快好几秒。
    """
    detail = fetch(
        f"/v2/api/web/classrooms/{quote(course.classroom_id, safe='')}?role=5",
        course.classroom_id,
        course.university_id,
    )
    data = detail.get("data") if isinstance(detail, dict) else None
    if not isinstance(data, dict):
        return []
    sign = str(data.get("course_sign") or "").strip()
    uv_id = data.get("uv_id")
    uv_id = str(uv_id).strip() if uv_id not in (None, "") else (course.university_id or "")
    if not sign:
        return []
    chapter = fetch(
        api_path(
            "/mooc-api/v1/lms/learn/course/chapter",
            cid=course.classroom_id,
            sign=sign,
            term="latest",
            uv_id=uv_id or None,
            classroom_id=course.classroom_id,
        ),
        course.classroom_id,
        course.university_id,
    )
    chapter_data = chapter.get("data") if isinstance(chapter, dict) else None
    if not isinstance(chapter_data, dict):
        return []

    found: list[tuple[str, str]] = []
    for section in _as_list(chapter_data.get("course_chapter")):
        if not isinstance(section, dict):
            continue
        for leaf in _as_list(section.get("section_leaf_list")):
            if not isinstance(leaf, dict) or not _is_slide_leaf(leaf):
                continue
            leaf_id = leaf.get("id")
            if leaf_id in (None, ""):
                continue
            title = str(leaf.get("name") or "未命名")
            if known_titles and _title_key(title) in known_titles:
                continue
            found.append((str(leaf_id), title))
    if not found:
        return []

    with _CACHE_LOCK:
        cache = _read_cache()
    resolved: dict[str, tuple[str, str]] = {}
    pending: list[tuple[str, str]] = []
    for leaf_id, _title in found:
        hit = _cache_get(cache, f"{course.classroom_id}|{leaf_id}")
        if hit:
            resolved[leaf_id] = (hit, "")
        else:
            pending.append((leaf_id, ""))
    if pending:

        def work(item: tuple[str, str]):
            leaf_id, _unused = item
            return leaf_id, _resolve_leaf(fetch, course, leaf_id, uv_id)

        workers = max(1, min(workers, len(pending)))
        fresh: dict[str, list] = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for leaf_id, (courseware_id, problem) in pool.map(work, pending):
                resolved[leaf_id] = (courseware_id, problem)
                if courseware_id:
                    fresh[f"{course.classroom_id}|{leaf_id}"] = [
                        courseware_id,
                        time.time(),
                    ]
        if fresh:
            with _CACHE_LOCK:
                cache = _read_cache()
                cache.update(fresh)
                _write_cache(cache)

    lessons: list[LessonItem] = []
    for leaf_id, title in found:
        courseware_id, problem = resolved.get(leaf_id, ("", ""))
        if courseware_id:
            lessons.append(LessonItem(8, title, courseware_id, True, "", SOURCE_LEAF))
        else:
            lessons.append(
                LessonItem(8, title, "", False, problem or "没有课件", SOURCE_LEAF)
            )
    return lessons


def _is_slide_leaf(leaf: dict) -> bool:
    """leaf_type 8 是线上学习/课件；带视频、讨论之类的不要。"""
    try:
        kind = int(leaf.get("leaf_type"))
    except (TypeError, ValueError):
        return False
    if kind != 8:
        return False
    if leaf.get("is_show") is False:
        return False
    for key in ("video", "audio", "discuss", "exam", "homework"):
        if leaf.get(key):
            return False
    return True


def _resolve_leaf(
    fetch: Fetch, course: CourseItem, leaf_id: str, uv_id: str | None
) -> tuple[str, str]:
    try:
        payload = fetch(
            api_path(
                "/edu_admin/leaf_level_info/",
                leaf_level_id=leaf_id,
                no_loading="false",
                term="latest",
                uv_id=uv_id or None,
                classroom_id=course.classroom_id,
            ),
            course.classroom_id,
            course.university_id,
        )
    except LoginRequired:
        raise
    except (ApiError, BrowserError) as exc:
        return "", str(exc)
    data = payload.get("data") if isinstance(payload, dict) else None
    source = data if isinstance(data, dict) else payload
    raw = source.get("courseware_id") if isinstance(source, dict) else None
    if raw in (None, "", "0", 0):
        return "", "没有课件"
    return str(raw), ""


def lessons_from_activities(activities: list[dict]) -> list[LessonItem]:
    lessons: list[LessonItem] = []
    for item in activities:
        try:
            kind = int(item.get("type"))
        except (TypeError, ValueError):
            continue
        title = str(item.get("title") or "未命名")
        raw_id = item.get("courseware_id")
        courseware_id = "" if raw_id is None else str(raw_id)
        if kind in (14, 3):
            if not courseware_id:
                continue
            lessons.append(LessonItem(kind, title, courseware_id, True, "", SOURCE_ACTIVITY))
        elif kind == 2:
            lessons.append(
                LessonItem(kind, title, courseware_id, False, "网页预习，无幻灯片", SOURCE_ACTIVITY)
            )
    return lessons


def merge_lessons(
    activities: list[LessonItem], leaves: list[LessonItem]
) -> list[LessonItem]:
    """课堂活动优先。同一 courseware_id 或同一标题只留一条，避免同名文件互相覆盖。"""
    merged: list[LessonItem] = []
    by_courseware: set[str] = set()
    by_title: set[str] = set()
    for lesson in list(activities) + list(leaves):
        key = _title_key(lesson.title)
        if lesson.courseware_id and lesson.courseware_id in by_courseware:
            continue
        if key in by_title:
            continue
        merged.append(lesson)
        if lesson.courseware_id:
            by_courseware.add(lesson.courseware_id)
        by_title.add(key)
    return merged


def _title_key(title: str) -> str:
    """比对标题用的键。和磁盘文件名的归一化同一套规则（标点、全半角、中文数字）。

    平台两边的标题常常只差标点：课堂活动写「第5讲 频域分析」，课件区写
    「第5讲-频域分析-5」。用弱键比不出来，每个 leaf 都要多问一次接口。
    """
    from yuketang_courseware.library import normalize

    return normalize(title)


def assign_folders(courses: list[CourseItem]) -> None:
    """每门课一个子目录，用课程名。同一学期课程名重复时加上班级名。"""
    groups: dict[str, list[CourseItem]] = {}
    for course in courses:
        groups.setdefault(course.course_name, []).append(course)
    for course in courses:
        base = safe_component(course.course_name, course.classroom_id)
        if len(groups.get(course.course_name, [])) > 1:
            extra = safe_component(course.class_name, course.classroom_id)
            base = safe_component(f"{base}（{extra}）", course.classroom_id)
        course.folder = base
    seen: set[str] = set()
    for course in courses:
        folder = course.folder
        if folder in seen:
            folder = safe_component(f"{folder}-{course.classroom_id}", course.classroom_id)
        if folder in seen:
            folder = safe_component(course.classroom_id, "course")
        course.folder = folder
        seen.add(folder)


def load_many(
    fetch: Fetch,
    courses: list[CourseItem],
    workers: int = 1,
    on_done: Callable[[CourseItem, int, int], None] | None = None,
    stop: object | None = None,
    include_leaves: bool = True,
) -> None:
    """按课读讲次。默认串行：同一调试口并发过高时，第一次整理会假死。

    两阶段：先课堂活动（快，立刻 on_done 刷新界面），再补课件区 leaf。
    """
    if not courses:
        return
    total = len(courses)
    counter = {"done": 0}
    lock = threading.Lock()

    def work(course: CourseItem) -> None:
        if stop is not None and getattr(stop, "is_set", lambda: False)():
            return
        # 先课堂活动：几秒内就能在界面上看到这门课
        load_lessons(fetch, course, include_leaves=False)
        with lock:
            counter["done"] += 1
            index = counter["done"]
        if on_done is not None:
            on_done(course, index, total)
        if not include_leaves:
            return
        if stop is not None and getattr(stop, "is_set", lambda: False)():
            return
        # 再补课件区；同一 index 再回调，界面替换为完整讲次
        enrich_leaf_lessons(fetch, course)
        if on_done is not None:
            on_done(course, index, total)

    workers = max(1, min(workers, total))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(work, courses))


def load_lessons(
    fetch: Fetch,
    course: CourseItem,
    delay: float = 0.0,
    include_leaves: bool = True,
) -> None:
    """先取课堂活动，再取课件区。两条都失败才记为课程错误。"""
    problems: list[str] = []
    activities: list[LessonItem] = []
    leaves: list[LessonItem] = []
    no_activity = False
    try:
        activities, no_activity = fetch_activity_lessons(fetch, course)
    except LoginRequired:
        raise
    except (ApiError, BrowserError) as exc:
        problems.append(str(exc))
    if delay:
        _sleep(delay)
    if include_leaves:
        known = {_title_key(item.title) for item in activities}
        try:
            leaves = fetch_leaf_lessons(fetch, course, known_titles=known)
        except LoginRequired:
            raise
        except (ApiError, BrowserError) as exc:
            problems.append(str(exc))
    course.lessons = merge_lessons(activities, leaves)
    course.no_activity = no_activity and not leaves
    course.loaded = True
    course.error = "" if course.lessons or not problems else "；".join(problems)


def enrich_leaf_lessons(fetch: Fetch, course: CourseItem) -> None:
    """在已有课堂活动讲次基础上补课件区。单叶失败就跳过，不拖死整门课。"""
    activities = [item for item in course.lessons if item.source == SOURCE_ACTIVITY]
    known = {_title_key(item.title) for item in activities}
    try:
        leaves = fetch_leaf_lessons(fetch, course, known_titles=known)
    except LoginRequired:
        raise
    except (ApiError, BrowserError) as exc:
        if not course.lessons:
            course.error = str(exc)
        return
    course.lessons = merge_lessons(activities, leaves)
    if leaves:
        course.no_activity = False
        if course.error and not activities:
            course.error = ""


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


def _as_list(value) -> list:
    """接口字段的形状不保证：可能是 list、可能是 {"list": [...]}、可能是 null。"""
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("list", "data"):
            nested = value.get(key)
            if isinstance(nested, list):
                return nested
    return []


def _extract_list(payload) -> list:
    """课程列表可能在 data 里、在 data.list 里、也可能整个返回不是 dict。"""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "courses"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def parse_courses(payload, university_fallback: str | None) -> list[CourseItem]:
    courses: list[CourseItem] = []
    seen: set[str] = set()
    for item in _extract_list(payload):
        if not isinstance(item, dict):
            continue
        classroom_id = item.get("classroom_id")
        if classroom_id in (None, ""):
            continue
        classroom = str(classroom_id)
        if classroom in seen:
            continue
        seen.add(classroom)
        nested = item.get("course") if isinstance(item.get("course"), dict) else {}
        course_name = str(nested.get("name") or item.get("name") or "未命名课程")
        class_name = str(item.get("name") or "")
        term = item.get("term")
        university = nested.get("university_id")
        if university in (None, ""):
            university = item.get("university_id")
        if university in (None, ""):
            university_id = university_fallback
        else:
            university_id = str(university)
        courses.append(
            CourseItem(
                classroom_id=classroom,
                term="" if term is None else str(term),
                course_name=course_name,
                class_name=class_name,
                university_id=university_id,
            )
        )
    return courses
