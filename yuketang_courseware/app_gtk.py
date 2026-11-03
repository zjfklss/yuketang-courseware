"""雨课堂课件窗口（Linux 后端：GTK4 / libadwaita）。

开箱即用的流程：点「登录雨课堂」→ 在程序自己打开的窗口里扫码或用账号登录 →
课程自动出现、讲次默认全勾 → 点「开始下载」。全程不用看文档。

登录只发生在独立配置目录里，不读日常 Edge / Chrome 的 Cookie；学校 id、课堂 id
只从网址的查询参数里读。
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk

try:  # libadwaita 是首选；没有它时只给命令行提示，不硬崩
    gi.require_version("Adw", "1")
    from gi.repository import Adw

    HAVE_ADW = True
except (ImportError, ValueError):  # pragma: no cover - 取决于系统
    Adw = None
    HAVE_ADW = False

from yuketang_courseware import __version__, library, platforms, settings
from yuketang_courseware.browser import CoursewareBrowser, timing_summary
from yuketang_courseware.catalog import (
    SOURCE_LEAF,
    CourseItem,
    LessonItem,
    assign_folders,
    fetch_courses,
    load_many,
)
from yuketang_courseware.errors import LoginRequired
from yuketang_courseware.names import (
    CourseUrl,
    parse_course_url,
    redact,
    resolve_directory,
    term_sort_key,
)
from yuketang_courseware.slides import download_selected, format_report

# 不写死学校入口：不同学校的雨课堂子域名不同，网址由使用者从地址栏复制。
URL_HINT = "https://xxx.yuketang.cn/v2/web/index"
CLI_HELP = """\
yuketang-courseware --url <网址> --list        只看清单
yuketang-courseware --url <网址> --all         整批下载（最新学期）
yuketang-courseware --url <网址> --all --course 自动控制
yuketang-courseware --url <网址> --all --all-terms
man yuketang-courseware                        全部选项
"""

LOGIN_TIMEOUT = 900.0
POLL_SECONDS = 1.5


def main() -> int:
    """开窗口。没有图形界面时提示改用命令行。"""
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print(
            "没有图形界面（DISPLAY / WAYLAND_DISPLAY 都没设）。"
            "可以改用命令行：yuketang-courseware --list",
            flush=True,
        )
        return 1
    app_class = Adw.Application if HAVE_ADW else Gtk.Application
    app = app_class(application_id="local.yuketang.Courseware")
    if HAVE_ADW:
        manager = Adw.StyleManager.get_default()
        manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
    app.connect("activate", _on_activate)
    code = app.run(["yuketang-courseware"])
    return 0 if code in (None, 0) else int(code)


def _on_activate(app: Gtk.Application) -> None:
    window = app.get_active_window()
    if window is None:
        window = MainWindow(app)
    window.present()


class MainWindow(Adw.ApplicationWindow if HAVE_ADW else Gtk.ApplicationWindow):  # type: ignore[misc]
    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app, title="雨课堂课件")
        self.set_default_size(900, 800)
        self._alive = True
        self._busy = False
        self._suppress_term = False
        self._suppress_select = False
        self._stop = threading.Event()
        self._fetch_timeout = 60.0
        self._browser = CoursewareBrowser()
        self._course_url: CourseUrl | None = None
        self._all_courses: list[CourseItem] = []
        self._terms: list[str] = []
        self._checks: list[tuple[CourseItem, LessonItem, Gtk.CheckButton]] = []
        self._course_rows: list[Gtk.Widget] = []
        self._course_row_by_id: dict[int, Gtk.Widget] = {}
        self._rendering = False
        self._placeholder_shown = False
        self._result_group = None
        self._pending_jobs = None
        self._library_files: list[library.FoundFile] = []
        self._scan_stats: dict = {}
        self._library: library.Library | None = None
        self._tidy_plan = None
        self._native_chooser = None
        self.connect("destroy", self._on_destroy)

        saved = settings.load()
        self._saved_url = str(saved.get("url") or "")
        self._saved_dir = str(saved.get("directory") or "")

        if not HAVE_ADW:
            self._build_fallback()
            return
        self._build()

    # ---------- 界面 ----------

    def _build_fallback(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        label = Gtk.Label(
            label=(
                "缺少 libadwaita（gir1.2-adw-1），窗口用不了。\n"
                "装一下：sudo apt install gir1.2-adw-1\n\n" + CLI_HELP
            ),
            xalign=0,
            wrap=True,
            selectable=True,
        )
        box.append(label)
        self.set_child(box)

    def _build(self) -> None:
        self._install_actions()
        self._install_css()

        self.toast_overlay = Adw.ToastOverlay()
        toolbar = _toolbar_view()
        header = Adw.HeaderBar()
        header.set_title_widget(
            Adw.WindowTitle(
                title="雨课堂课件",
                subtitle="下载本人课程的幻灯片 · 仅供本人学习",
            )
        )
        menu = Gio.Menu()
        menu.append("关闭登录窗口", "win.close-browser")
        menu.append("命令行用法", "win.cli")
        menu.append("关于", "win.about")
        button = Gtk.MenuButton()
        button.set_icon_name("open-menu-symbolic")
        button.set_menu_model(menu)
        button.set_tooltip_text("更多")
        header.pack_end(button)
        toolbar.add_top_bar(header)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.banner = _banner()
        content.append(self.banner)

        self.page = Adw.PreferencesPage()
        self.page.set_vexpand(True)
        content.append(self.page)

        self._build_login()
        self._build_pick()
        self._build_courses()
        self._build_save()
        content.append(self._build_bottom())

        toolbar.set_content(content)
        self.toast_overlay.set_child(toolbar)
        if HAVE_ADW:
            self.set_content(self.toast_overlay)
        else:
            self.set_child(self.toast_overlay)
        self._sync_term_controls()
        self._update_selection()
        self._probe_existing()

    def _install_actions(self) -> None:
        for name, handler in (
            ("close-browser", self._on_close_browser),
            ("cli", self._show_cli),
            ("about", self._show_about),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda *_args, func=handler: func())
            self.add_action(action)

    def _install_css(self) -> None:
        css = """
        .lesson-count { font-size: 0.88em; }
        .hero-button { font-weight: 700; }
        progressbar.thin trough { min-height: 6px; }
        progressbar.thin progress { min-height: 6px; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css.encode("utf-8"))
        display = self.get_display()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _build_login(self) -> None:
        # 网址和登录是同一件事，别拆成两组——省出高度给下面的课程列表
        group = Adw.PreferencesGroup()
        group.set_title("1　登录雨课堂")
        self.login_group = group

        self.url_row = Adw.EntryRow(title="雨课堂网址")
        self.url_row.set_text(self._saved_url)
        self.url_row.set_tooltip_text("例：" + URL_HINT)
        self.url_row.connect("changed", self._on_url_changed)
        self.btn_paste = Gtk.Button(label="粘贴")
        self.btn_paste.add_css_class("flat")
        self.btn_paste.set_valign(Gtk.Align.CENTER)
        self.btn_paste.connect("clicked", self._on_paste)
        self.url_row.add_suffix(self.btn_paste)
        group.add(self.url_row)

        how = Adw.ExpanderRow(title="怎么拿到网址？")
        for text in (
            "1. 在你自己的浏览器里打开雨课堂，随便点开一门课。",
            "2. 复制地址栏整条网址（每个人、每个学校的入口都不一样）。",
            "3. 粘贴到上面，再点下面的「登录雨课堂」。",
        ):
            how.add_row(Adw.ActionRow(title=text))
        group.add(how)

        self.status_row = Adw.ActionRow(title="登录状态", subtitle=_esc("还没有登录"))
        self.status_spinner = Adw.Spinner()
        self.status_spinner.set_visible(False)
        self.status_row.add_prefix(self.status_spinner)
        self.btn_login = Gtk.Button(label="登录雨课堂")
        self.btn_login.add_css_class("suggested-action")
        self.btn_login.add_css_class("pill")
        self.btn_login.set_valign(Gtk.Align.CENTER)
        self.btn_login.connect("clicked", self._on_login)
        self.status_row.add_suffix(self.btn_login)
        self.status_row.set_activatable_widget(self.btn_login)
        group.add(self.status_row)
        self.page.add(group)

    def _build_pick(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("2　选课")
        self.pick_group = group

        self.term_combo = Adw.ComboRow(title="学期")
        self.term_combo.set_model(Gtk.StringList())
        self.term_combo.connect("notify::selected", self._on_term)
        group.add(self.term_combo)

        self.all_terms = Adw.SwitchRow(title="全部学期")
        self.all_terms.set_subtitle("把往年的课一起列出来，默认只看最新学期")
        self.all_terms.connect("notify::active", self._on_all_terms)
        group.add(self.all_terms)

        self.select_all = Adw.SwitchRow(title="全选所有课程")
        self.select_all.set_subtitle("一键勾上全部讲次")
        self.select_all.connect("notify::active", self._on_select_all)
        group.add(self.select_all)
        self.page.add(group)

    def _build_courses(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("3　课程与讲次")
        group.set_description("点课程看每一讲；勾上的就下载，已下载过的自动跳过。")
        self.course_group = group
        self.course_placeholder = Adw.ActionRow(
            title="还没有读到课程", subtitle="点上面的「登录雨课堂」"
        )
        group.add(self.course_placeholder)
        self._placeholder_shown = True
        self.page.add(group)

    def _build_save(self) -> None:
        group = Adw.PreferencesGroup()
        group.set_title("4　保存")
        group.set_description("结构是 保存目录/课程名/课件/讲次.pdf；不选目录就不下载，不替你猜。")
        self.dir_row = Adw.EntryRow(title="保存目录")
        self.dir_row.set_text(self._save_root_text())
        self.dir_row.connect("changed", self._on_dir_changed)
        self.btn_choose = Gtk.Button(label="选择")
        self.btn_choose.add_css_class("flat")
        self.btn_choose.set_valign(Gtk.Align.CENTER)
        self.btn_choose.connect("clicked", self._on_choose_dir)
        self.btn_open = Gtk.Button(label="打开")
        self.btn_open.add_css_class("flat")
        self.btn_open.set_valign(Gtk.Align.CENTER)
        self.btn_open.connect("clicked", self._on_open_dir)
        self.dir_row.add_suffix(self.btn_choose)
        self.dir_row.add_suffix(self.btn_open)
        group.add(self.dir_row)

        self.library_row = Adw.ActionRow(title="目录里已有", subtitle="还没扫描")
        self.btn_tidy = Gtk.Button(label="整理")
        self.btn_tidy.add_css_class("flat")
        self.btn_tidy.set_valign(Gtk.Align.CENTER)
        self.btn_tidy.set_tooltip_text("把目录里已有的 PDF 归到「课程/讲次.pdf」，只搬不删")
        self.btn_tidy.connect("clicked", self._on_tidy)
        self.library_row.add_suffix(self.btn_tidy)
        group.add(self.library_row)
        self.save_group = group
        self.page.add(group)

    def _build_bottom(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bar.set_margin_top(10)
        bar.set_margin_bottom(14)
        bar.set_margin_start(18)
        bar.set_margin_end(18)

        self.selection_label = Gtk.Label(label="")
        self.selection_label.set_xalign(0)
        self.selection_label.add_css_class("lesson-count")
        bar.append(self.selection_label)

        self.progress = Gtk.ProgressBar()
        self.progress.set_hexpand(True)
        self.progress.set_show_text(True)
        self.progress.set_text("")
        self.progress.set_valign(Gtk.Align.CENTER)
        self.progress.add_css_class("thin")
        bar.append(self.progress)

        self.btn_stop = Gtk.Button(label="停止")
        self.btn_stop.add_css_class("flat")
        self.btn_stop.add_css_class("pill")
        self.btn_stop.set_sensitive(False)
        self.btn_stop.connect("clicked", self._on_stop)
        bar.append(self.btn_stop)

        self.btn_download = Gtk.Button(label="开始下载")
        self.btn_download.add_css_class("suggested-action")
        self.btn_download.add_css_class("pill")
        self.btn_download.add_css_class("hero-button")
        self.btn_download.set_size_request(150, -1)
        self.btn_download.connect("clicked", self._on_download)
        bar.append(self.btn_download)
        return bar

    # ---------- 状态 ----------

    def _remember(self) -> None:
        settings.save(self.url_row.get_text().strip(), self.dir_row.get_text().strip())

    def _save_root_text(self) -> str:
        return self._saved_dir

    def _save_root(self) -> Path | None:
        """还没选目录就返回 None，不猜路径。"""
        text = (
            self.dir_row.get_text().strip()
            if hasattr(self, "dir_row")
            else self._save_root_text()
        )
        if not text:
            return None
        # 从菜单启动的窗口没有稳定的当前目录，相对路径一律按家目录算
        path = resolve_directory(text, base=Path.home())
        return path

    def _status(self, text: str, pulse: bool = False) -> None:
        def apply(text=text, pulse=pulse) -> None:
            self.status_row.set_subtitle(_esc(redact(text)))
            self.status_spinner.set_visible(pulse)
            if pulse:
                self.progress.pulse()

        self.ui(apply)

    def _toast(self, text: str) -> None:
        def apply(text=text) -> None:
            self.toast_overlay.add_toast(Adw.Toast(title=_esc(redact(text)), timeout=6))

        self.ui(apply)

    def _notify_error(self, text: str) -> None:
        def apply(text=text) -> None:
            self.banner.set_title(redact(text))
            self.banner.set_revealed(True)
            self.status_spinner.set_visible(False)
            self.progress.set_fraction(0)
            self._set_busy(False)

        self.ui(apply)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (
            self.btn_login,
            self.url_row,
            self.dir_row,
            self.btn_choose,
            self.btn_open,
            self.btn_tidy,
            self.term_combo,
            self.all_terms,
            self.select_all,
        ):
            widget.set_sensitive(not busy)
        self.btn_stop.set_sensitive(busy)
        self.btn_download.set_sensitive(not busy and self._chosen_count() > 0)
        self._sync_term_controls()

    def _chosen_count(self) -> int:
        return sum(1 for _course, _lesson, button in self._checks if button.get_active())

    def _sync_term_controls(self) -> None:
        every = self.all_terms.get_active() if hasattr(self, "all_terms") else False
        self.term_combo.set_sensitive(not every and not self._busy)
        self.all_terms.set_sensitive(not self._busy)
        self.select_all.set_sensitive(not self._busy)

    def _update_selection(self) -> None:
        if getattr(self, "_rendering", False):
            return
        total = len(self._checks)
        chosen = self._chosen_count()
        if not hasattr(self, "selection_label"):
            return
        self.selection_label.set_text(f"已选 {chosen} / {total} 讲")
        self.btn_download.set_sensitive(bool(chosen) and not self._busy)
        if hasattr(self, "select_all"):
            self._suppress_select = True
            try:
                self.select_all.set_active(total > 0 and chosen == total)
            finally:
                self._suppress_select = False

    # ---------- 登录 ----------

    def _on_paste(self, *_args) -> None:
        clipboard = self.get_clipboard()
        clipboard.read_text_async(None, self._on_paste_done)

    def _on_paste_done(self, clipboard, result) -> None:
        try:
            text = clipboard.read_text_finish(result) or ""
        except GLib.Error:
            self._notify_error("读不到剪贴板里的网址")
            return
        line = text.strip().splitlines()[0] if text.strip() else ""
        if not line:
            self._notify_error("剪贴板里没有网址")
            return
        self.url_row.set_text(line)
        self._remember()

    def _on_login(self, *_args) -> None:
        if self._busy:
            return
        text = self.url_row.get_text().strip()
        if not text:
            self.banner.set_title(
                "先在第 1 步粘贴雨课堂网址：在自己浏览器里打开雨课堂，随便点开一门课，"
                "把地址栏整条复制过来。"
            )
            self.banner.set_revealed(True)
            self.url_row.grab_focus()
            return
        self._set_busy(True)
        self._stop.clear()
        self.banner.set_revealed(False)
        self._status("正在打开浏览器…", pulse=True)
        threading.Thread(target=self._login_worker, args=(text,), daemon=True).start()

    def _login_worker(self, text: str) -> None:
        try:
            course_url = parse_course_url(text)
            self._course_url = course_url
            self._remember()
            self._browser.open_url(f"{course_url.origin}/v2/web/index")
            self._status("浏览器已打开。扫码或用账号登录，登录成功后课程会自动出现。", pulse=True)
        except Exception as exc:
            self._notify_error(str(exc) or "没能打开浏览器")
            return
        self._wait_and_load(course_url)

    def _wait_and_load(self, course_url: CourseUrl) -> None:
        started = time.time()
        deadline = started + LOGIN_TIMEOUT
        self._fetch_timeout = 15.0
        while not self._stop.is_set() and self._alive:
            try:
                courses = fetch_courses(
                    self._fetch, course_url.university_id, course_url.classroom_id
                )
            except Exception:
                courses = []
            if courses:
                self._fetch_timeout = 60.0
                self._load_all(courses, course_url)
                return
            if time.time() >= deadline:
                self._notify_error("等登录超时了。再点一次「登录雨课堂」即可。")
                return
            self._status(f"等登录中…（已等 {int(time.time() - started)} 秒）", pulse=True)
            if self._sleep(POLL_SECONDS):
                return
            if not self._course_url:
                return
        if self._alive:
            self._notify_error("已停止等待登录。")

    def _sleep(self, seconds: float) -> bool:
        """等待时也响应停止。返回 True 表示已被要求停止。"""
        end = time.time() + seconds
        while time.time() < end:
            if self._stop.is_set() or not self._alive:
                return True
            time.sleep(0.1)
        return False

    def _probe_existing(self) -> None:
        """启动时若本工具的浏览器还开着并已登录，直接把课程读出来。"""

        def run() -> None:
            if not self._browser.alive():
                return
            if self._course_url is not None:
                return
            text = (self._saved_url or "").strip()
            if not text:
                # 没记住网址时，用本工具浏览器里已打开的雨课堂页面反推
                for url in self._browser.page_urls():
                    if "yuketang.cn" in url:
                        text = url
                        break
            if not text:
                return
            try:
                course_url = parse_course_url(text)
            except ValueError:
                return
            self._course_url = course_url
            try:
                courses = fetch_courses(
                    self._fetch, course_url.university_id, course_url.classroom_id
                )
            except Exception:
                self._course_url = None
                return
            if not courses:
                self._course_url = None
                return
            self.ui(lambda url=course_url.url: self.url_row.set_text(url))
            self._load_all(courses, course_url)

        threading.Thread(target=run, daemon=True).start()

    def _load_all(self, courses: list[CourseItem], course_url: CourseUrl) -> None:
        terms = sorted({course.term or "未知" for course in courses}, key=term_sort_key)
        selected = terms[0]
        if course_url.classroom_id:
            for course in courses:
                if course.classroom_id == course_url.classroom_id and course.term:
                    selected = course.term
                    break
        self._status(f"已读到 {len(courses)} 门课，正在整理讲次…", pulse=True)

        def finish() -> None:
            self._all_courses = courses
            self._install_terms(terms, selected)
            # 登录线程里已经 busy=True；_reload 会因此直接 return，讲次永远不整理。
            self._set_busy(False)
            self._reload(term=selected, every=self.all_terms.get_active())

        self.ui(finish)

    def _on_term(self, dropdown: Adw.ComboRow, _pspec) -> None:
        if self._suppress_term or self._busy or not self._all_courses:
            return
        index = dropdown.get_selected()
        if index is None or index >= len(self._terms):
            return
        self._reload(term=self._terms[index])

    def _on_all_terms(self, switch: Adw.SwitchRow, _pspec) -> None:
        every = switch.get_active()
        self._sync_term_controls()
        if not self._all_courses or self._busy:
            return
        index = self.term_combo.get_selected()
        term = self._terms[index] if index is not None and index < len(self._terms) else ""
        self._reload(term=term, every=every)

    def _reload(self, term: str = "", every: bool | None = None) -> None:
        if self._busy:
            return
        if every is None:
            every = self.all_terms.get_active()
        self._set_busy(True)
        self._stop.clear()

        def run() -> None:
            try:
                visible = self._visible(term, every)
                self.ui(lambda: self._start_fill(visible))
                self._fill_lessons(visible)
                self.ui(lambda: self._finish_fill(visible))
            except Exception as exc:
                self._notify_error(str(exc) or "读取失败")
                return
            self.ui(lambda: self._set_busy(False))

        threading.Thread(target=run, daemon=True).start()

    def _visible(self, term: str, every: bool) -> list[CourseItem]:
        if every:
            chosen = list(self._all_courses)
        else:
            chosen = [c for c in self._all_courses if (c.term or "未知") == term]
        chosen.sort(key=lambda course: 0 if self._is_wanted(course) else 1)
        assign_folders(chosen)
        return chosen

    def _is_wanted(self, course: CourseItem) -> bool:
        return bool(
            self._course_url
            and self._course_url.classroom_id
            and course.classroom_id == self._course_url.classroom_id
        )

    def _start_fill(self, courses: list[CourseItem]) -> None:
        """清空课程区。已经读过的课直接摆上去，其余的读完一门 появля一门。"""
        self._rendering = True
        self._clear_courses()
        self._checks.clear()
        self._update_selection()
        if not courses:
            self._rendering = False
            self._show_placeholder()
            self._status("这个学期没有课程。换个学期，或打开「全部学期」。")
            return
        expand = len(courses) <= 3
        for course in courses:
            if course.loaded:
                self._add_course(course, expand)
        if not self._course_rows:
            self._status(f"正在整理 {len(courses)} 门课的讲次…", pulse=True)

    def _fill_lessons(self, courses: list[CourseItem]) -> None:
        pending = [course for course in courses if not course.loaded]
        if not pending:
            return
        expand = len(courses) <= 3
        began = time.time()
        self._fetch_timeout = 20.0
        if self._course_url is not None:
            try:
                self._browser.open_url(f"{self._course_url.origin}/v2/web/index")
            except Exception:
                pass

        def on_done(course: CourseItem, done: int, total: int) -> None:
            self._status(
                f"整理讲次 {done}/{total}：{course.course_name}"
                f"（{len([item for item in course.lessons if item.selectable])} 讲）",
                pulse=True,
            )
            self.ui(lambda course=course: self._upsert_course(course, expand))
            self.ui(lambda done=done, total=total: self.progress.set_fraction(done / max(1, total)))

        load_many(self._fetch, pending, workers=1, on_done=on_done, stop=self._stop)
        self._fill_seconds = time.time() - began

    def _upsert_course(self, course: CourseItem, expand: bool) -> None:
        if not self._alive:
            return
        row = self._course_row_by_id.pop(id(course), None)
        if row is not None:
            self.course_group.remove(row)
            if row in self._course_rows:
                self._course_rows.remove(row)
            self._checks = [item for item in self._checks if item[0] is not course]
        self._append_course(course, expand)

    def _append_course(self, course: CourseItem, expand: bool) -> None:
        if not self._alive:
            return
        if self._placeholder_shown:
            self.course_group.remove(self.course_placeholder)
            self._placeholder_shown = False
        self._add_course(course, expand)

    def _finish_fill(self, courses: list[CourseItem]) -> None:
        self._rendering = False
        if not courses:
            return
        lessons = [
            lesson for course in courses for lesson in course.lessons if lesson.selectable
        ]
        spent = getattr(self, "_fill_seconds", 0.0)
        line = f"已读到 {len(courses)} 门课、{len(lessons)} 个可下载讲次，讲次已默认勾好。"
        if spent:
            line += f"（整理讲次 {spent:.1f} 秒）"
        note = timing_summary()
        if note:
            line += f" {note}"
            print("[timing] " + note, file=sys.stderr, flush=True)
        self._status(line)
        self._update_selection()

    def _show_placeholder(self) -> None:
        if not self._placeholder_shown:
            self.course_group.add(self.course_placeholder)
            self._placeholder_shown = True

    def _fetch(self, path: str, classroom_id: str | None, university_id: str | None) -> dict:
        if self._course_url is None:
            raise LoginRequired()
        return self._browser.fetch_json(
            self._course_url.origin,
            self._course_url.host,
            path,
            classroom_id,
            university_id,
            timeout=self._fetch_timeout,
        )

    # ---------- 渲染 ----------

    def _render(self) -> None:
        """一次性重画（截图脚本、测试用）。正常流程走 _start_fill/_finish_fill。"""
        every = self.all_terms.get_active()
        courses = self._visible(self._current_term(), every) if self._all_courses else []
        self._start_fill(courses)
        self._finish_fill(courses)

    def _current_term(self) -> str:
        index = self.term_combo.get_selected()
        if index is not None and index < len(self._terms):
            return self._terms[index]
        return ""

    def _clear_courses(self) -> None:
        for row in self._course_rows:
            self.course_group.remove(row)
        self._course_rows = []
        self._course_row_by_id = {}
        if self._placeholder_shown:
            self.course_group.remove(self.course_placeholder)
            self._placeholder_shown = False

    def _add_course(self, course: CourseItem, expand: bool) -> None:
        selectable = [lesson for lesson in course.lessons if lesson.selectable]
        title = course.course_name
        if course.class_name and course.class_name != course.course_name:
            title = f"{course.course_name} · {course.class_name}"
        subtitle = f"{len(selectable)} 讲可下载"
        if self.all_terms.get_active() and course.term:
            subtitle += f" · 学期 {course.term}"
        if course.error:
            subtitle = course.error
        elif course.no_activity:
            subtitle = "无课件"
        expander = Adw.ExpanderRow(title=_esc(title), subtitle=_esc(subtitle))
        expander.set_expanded(expand)
        buttons: list[Gtk.CheckButton] = []
        guard = {"on": False, "toggle": None, "buttons": buttons}
        if selectable:
            toggle = Gtk.CheckButton()
            toggle.set_valign(Gtk.Align.CENTER)
            toggle.set_tooltip_text("全选本课")
            toggle.set_active(True)
            expander.add_suffix(toggle)
            toggle.connect(
                "toggled",
                lambda button, buttons=buttons, flag=guard: self._toggle_course(
                    button, buttons, flag
                ),
            )
            guard["toggle"] = toggle
        for lesson in course.lessons:
            row = Adw.ActionRow(title=_esc(lesson.title))
            bits = []
            if lesson.source == SOURCE_LEAF:
                bits.append("课件区")
            if lesson.note:
                bits.append(lesson.note)
            if bits:
                row.set_subtitle(_esc(" · ".join(bits)))
            if lesson.selectable:
                check = Gtk.CheckButton()
                check.set_valign(Gtk.Align.CENTER)
                check.set_active(True)
                check.connect(
                    "toggled",
                    lambda _button, flag=guard: self._sync_course(flag),
                )
                row.add_suffix(check)
                row.set_activatable_widget(check)
                buttons.append(check)
                self._checks.append((course, lesson, check))
            else:
                row.set_sensitive(False)
            expander.add_row(row)
        self.course_group.add(expander)
        self._course_rows.append(expander)
        self._course_row_by_id[id(course)] = expander

    def _toggle_course(self, toggle: Gtk.CheckButton, buttons, guard: dict) -> None:
        if guard.get("on"):
            return
        guard["on"] = True
        try:
            active = toggle.get_active()
            toggle.set_inconsistent(False)
            for button in buttons:
                button.set_active(active)
        finally:
            guard["on"] = False
        self._update_selection()

    def _sync_course(self, guard: dict) -> None:
        if guard.get("on"):
            return
        toggle = guard.get("toggle")
        if toggle is None:
            self._update_selection()
            return
        guard["on"] = True
        try:
            actives = [button.get_active() for button in guard.get("buttons", [])]
            toggle.set_inconsistent(False)
            toggle.set_active(all(actives))
            toggle.set_inconsistent(any(actives) and not all(actives))
        finally:
            guard["on"] = False
        self._update_selection()

    def _on_select_all(self, switch: Adw.SwitchRow, _pspec) -> None:
        if self._suppress_select:
            return
        active = switch.get_active()
        for _course, _lesson, button in self._checks:
            button.set_active(active)
        self._update_selection()

    # ---------- 下载 ----------

    def _on_download(self, *_args) -> None:
        if self._busy:
            self._toast("还在整理讲次，等进度条走完再点下载")
            return
        jobs = [
            (course, lesson)
            for course, lesson, button in self._checks
            if lesson.selectable and button.get_active()
        ]
        if not jobs:
            self._toast("先勾选要下载的讲次")
            return
        save_root = self._save_root()
        if save_root is None:
            self._pending_jobs = jobs
            self._toast("先选一个保存文件夹")
            self._choose_dir()
            return
        try:
            if save_root.exists() and not save_root.is_dir():
                self._notify_error("保存位置不是目录")
                return
            save_root.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._notify_error("无法使用这个保存目录")
            return
        self._remember()
        self._set_busy(True)
        self._stop.clear()
        self._clear_result()
        threading.Thread(
            target=self._download_worker, args=(jobs, save_root), daemon=True
        ).start()

    def _download_worker(self, jobs, save_root: Path) -> None:
        try:
            courses = []
            seen = set()
            for course, _lesson in jobs:
                if id(course) not in seen:
                    seen.add(id(course))
                    courses.append(course)
            index = self._scan_library(courses, save_root)
            matched = self._matched_count(courses)
            if self._library_files:
                self._status(
                    f"目录里已有 {len(self._library_files)} 个 PDF，其中 {matched} 个会跳过。",
                    pulse=True,
                )
            def on_progress(done: int, total: int, text: str) -> None:
                fraction = 0.0 if total <= 0 else done / total

                def apply(done=done, total=total, text=text, fraction=fraction) -> None:
                    self.status_row.set_subtitle(_esc(redact(text)))
                    self.progress.set_fraction(max(0.0, min(1.0, fraction)))
                    self.progress.set_text(f"{done}/{total}")

                self.ui(apply)

            def on_page(title: str, done: int, total: int) -> None:
                def apply(title=title, done=done, total=total) -> None:
                    if total:
                        self.status_row.set_subtitle(
                            _esc(f"{title}：第 {done}/{total} 页")
                        )

                self.ui(apply)

            results = download_selected(
                self._fetch,
                jobs,
                save_root,
                on_progress,
                self._stop,
                existing=index,
                on_page=on_page,
            )
            summary, _detail = format_report(results)

            def finish(summary=summary) -> None:
                self._show_result(results, summary, save_root)
                # 第二、三个参数是 courses / root，别把目录当成课程传进去
                self._scan_library(courses, save_root)
                self._update_library_row()
                self._set_busy(False)

            self.ui(finish)
        except Exception as exc:
            self._notify_error(str(exc) or "下载失败")

    def _clear_result(self) -> None:
        if self._result_group is not None:
            self.page.remove(self._result_group)
            self._result_group = None

    def _show_result(self, results, summary: str, save_root: Path) -> None:
        self.progress.set_fraction(1.0)
        self.progress.set_text("完成")
        self.status_row.set_subtitle(_esc(redact(summary)))
        self.status_spinner.set_visible(False)
        self._toast(summary)
        group = Adw.PreferencesGroup()
        group.set_title("6　下载结果")
        _describe(group, summary)
        left = [item for item in results if item.status != "已下载"]
        touched = sorted({item.folder for item in results if item.folder})
        where = Adw.ActionRow(title="保存位置", subtitle=_esc(str(save_root)))
        group.add(where)
        if not left:
            group.add(Adw.ActionRow(title="全部成功", subtitle=_esc(f"{len(results)} 个文件")))
        for item in left:
            row = Adw.ActionRow(
                title=_esc(item.filename),
                subtitle=_esc(f"{item.course} · {item.status} {item.detail}".strip()),
            )
            group.add(row)

        files = library.scan(save_root)
        for folder in touched:
            items = [item for item in files if item.folder == folder]
            holder = Adw.ExpanderRow(
                title=_esc(folder + "/"),
                subtitle=_esc(f"{len(items)} 个 PDF"),
            )
            holder.set_expanded(len(touched) <= 2)
            for item in items[:40]:
                holder.add_row(Adw.ActionRow(title=_esc(item.name)))
            if len(items) > 40:
                holder.add_row(Adw.ActionRow(title=_esc(f"……还有 {len(items) - 40} 个")))
            group.add(holder)
        self.page.add(group)
        self._result_group = group

    def _on_stop(self, *_args) -> None:
        self._stop.set()
        self._status("正在停止：手上这一讲做完就停。")

    # ---------- 目录 ----------

    def _on_dir_changed(self, *_args) -> None:
        self._remember()
        self._forget_library()

    def _forget_library(self) -> None:
        self._library_files = []
        self._library = None
        if hasattr(self, "library_row"):
            self.library_row.set_subtitle("还没扫描")

    def _scan_library(self, courses=None, root: Path | None = None):
        """递归扫保存目录，和当前讲次对上号。"""
        target = root or self._save_root()
        if target is None or not target.is_dir():
            self._forget_library()
            return {}
        self._scan_stats = {}
        files = library.scan(target, stats=self._scan_stats)
        self._library_files = files
        if courses is None:
            courses = self._visible(self._current_term(), self.all_terms.get_active())
        self._library = library.Library(target, files).bind(courses)
        return self._library

    def _matched_count(self, courses=None) -> int:
        if self._library is None:
            return 0
        if courses is None:
            courses = self._visible(self._current_term(), self.all_terms.get_active())
        count = 0
        for course in courses:
            for lesson in course.lessons:
                if lesson.selectable and self._library.matched_ok(course, lesson):
                    count += 1
        return count

    def _near_count(self, courses=None) -> int:
        """名字相近、但还没精确对上的讲次数量（下载时按页数再核对）。"""
        if self._library is None:
            return 0
        if courses is None:
            courses = self._visible(self._current_term(), self.all_terms.get_active())
        count = 0
        for course in courses:
            for lesson in course.lessons:
                if not lesson.selectable or self._library.matched(course, lesson):
                    continue
                if self._library.near(lesson.title, limit=1):
                    count += 1
        return count

    def _update_library_row(self) -> None:
        root = self._save_root()
        if root is None or not root.is_dir():
            self.library_row.set_subtitle("还没有选目录")
            return
        visible = self._visible(self._current_term(), self.all_terms.get_active())
        matched = self._matched_count(visible)
        near = self._near_count(visible)
        broken = len(self._library.suspect(visible)) if self._library else 0
        extra = f"，另有 {near} 讲名称相近（下载时按页数核对）" if near else ""
        if broken:
            extra += f"；{broken} 个文件疑似不完整，会重新下"
        if self._scan_stats.get("truncated"):
            extra += "；文件太多，只扫了前 5000 个"
        self.library_row.set_subtitle(
            f"{len(self._library_files)} 个 PDF，其中 {matched} 个对得上当前讲次（下载会跳过）{extra}"
        )

    def _on_tidy(self, *_args) -> None:
        if self._busy:
            self._toast("先等当前任务结束")
            return
        root = self._save_root()
        if root is None or not root.is_dir():
            self._toast("先选一个已有的保存文件夹")
            return
        self._set_busy(True)
        self._toast("正在扫描目录…")

        def run() -> None:
            try:
                visible = self._visible(self._current_term(), self.all_terms.get_active())
                self._scan_library(visible, root)
                plan = library.plan_tidy(root, visible, self._library_files)
                self._tidy_plan = plan
                self.ui(lambda: self._show_tidy_plan(plan, root))
            except Exception as exc:
                self._notify_error(str(exc) or "扫描目录失败")
                return
            self.ui(lambda: self._set_busy(False))

        threading.Thread(target=run, daemon=True).start()

    def _show_tidy_plan(self, plan, root: Path) -> None:
        lines = [library.summarize(plan), ""]
        for move in plan.moves[:10]:
            note = f"（{move.reason}）" if move.reason else ""
            lines.append(
                f"{move.source.relative}  →  "
                f"{move.target.relative_to(root).as_posix()}{note}"
            )
        if len(plan.moves) > 10:
            lines.append(f"……还有 {len(plan.moves) - 10} 个")
        for clash in plan.conflicts[:5]:
            note = clash.note()
            extra = f"（{note}）" if note else ""
            lines.append(f"没动，内容冲突{extra}：{clash.source.relative}")
        for first, second in plan.duplicates[:5]:
            lines.append(f"内容重复（没动，可自行删一个）：{first.relative}")
        for item in plan.unmatched[:5]:
            lines.append(f"没认出来（没动）：{item.relative}")
        if len(plan.unmatched) > 5:
            lines.append(f"……还有 {len(plan.unmatched) - 5} 个没认出来")
        body = "\n".join(lines) if lines else "没有可整理的文件"
        if not plan.moves:
            self._toast("目录里没有需要整理的文件")
            return
        dialog = _alert("整理目录", body)
        if hasattr(dialog, "add_response"):
            dialog.add_response("cancel", "取消")
            dialog.add_response("go", "开始整理")
            if hasattr(Adw, "ResponseAppearance"):
                dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
            dialog.connect("response", self._on_tidy_response)
            dialog.present(self)
        else:
            self._toast(body)

    def _on_tidy_response(self, dialog, response: str) -> None:
        if response != "go" or self._tidy_plan is None:
            return
        plan = self._tidy_plan

        def run() -> None:
            try:
                done, failed = library.apply_tidy(plan)
            except Exception as exc:
                self._notify_error(str(exc) or "整理失败")
                return
            self._tidy_plan = None
            parked = len([item for item in done if "另一版本" in item.target.name])
            text = f"已整理 {len(done)} 个文件" + (f"，{len(failed)} 个没动" if failed else "")
            if parked:
                text += f"（{parked} 个同名文件内容不同，已另存一份）"
            self._toast(text)
            self._scan_library()
            self.ui(self._update_library_row)

        threading.Thread(target=run, daemon=True).start()

    def _on_url_changed(self, *_args) -> None:
        self._remember()

    def _on_choose_dir(self, *_args) -> None:
        self._choose_dir()

    def _choose_dir(self) -> None:
        current = self._save_root()
        start = (
            Gio.File.new_for_path(str(current))
            if current is not None and current.is_dir()
            else None
        )
        if hasattr(Gtk, "FileDialog"):
            dialog = Gtk.FileDialog()
            dialog.set_title("选择保存目录")
            if start is not None:
                dialog.set_initial_folder(start)
            dialog.select_folder(self, None, self._on_folder)
            return
        # GTK 4.6 没有 Gtk.FileDialog，退回 FileChooserNative（4.0 就有）
        chooser = Gtk.FileChooserNative.new(
            "选择保存目录", self, Gtk.FileChooserAction.SELECT_FOLDER, "选择", "取消"
        )
        if start is not None:
            try:
                chooser.set_current_folder(start)
            except (AttributeError, TypeError):
                pass
        chooser.connect("response", self._on_native_folder)
        self._native_chooser = chooser       # 保持引用，别被回收
        chooser.show()

    def _on_folder(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return
        if folder is None:
            return
        path = folder.get_path()
        if not path:
            return
        self._apply_folder(path)

    def _on_native_folder(self, chooser, response) -> None:
        self._native_chooser = None
        if response != Gtk.ResponseType.ACCEPT:
            return
        chosen = chooser.get_file()
        path = chosen.get_path() if chosen is not None else None
        if path:
            self._apply_folder(path)

    def _apply_folder(self, path: str) -> None:
        self.dir_row.set_text(path)
        self._remember()
        pending = self._pending_jobs
        self._pending_jobs = None
        self._library_files: list[library.FoundFile] = []
        self._library: library.Library | None = None
        self._tidy_plan = None
        if pending:
            self._on_download()

    def _on_open_dir(self, *_args) -> None:
        target = self._save_root()
        if target is None:
            self._toast("还没有选保存文件夹")
            return
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            self._notify_error("无法创建保存目录")
            return
        try:
            platforms.open_folder(target)
        except OSError:
            self._notify_error("打不开文件管理器")

    def _on_close_browser(self) -> None:
        self._browser.terminate_profile()
        self._course_url = None
        self._toast("已关掉本工具的登录窗口")

    def _show_cli(self) -> None:
        _alert("命令行用法", CLI_HELP).present(self)

    def _show_about(self) -> None:
        _about(
            "雨课堂课件",
            __version__,
            "下载本人课程的幻灯片，合成 PDF；命令行可整批下载。\n仅供本人学习，不要再传播。",
        ).present(self)

    # ---------- 杂项 ----------

    def _install_terms(self, terms: list[str], selected: str) -> None:
        self._suppress_term = True
        try:
            self._terms = list(terms)
            model = Gtk.StringList()
            for term in terms:
                model.append(term)
            self.term_combo.set_model(model)
            index = terms.index(selected) if selected in terms else 0
            self.term_combo.set_selected(index)
        finally:
            self._suppress_term = False

    def _on_destroy(self, *_args) -> None:
        self._alive = False
        self._stop.set()

    def ui(self, callback) -> None:
        def runner() -> bool:
            if self._alive:
                callback()
            return False

        GLib.idle_add(runner)


def _esc(text: str) -> str:
    """Adw 的行标题按 Pango markup 解析，课程名里的 & < > 必须转义，否则整行不显示。"""
    return GLib.markup_escape_text(str(text or ""))


def _describe(group, text: str) -> None:
    """PreferencesGroup.description 在旧版 libadwaita 上没有，按能力设置。"""
    if text and hasattr(group, "set_description"):
        group.set_description(_esc(text))


def _toolbar_view():
    """Adw.ToolbarView 是 libadwaita 1.4 才有的，Ubuntu 22.04（1.0）没有。

    退回一个竖直的 Gtk.Box：顶栏一个 HeaderBar、下面放内容，效果一样。
    """
    if hasattr(Adw, "ToolbarView"):
        return Adw.ToolbarView()
    return _ToolbarFallback()


class _ToolbarFallback(Gtk.Box):
    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

    def add_top_bar(self, widget) -> None:
        self.append(widget)

    def set_content(self, widget) -> None:
        widget.set_vexpand(True)
        self.append(widget)


def _banner():
    """Adw.Banner 是 libadwaita 1.3 才有的；老系统上用一条会自己消失的提示条。"""
    if hasattr(Adw, "Banner"):
        banner = Adw.Banner(title="")
        banner.connect("button-clicked", lambda *_args: banner.set_revealed(False))
        return banner
    return _BannerShim()


class _BannerShim(Gtk.Revealer):
    """Adw.Banner 的最小替身：set_title / set_revealed，点右边 × 收起。"""

    def __init__(self) -> None:
        super().__init__()
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.set_margin_top(8)
        bar.set_margin_bottom(8)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.add_css_class("card")
        self._label = Gtk.Label(label="", xalign=0, wrap=True, hexpand=True)
        close = Gtk.Button(icon_name="window-close-symbolic")
        close.add_css_class("flat")
        close.set_valign(Gtk.Align.CENTER)
        close.connect("clicked", lambda *_args: self.set_revealed(False))
        bar.append(self._label)
        bar.append(close)
        self.set_child(bar)

    def set_title(self, text: str) -> None:
        self._label.set_label(text or "")

    def set_revealed(self, value: bool) -> None:
        self.set_reveal_child(bool(value))


def _alert(heading: str, body: str):
    if hasattr(Adw, "AlertDialog"):
        return Adw.AlertDialog(heading=heading, body=body)
    return _MessageDialog(heading, body)


def _about(name: str, version: str, comments: str):
    if hasattr(Adw, "AboutDialog"):
        return Adw.AboutDialog(
            application_name=name,
            application_icon="yuketang-courseware",
            version=version,
            comments=comments,
            developer_name="本地工具",
            copyright="仅供本人学习使用",
        )
    return _MessageDialog(name, f"{version}\n\n{comments}")


class _MessageDialog:
    """没有 Adw.AlertDialog / Adw.AboutDialog 时的兜底对话框。"""

    def __init__(self, heading: str, body: str) -> None:
        self._dialog = Gtk.Window(title=heading)
        self._dialog.set_default_size(420, 260)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(18)
        box.set_margin_bottom(18)
        box.set_margin_start(18)
        box.set_margin_end(18)
        label = Gtk.Label(label=body, xalign=0, wrap=True, selectable=True)
        label.set_vexpand(True)
        box.append(label)
        close = Gtk.Button(label="关闭")
        close.connect("clicked", lambda *_args: self._dialog.destroy())
        box.append(close)
        self._dialog.set_child(box)

    def present(self, parent) -> None:
        self._dialog.set_transient_for(parent)
        self._dialog.present()
