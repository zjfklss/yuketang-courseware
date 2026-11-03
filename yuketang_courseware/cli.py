"""命令行批量下载：列清单、按课筛、按学期筛、全量下载。

登录仍然只发生在程序自己打开的浏览器窗口里（独立配置目录），命令行不读日常
浏览器的 Cookie。没有图形界面时也能用。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from yuketang_courseware import __version__, library
from yuketang_courseware.browser import CoursewareBrowser, timing_summary
from yuketang_courseware.catalog import (
    SOURCE_LEAF,
    CourseItem,
    LessonItem,
    assign_folders,
    fetch_courses,
    load_many,
)
from yuketang_courseware.errors import (
    BrowserError,
    CoursewareError,
    LoginRequired,
)
from yuketang_courseware.names import (
    CourseUrl,
    parse_course_url,
    redact,
    resolve_directory,
    term_sort_key,
)
from yuketang_courseware.slides import FileResult, download_lesson, format_report

EPILOG = """\
网址从你自己浏览器的地址栏复制（每个人、每个学校的入口都不一样）：
  yuketang-courseware --url <粘贴的网址> --list
      打开登录窗口，列出现在能拿到的讲次，不下载。
  yuketang-courseware --url ... --all --out ~/课件
      上一行里全部的讲次都下载到 ~/课件/课程名/。
  yuketang-courseware --url ... --course 自动控制 --all
      只下载课程名含「自动控制」的课。
  yuketang-courseware --url ... --all-terms --all
      全部学期一起下（包括往年的课）。

只下载本人已加入的课。课件仅供本人学习，不要再传播。
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="yuketang-courseware",
        description="批量下载本人雨课堂课堂幻灯片并合成 PDF。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG,
    )
    parser.add_argument(
        "--url",
        default=None,
        help="雨课堂网址：在浏览器里随便点开一门课，从地址栏整条复制。"
        "学校 id、课堂 id 只从这个网址里读",
    )
    parser.add_argument(
        "--out",
        default=".",
        help="保存目录，默认当前目录；结构是 保存目录/课程名/课件/讲次.pdf",
    )
    parser.add_argument(
        "--course",
        action="append",
        default=[],
        metavar="关键词",
        help="只处理课程名或班级名含这个词的课，可重复",
    )
    parser.add_argument("--term", default=None, metavar="学期", help="只处理这个学期号")
    parser.add_argument(
        "--all-terms", action="store_true", help="处理全部学期；默认只处理最新学期"
    )
    parser.add_argument(
        "--all", action="store_true", help="真正开始下载；不加时只列清单"
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="只列清单，不下载（不加 --all 时本来就是这个行为）",
    )
    parser.add_argument(
        "--source",
        choices=("both", "activity", "leaf"),
        default="both",
        help="课堂活动 / 课件区 / 两者都要，默认两者",
    )
    parser.add_argument(
        "--jobs", type=int, default=1, help="同时下载几讲，默认 1；不要调太大"
    )
    parser.add_argument(
        "--delay", type=float, default=0.5, help="相邻两门课之间停几秒，默认 0.5"
    )
    parser.add_argument(
        "--tidy",
        action="store_true",
        help="把目录里已有的 PDF 归到「课程/讲次.pdf」，只搬不删、不覆盖",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印整理计划，不动文件、不下载",
    )
    parser.add_argument(
        "--on-conflict",
        choices=("keep-both", "skip"),
        default="keep-both",
        help="整理时目标位置已有同名文件、内容又不同怎么办："
        "keep-both 另存为「标题（另一版本 日期）.pdf」，两份都留（默认）；"
        "skip 只报告不动",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="不再打开浏览器，直接用已经在跑的本工具登录窗口",
    )
    parser.add_argument(
        "--wait", type=float, default=300, help="最多等登录多少秒，默认 300"
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        help="检查运行环境（Python 版本、图形界面依赖、浏览器、目录权限）后退出",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except LoginRequired as exc:
        _error(str(exc))
        return 1
    except (BrowserError, CoursewareError) as exc:
        _error(str(exc))
        return 1
    except KeyboardInterrupt:
        print("\n已停止", file=sys.stderr)
        return 130


def _run(args) -> int:
    if getattr(args, "doctor", False):
        from yuketang_courseware import preflight

        return 0 if preflight.ok(preflight.run()) else 1
    if not (args.url or "").strip():
        _error(
            "请用 --url 指定雨课堂网址：在浏览器里打开雨课堂，随便点开一门课，"
            "把地址栏整条网址复制过来。"
        )
        return 2
    try:
        course_url = parse_course_url(args.url)
    except ValueError as exc:
        _error(str(exc))
        return 2
    if args.jobs < 1:
        _error("--jobs 至少是 1")
        return 2
    try:
        # 相对路径按当前目录算（有人写「课件」，有人写绝对路径，两种都要认）
        save_root = resolve_directory(args.out)
        save_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        _error("无法使用这个保存目录")
        return 2

    browser = CoursewareBrowser()
    if not args.no_open:
        browser.open_url(course_url.url)
        print("已打开登录窗口。没登录的话，在这个窗口里扫码或用账号登录。", flush=True)

    def fetch(
        path: str,
        classroom_id: str | None,
        university_id: str | None,
        timeout: float = 60,
    ) -> dict:
        return browser.fetch_json(
            course_url.origin,
            course_url.host,
            path,
            classroom_id,
            university_id,
            timeout=timeout,
        )

    courses = _wait_for_courses(fetch, course_url, args.wait, args.no_open)
    if courses is None:
        return 1
    print(f"已读到 {len(courses)} 门课。", flush=True)

    selected = _select_courses(courses, course_url, args)
    if not selected:
        print("没有符合筛选条件的课程。")
        return 0
    assign_folders(selected)

    def on_loaded(course, done: int, total: int) -> None:
        count = len([item for item in course.lessons if item.selectable])
        print(f"[{done}/{total}] {_course_title(course)}：{count} 讲", flush=True)
        if course.error:
            print(f"    注意：{course.error}", flush=True)

    began = time.time()
    load_many(fetch, selected, workers=3, on_done=on_loaded)
    elapsed = time.time() - began
    note = timing_summary()
    print(f"读讲次用了 {elapsed:.1f} 秒。" + (f" {note}" if note else ""), flush=True)

    print()
    print(_describe(selected, args))
    print()

    kept = library.Library(save_root).bind(selected)
    matched = sum(
        1
        for course, lesson in _collect_jobs(selected, args)
        if kept.matched(course, lesson)
    )
    if kept.files:
        broken = kept.suspect(selected)
        broken_text = f"，{len(broken)} 个疑似不完整（会重新下）" if broken else ""
        tail_text = "（文件太多，只扫了前 5000 个，其余按「没有」处理）" if kept.stats.get(
            "truncated"
        ) else ""
        print(
            f"保存目录里已有 {len(kept.files)} 个 PDF，"
            f"其中 {matched} 个对得上本次讲次（会跳过）{broken_text}。{tail_text}"
        )
        print()

    if args.tidy or args.dry_run:
        plan = library.plan_tidy(
            save_root, selected, kept.files, on_conflict=args.on_conflict
        )
        print("整理计划：" + library.summarize(plan))
        for move in plan.moves[:200]:
            note = f"（{move.reason}）" if move.reason else ""
            print(
                f"  {move.source.relative}  ->  "
                f"{move.target.relative_to(save_root).as_posix()}{note}"
            )
        if len(plan.moves) > 200:
            print(f"  ……还有 {len(plan.moves) - 200} 个")
        for clash in plan.conflicts[:20]:
            note = clash.note()
            extra = f"（{note}）" if note else ""
            print(
                f"  未动，内容冲突：{clash.source.relative}{extra}  vs  "
                f"{clash.target.relative_to(save_root).as_posix()}"
            )
        for first, second in plan.duplicates[:20]:
            print(f"  内容重复（未动，可自行删除）：{first.relative}  与  {second.relative}")
        for item in plan.unmatched[:10]:
            print(f"  没认出来（未动）：{item.relative}")
        if len(plan.unmatched) > 10:
            print(f"  ……还有 {len(plan.unmatched) - 10} 个没认出来")
        print()
        if args.dry_run:
            return 0
        if plan.moves:
            done, failed = library.apply_tidy(plan)
            print(f"已整理 {len(done)} 个文件" + (f"，失败 {len(failed)} 个" if failed else ""))
            for found, reason in failed:
                print(f"  未动：{found.relative}（{reason}）")
            conflicts = [item for item in done if "另一版本" in item.target.name]
            if conflicts:
                print(f"  其中 {len(conflicts)} 个同名文件内容不同，已另存为「（另一版本 日期）」")
            print()
            kept = library.Library(save_root).bind(selected)

    jobs = _collect_jobs(selected, args)
    if not args.all or args.list:
        print(f"只列清单，没有下载。共 {len(jobs)} 个可下载讲次；加 --all 开始下载。")
        return 0
    if not jobs:
        print("没有可下载的讲次。")
        return 0

    results = _download(fetch, jobs, save_root, args, kept)
    summary, detail = format_report(results)
    print("已保存到 " + str(save_root))
    print()
    print(detail, end="")
    print(summary)
    print()
    folders = sorted({item.folder for item in results if item.folder})
    print("下载之后的目录：")
    print(library.render_tree(save_root, folders))
    print()
    print("登录窗口可以关掉。课件仅供本人学习，不要再传播。")
    return 0 if not any(item.status == "失败" for item in results) else 1


def _wait_for_courses(
    fetch, course_url: CourseUrl, timeout: float, no_open: bool
) -> list[CourseItem] | None:
    """轮询课程列表，直到登录成功或超时。"""
    deadline = time.time() + max(1.0, timeout)
    told_login = False
    told_browser = False
    while True:
        try:
            courses = fetch_courses(
                lambda path, classroom_id, university_id: fetch(
                    path, classroom_id, university_id, timeout=15
                ),
                course_url.university_id,
                course_url.classroom_id,
            )
        except BrowserError:
            courses = []
            if no_open and not told_browser:
                _error(
                    "浏览器里的雨课堂页面打不开或没有响应。"
                    "请去掉 --no-open 让程序自己打开窗口，或在已有窗口里打开雨课堂。"
                )
                return None
            told_browser = True
        except LoginRequired:
            courses = []
        if courses:
            return courses
        if time.time() >= deadline:
            _error("等待登录超时。请在打开的浏览器里登录后重试。")
            return None
        if not told_login:
            print("等待登录…登录完成后自动继续。", flush=True)
            told_login = True
        time.sleep(2)


def _select_courses(
    courses: list[CourseItem], course_url: CourseUrl, args
) -> list[CourseItem]:
    if args.all_terms:
        chosen = list(courses)
    elif args.term:
        chosen = [course for course in courses if (course.term or "未知") == args.term]
        if not chosen:
            terms = sorted({course.term or "未知" for course in courses}, key=term_sort_key)
            _error(f"没有这个学期：{args.term}。有的学期：" + "、".join(terms))
    else:
        latest = sorted({course.term or "未知" for course in courses}, key=term_sort_key)[0]
        chosen = [course for course in courses if (course.term or "未知") == latest]
    keywords = [word.strip().lower() for word in args.course if word.strip()]
    if keywords:
        chosen = [
            course
            for course in chosen
            if any(
                word in f"{course.course_name} {course.class_name}".lower()
                for word in keywords
            )
        ]
    if course_url.classroom_id:
        chosen.sort(key=lambda course: 0 if course.classroom_id == course_url.classroom_id else 1)
    return chosen


def _collect_jobs(courses: list[CourseItem], args) -> list[tuple[CourseItem, LessonItem]]:
    jobs: list[tuple[CourseItem, LessonItem]] = []
    for course in courses:
        for lesson in course.lessons:
            if not lesson.selectable:
                continue
            if args.source == "activity" and lesson.source == SOURCE_LEAF:
                continue
            if args.source == "leaf" and lesson.source != SOURCE_LEAF:
                continue
            jobs.append((course, lesson))
    return jobs


def _download(fetch, jobs, save_root: Path, args, existing=None) -> list[FileResult]:
    results: list[FileResult] = []
    stop = threading.Event()
    total = len(jobs)
    lock = threading.Lock()
    finished = [0]

    def page_note(title: str, done: int, total: int) -> None:
        if done and (done % 10 == 0 or done == total):
            print(f"       {title}：第 {done}/{total} 页", flush=True)

    def run_one(course: CourseItem, lesson: LessonItem) -> list[FileResult]:
        with lock:
            finished[0] += 1
            index = finished[0]
        print(f"[{index}/{total}] {_course_title(course)} / {lesson.title}", flush=True)
        try:
            found = download_lesson(
                fetch, course, lesson, save_root, existing=existing, on_page=page_note
            )
        except LoginRequired:
            raise
        except Exception as exc:  # 单讲失败不影响其他讲
            found = [
                FileResult("失败", course.course_name, lesson.title, redact(str(exc)), course.folder)
            ]
        for item in found:
            extra = f"（{item.detail}）" if item.detail else ""
            print(f"    {item.status}：{item.filename}{extra}", flush=True)
        if args.delay and args.jobs <= 1:
            time.sleep(max(0.0, args.delay))
        return found

    workers = max(1, min(args.jobs, total))
    if workers == 1:
        try:
            for course, lesson in jobs:
                results.extend(run_one(course, lesson))
        except LoginRequired:
            raise
        return results
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(run_one, course, lesson) for course, lesson in jobs]
        try:
            for future in futures:
                results.extend(future.result())
        except LoginRequired:
            stop.set()
            raise
    return results


def _describe(courses: list[CourseItem], args) -> str:
    lines: list[str] = []
    for course in courses:
        selectable = [lesson for lesson in course.lessons if lesson.selectable]
        lines.append(f"{_course_title(course)}  〔学期 {course.term or '未知'}〕")
        if course.error:
            lines.append(f"  ! {course.error}")
        elif course.no_activity:
            lines.append("  无课件")
        elif not course.lessons:
            lines.append("  无课堂幻灯片")
        for lesson in course.lessons:
            if lesson.selectable:
                tag = "（课件）" if lesson.source == SOURCE_LEAF else ""
                lines.append(f"  ✓ {lesson.title}{tag}")
            else:
                lines.append(f"  - {lesson.title}（{lesson.note}）")
        lines.append(f"  可下载 {len(selectable)} 讲")
        lines.append("")
    return "\n".join(lines).rstrip()


def _course_title(course: CourseItem) -> str:
    if course.class_name and course.class_name != course.course_name:
        return f"{course.course_name}（{course.class_name}）"
    return course.course_name


def _error(message: str) -> None:
    print(redact(message), file=sys.stderr, flush=True)
