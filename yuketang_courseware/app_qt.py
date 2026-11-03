"""雨课堂课件窗口（Windows 后端：PySide6）。

界面和 GTK 版一一对应：登录 → 选学期 → 勾讲次 → 选目录 → 下载。
抓取、合并 PDF 都在工作线程里跑，进度通过 Qt 信号回到主线程；
实际逻辑全部复用 catalog / library / slides，不在这里重复一遍。
"""

from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QCloseEvent, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

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

URL_HINT = "https://xxx.yuketang.cn/v2/web/index"
CLI_HELP = """\
yuketang-courseware --url <网址> --list        只看清单
yuketang-courseware --url <网址> --all         整批下载（最新学期）
yuketang-courseware --url <网址> --all --course 自动控制
yuketang-courseware --url <网址> --all --all-terms
yuketang-courseware --help                     全部选项
"""

LOGIN_TIMEOUT = 900.0
POLL_SECONDS = 1.5


class UiBridge(QObject):
    """工作线程 → 主线程。Qt 的队列连接保证回调在 GUI 线程里执行。"""

    call = Signal(object)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.call.connect(self._run, Qt.ConnectionType.QueuedConnection)

    def _run(self, callback) -> None:
        try:
            callback()
        except Exception:  # 界面回调出错不该把整个窗口带下去
            traceback.print_exc()


def main() -> int:
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("yuketang-courseware")
    app.setApplicationDisplayName("雨课堂课件")
    icon = platforms.resource_path("docs/icon-128.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    app.setStyleSheet(_STYLE)
    window = MainWindow()
    window.show()
    return int(app.exec())


_STYLE = """
QWidget { font-size: 13px; }
QGroupBox {
    border: 1px solid palette(midlight);
    border-radius: 8px;
    margin-top: 14px;
    padding-top: 6px;
    font-weight: 600;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
}
QLabel#title { font-size: 18px; font-weight: 700; }
QLabel#subtitle, QLabel#hint { color: palette(dark); font-weight: 400; }
QPushButton#primary {
    background: #3584e4;
    color: #ffffff;
    border: none;
    border-radius: 6px;
    padding: 6px 16px;
    font-weight: 600;
}
QPushButton#primary:disabled { background: #9db8d2; }
QPushButton#primary:hover { background: #1c71d8; }
QFrame#header { background: palette(alternate-base); border-bottom: 1px solid palette(midlight); }
QFrame#banner {
    background: #fce8e6;
    color: #7a1c12;
    border-bottom: 1px solid #f3b3ac;
}
QFrame#bottom { border-top: 1px solid palette(midlight); }
QTreeWidget { border: 1px solid palette(midlight); border-radius: 6px; }
"""



class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("雨课堂课件")
        self.resize(920, 820)

        self._alive = True
        self._busy = False
        self._suppress_term = False
        self._suppress_select = False
        self._syncing = False
        self._stop = threading.Event()
        self._fetch_timeout = 60.0
        self._browser = CoursewareBrowser()
        self._course_url: CourseUrl | None = None
        self._all_courses: list[CourseItem] = []
        self._terms: list[str] = []
        self._checks: list[tuple[CourseItem, LessonItem, QTreeWidgetItem]] = []
        self._course_items: dict[int, QTreeWidgetItem] = {}
        self._lesson_items: dict[int, QTreeWidgetItem] = {}
        self._pending_jobs = None
        self._library_files: list[library.FoundFile] = []
        self._scan_stats: dict = {}
        self._library: library.Library | None = None
        self._tidy_plan = None
        self._fill_seconds = 0.0
        self._bridge = UiBridge(self)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self._remember)

        saved = settings.load()
        self._saved_url = str(saved.get("url") or "")
        self._saved_dir = str(saved.get("directory") or "")

        self._build()
        self._sync_term_controls()
        self._update_selection()
        self._probe_existing()

    # ---------- 界面 ----------

    def _build(self) -> None:
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_header())
        outer.addWidget(self._build_banner())

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        self.body = QVBoxLayout(content)
        self.body.setContentsMargins(18, 14, 18, 14)
        self.body.setSpacing(12)
        self._build_login()
        self._build_pick()
        self._build_courses()
        self._build_save()
        self._build_result()
        self.body.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        outer.addWidget(self._build_bottom())
        self.setCentralWidget(central)

    def _build_header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("header")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 10, 18, 10)
        title = QLabel("雨课堂课件")
        title.setObjectName("title")
        subtitle = QLabel("下载本人课程的幻灯片 · 仅供本人学习")
        subtitle.setObjectName("subtitle")
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(title)
        text.addWidget(subtitle)
        layout.addLayout(text, 1)

        menu = QMenu(self)
        close_action = QAction("关闭登录窗口", menu)
        close_action.triggered.connect(self._on_close_browser)
        cli_action = QAction("命令行用法", menu)
        cli_action.triggered.connect(self._show_cli)
        about_action = QAction("关于", menu)
        about_action.triggered.connect(self._show_about)
        menu.addActions([close_action, cli_action, about_action])
        button = QToolButton()
        button.setText("⋮")
        button.setToolTip("更多")
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setMenu(menu)
        layout.addWidget(button)
        return bar

    def _build_banner(self) -> QWidget:
        self.banner = QFrame()
        self.banner.setObjectName("banner")
        layout = QHBoxLayout(self.banner)
        layout.setContentsMargins(14, 8, 8, 8)
        self.banner_label = QLabel("")
        self.banner_label.setTextFormat(Qt.TextFormat.PlainText)
        self.banner_label.setWordWrap(True)
        layout.addWidget(self.banner_label, 1)
        close = QPushButton("×")
        close.setFlat(True)
        close.setFixedWidth(28)
        close.clicked.connect(self._hide_banner)
        layout.addWidget(close)
        self.banner.setVisible(False)
        return self.banner

    def _group(self, title: str, description: str = "") -> tuple[QGroupBox, QVBoxLayout]:
        box = QGroupBox(title)
        layout = QVBoxLayout(box)
        layout.setContentsMargins(12, 14, 12, 12)
        layout.setSpacing(8)
        if description:
            note = QLabel(description)
            note.setObjectName("hint")
            note.setWordWrap(True)
            layout.addWidget(note)
        self.body.addWidget(box)
        return box, layout

    def _build_login(self) -> None:
        _box, layout = self._group("1　登录雨课堂")

        row = QHBoxLayout()
        self.url_edit = QLineEdit(self._saved_url)
        self.url_edit.setPlaceholderText("粘贴雨课堂网址，例：" + URL_HINT)
        self.url_edit.setToolTip("例：" + URL_HINT)
        self.url_edit.textChanged.connect(self._on_url_changed)
        row.addWidget(self.url_edit, 1)
        paste = QPushButton("粘贴")
        paste.clicked.connect(self._on_paste)
        row.addWidget(paste)
        layout.addLayout(row)

        how = QLabel(
            "怎么拿到网址？\n"
            "1. 在你自己的浏览器里打开雨课堂，随便点开一门课。\n"
            "2. 复制地址栏整条网址（每个人、每个学校的入口都不一样）。\n"
            "3. 粘贴到上面，再点下面的「登录雨课堂」。"
        )
        how.setObjectName("hint")
        how.setWordWrap(True)
        layout.addWidget(how)

        status = QHBoxLayout()
        self.spinner = QProgressBar()
        self.spinner.setRange(0, 0)
        self.spinner.setTextVisible(False)
        self.spinner.setFixedWidth(90)
        self.spinner.setVisible(False)
        status.addWidget(self.spinner)
        self.status_label = QLabel("还没有登录")
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setWordWrap(True)
        status.addWidget(self.status_label, 1)
        self.btn_login = QPushButton("登录雨课堂")
        self._primary(self.btn_login)
        self.btn_login.clicked.connect(self._on_login)
        status.addWidget(self.btn_login)
        layout.addLayout(status)

    def _build_pick(self) -> None:
        _box, layout = self._group("2　选课")

        term_row = QHBoxLayout()
        term_label = QLabel("学期")
        term_label.setFixedWidth(52)
        term_row.addWidget(term_label)
        self.term_combo = QComboBox()
        self.term_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.term_combo.currentIndexChanged.connect(self._on_term)
        term_row.addWidget(self.term_combo, 1)
        layout.addLayout(term_row)

        self.all_terms = QCheckBox("全部学期")
        self.all_terms.setToolTip("把往年的课一起列出来，默认只看最新学期")
        self.all_terms.toggled.connect(self._on_all_terms)
        layout.addWidget(self.all_terms)

        self.select_all = QCheckBox("全选所有课程")
        self.select_all.setToolTip("一键勾上全部讲次")
        self.select_all.toggled.connect(self._on_select_all)
        layout.addWidget(self.select_all)

    def _build_courses(self) -> None:
        _box, layout = self._group(
            "3　课程与讲次", "点课程看每一讲；勾上的就下载，已下载过的自动跳过。"
        )
        self.course_note = QLabel("还没有读到课程，点上面的「登录雨课堂」。")
        self.course_note.setObjectName("hint")
        self.course_note.setWordWrap(True)
        layout.addWidget(self.course_note)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setColumnCount(2)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setMinimumHeight(220)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.tree)

    def _build_save(self) -> None:
        _box, layout = self._group(
            "4　保存", "结构是 保存目录/课程名/课件/讲次.pdf；不选目录就不下载，不替你猜。"
        )
        row = QHBoxLayout()
        self.dir_edit = QLineEdit(self._saved_dir)
        self.dir_edit.setPlaceholderText("选择一个保存文件夹")
        self.dir_edit.textChanged.connect(self._on_dir_changed)
        row.addWidget(self.dir_edit, 1)
        self.btn_choose = QPushButton("选择")
        self.btn_choose.clicked.connect(self._choose_dir)
        row.addWidget(self.btn_choose)
        self.btn_open = QPushButton("打开")
        self.btn_open.clicked.connect(self._on_open_dir)
        row.addWidget(self.btn_open)
        layout.addLayout(row)

        library_row = QHBoxLayout()
        self.library_label = QLabel("目录里已有：还没扫描")
        self.library_label.setTextFormat(Qt.TextFormat.PlainText)
        self.library_label.setWordWrap(True)
        library_row.addWidget(self.library_label, 1)
        self.btn_tidy = QPushButton("整理")
        self.btn_tidy.setToolTip("把目录里已有的 PDF 归到「课程/讲次.pdf」，只搬不删")
        self.btn_tidy.clicked.connect(self._on_tidy)
        library_row.addWidget(self.btn_tidy)
        layout.addLayout(library_row)

    def _build_result(self) -> None:
        self.result_box = QGroupBox("下载结果")
        layout = QVBoxLayout(self.result_box)
        layout.setContentsMargins(12, 14, 12, 12)
        self.result_label = QLabel("")
        self.result_label.setTextFormat(Qt.TextFormat.PlainText)
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        self.result_text = QPlainTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setMaximumHeight(170)
        self.result_text.setVisible(False)
        layout.addWidget(self.result_text)
        self.result_box.setVisible(False)
        self.body.addWidget(self.result_box)

    def _build_bottom(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("bottom")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 10, 18, 12)
        self.selection_label = QLabel("")
        layout.addWidget(self.selection_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        layout.addWidget(self.progress, 1)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._on_stop)
        layout.addWidget(self.btn_stop)
        self.btn_download = QPushButton("开始下载")
        self.btn_download.setMinimumWidth(150)
        self._primary(self.btn_download)
        self.btn_download.clicked.connect(self._on_download)
        layout.addWidget(self.btn_download)
        return bar

    @staticmethod
    def _primary(button: QPushButton) -> None:
        button.setObjectName("primary")
        button.setMinimumHeight(32)

    # ---------- 状态 ----------

    def _remember(self) -> None:
        url = self.url_edit.text().strip() if hasattr(self, "url_edit") else self._saved_url
        directory = self.dir_edit.text().strip() if hasattr(self, "dir_edit") else ""
        settings.save(url, directory)

    def _schedule_remember(self) -> None:
        self._save_timer.start()

    def _save_root(self) -> Path | None:
        """还没选目录就返回 None，不猜路径。"""
        text = self.dir_edit.text().strip()
        if not text:
            return None
        # 从开始菜单启动的窗口没有稳定的当前目录，相对路径一律按家目录算
        return resolve_directory(text, base=Path.home())

    def _status(self, text: str, pulse: bool = False) -> None:
        def apply() -> None:
            self.status_label.setText(redact(text))
            self.spinner.setVisible(pulse)
            if pulse:
                self.progress.setValue(0)

        self._ui(apply)

    def _toast(self, text: str) -> None:
        def apply() -> None:
            self.statusBar().showMessage(redact(text), 6000)

        self._ui(apply)

    def _notify_error(self, text: str) -> None:
        def apply() -> None:
            self.banner_label.setText(redact(text))
            self.banner.setVisible(True)
            self.spinner.setVisible(False)
            self.progress.setValue(0)
            self.status_label.setText(redact(text))
            self._set_busy(False)

        self._ui(apply)

    def _hide_banner(self) -> None:
        self.banner.setVisible(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for widget in (
            self.btn_login,
            self.url_edit,
            self.dir_edit,
            self.btn_choose,
            self.btn_open,
            self.btn_tidy,
            self.term_combo,
            self.all_terms,
            self.select_all,
        ):
            widget.setEnabled(not busy)
        self.btn_stop.setEnabled(busy)
        self.btn_download.setEnabled(not busy and self._chosen_count() > 0)
        self._sync_term_controls()

    def _chosen_count(self) -> int:
        return sum(
            1
            for _course, _lesson, item in self._checks
            if item.checkState(0) == Qt.CheckState.Checked
        )

    def _sync_term_controls(self) -> None:
        every = self.all_terms.isChecked() if hasattr(self, "all_terms") else False
        self.term_combo.setEnabled(not every and not self._busy)
        self.all_terms.setEnabled(not self._busy)
        self.select_all.setEnabled(not self._busy)

    def _update_selection(self) -> None:
        if self._syncing:
            return
        total = len(self._checks)
        chosen = self._chosen_count()
        self.selection_label.setText(f"已选 {chosen} / {total} 讲")
        self.btn_download.setEnabled(bool(chosen) and not self._busy)
        self._suppress_select = True
        try:
            self.select_all.setChecked(total > 0 and chosen == total)
        finally:
            self._suppress_select = False

    # ---------- 登录 ----------

    def _on_paste(self) -> None:
        text = QApplication.clipboard().text() or ""
        line = text.strip().splitlines()[0] if text.strip() else ""
        if not line:
            self._notify_error("剪贴板里没有网址")
            return
        self.url_edit.setText(line)
        self._remember()

    def _on_login(self) -> None:
        if self._busy:
            return
        text = self.url_edit.text().strip()
        if not text:
            self.banner_label.setText(
                "先在第 1 步粘贴雨课堂网址：在自己浏览器里打开雨课堂，随便点开一门课，"
                "把地址栏整条复制过来。"
            )
            self.banner.setVisible(True)
            self.url_edit.setFocus()
            return
        self._set_busy(True)
        self._stop.clear()
        self._hide_banner()
        self._status("正在打开浏览器…", pulse=True)
        threading.Thread(target=self._login_worker, args=(text,), daemon=True).start()

    def _login_worker(self, text: str) -> None:
        try:
            course_url = parse_course_url(text)
            self._course_url = course_url
            # 控件只能在 GUI 线程里读，排到主线程去存
            self._ui(self._remember)
            self._browser.open_url(course_url.url)
            self._status(
                "浏览器已打开。扫码或用账号登录，登录成功后课程会自动出现。", pulse=True
            )
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
            text = (self._saved_url or "").strip()
            if not text:
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
            self._ui(lambda: self.url_edit.setText(course_url.url))
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
            self._reload(term=selected, every=self.all_terms.isChecked())

        self._ui(finish)

    def _on_term(self, _index: int) -> None:
        if self._suppress_term or self._busy or not self._all_courses:
            return
        index = self.term_combo.currentIndex()
        if index < 0 or index >= len(self._terms):
            return
        self._reload(term=self._terms[index])

    def _on_all_terms(self, checked: bool) -> None:
        self._sync_term_controls()
        if not self._all_courses or self._busy:
            return
        self._reload(term=self._current_term(), every=checked)

    def _reload(self, term: str = "", every: bool | None = None) -> None:
        if self._busy:
            return
        if every is None:
            every = self.all_terms.isChecked()
        self._set_busy(True)
        self._stop.clear()

        def run() -> None:
            try:
                visible = self._visible(term, every)
                self._ui(lambda: self._start_fill(visible))
                self._fill_lessons(visible)
                self._ui(lambda: self._finish_fill(visible))
            except Exception as exc:
                self._notify_error(str(exc) or "读取失败")
                return
            self._ui(lambda: self._set_busy(False))

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
        """清空课程区。已经读过的课直接摆上去，其余的读完一门出现一门。"""
        self._syncing = True
        try:
            self.tree.clear()
            self._checks.clear()
            self._course_items.clear()
            self._lesson_items.clear()
            self._fill_seconds = 0.0
            expand = len(courses) <= 3
            for course in courses:
                if course.loaded:
                    self._add_course(course, expand)
        finally:
            self._syncing = False
        self._update_selection()
        if not courses:
            self.course_note.setText("这个学期没有课程。换个学期，或打开「全部学期」。")
            self.course_note.setVisible(True)
        elif not self._course_items:
            self._status(f"正在整理 {len(courses)} 门课的讲次…", pulse=True)

    def _fill_lessons(self, courses: list[CourseItem]) -> None:
        pending = [course for course in courses if not course.loaded]
        if not pending:
            return
        expand = len(courses) <= 3
        began = time.time()

        def on_done(course: CourseItem, done: int, total: int) -> None:
            count = len([item for item in course.lessons if item.selectable])
            self._status(
                f"整理讲次 {done}/{total}：{course.course_name}（{count} 讲）", pulse=True
            )
            self._ui(lambda course=course: self._append_course(course, expand))
            self._ui(lambda done=done, total=total: self._set_fraction(done, total))

        load_many(self._fetch, pending, workers=2, on_done=on_done, stop=self._stop)
        self._fill_seconds = time.time() - began

    def _append_course(self, course: CourseItem, expand: bool) -> None:
        if not self._alive:
            return
        self.course_note.setVisible(False)
        self._syncing = True
        try:
            self._add_course(course, expand)
        finally:
            self._syncing = False
        self._update_selection()

    def _finish_fill(self, courses: list[CourseItem]) -> None:
        self._syncing = False
        if not courses:
            return
        lessons = [
            lesson for course in courses for lesson in course.lessons if lesson.selectable
        ]
        line = f"已读到 {len(courses)} 门课、{len(lessons)} 个可下载讲次，讲次已默认勾好。"
        if self._fill_seconds:
            line += f"（整理讲次 {self._fill_seconds:.1f} 秒）"
        note = timing_summary()
        if note:
            line += f" {note}"
        self._status(line)
        self._update_selection()

    def _set_fraction(self, done: int, total: int) -> None:
        self.progress.setValue(int(1000 * done / max(1, total)))

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

    # ---------- 课程树 ----------

    def _add_course(self, course: CourseItem, expand: bool) -> None:
        selectable = [lesson for lesson in course.lessons if lesson.selectable]
        title = course.course_name
        if course.class_name and course.class_name != course.course_name:
            title = f"{course.course_name} · {course.class_name}"
        subtitle = f"{len(selectable)} 讲可下载"
        if self.all_terms.isChecked() and course.term:
            subtitle += f" · 学期 {course.term}"
        if course.error:
            subtitle = course.error
        elif course.no_activity:
            subtitle = "无课件"

        item = QTreeWidgetItem([title, subtitle])
        flags = item.flags() | Qt.ItemFlag.ItemIsUserCheckable
        if selectable:
            item.setFlags(flags)
            item.setCheckState(0, Qt.CheckState.Checked)
        self.tree.addTopLevelItem(item)
        self._course_items[id(course)] = item

        for lesson in course.lessons:
            bits = []
            if lesson.source == SOURCE_LEAF:
                bits.append("课件区")
            if lesson.note:
                bits.append(lesson.note)
            row = QTreeWidgetItem([lesson.title, " · ".join(bits)])
            if lesson.selectable:
                row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                row.setCheckState(0, Qt.CheckState.Checked)
                row.setData(0, Qt.ItemDataRole.UserRole, (id(course), id(lesson)))
                self._checks.append((course, lesson, row))
                self._lesson_items[id(lesson)] = row
            else:
                row.setFlags(row.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            item.addChild(row)
        # 展开要在子项都挂上之后设置，否则插进树里又会被收起来
        item.setExpanded(expand)

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if self._syncing or column != 0:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data is None:
            # 课程行被点：同步它下面的讲次
            self._syncing = True
            try:
                state = item.checkState(0)
                for index in range(item.childCount()):
                    child = item.child(index)
                    if child.data(0, Qt.ItemDataRole.UserRole) is None:
                        continue
                    child.setCheckState(
                        0, Qt.CheckState.Checked if state != Qt.CheckState.Unchecked else Qt.CheckState.Unchecked
                    )
            finally:
                self._syncing = False
            self._update_selection()
            return

        course_id, _lesson_id = data
        parent = item.parent()
        if parent is not None:
            states = []
            for index in range(parent.childCount()):
                child = parent.child(index)
                if child.data(0, Qt.ItemDataRole.UserRole) is not None:
                    states.append(child.checkState(0) == Qt.CheckState.Checked)
            self._syncing = True
            try:
                if states and all(states):
                    parent.setCheckState(0, Qt.CheckState.Checked)
                elif states and any(states):
                    parent.setCheckState(0, Qt.CheckState.PartiallyChecked)
                else:
                    parent.setCheckState(0, Qt.CheckState.Unchecked)
            finally:
                self._syncing = False
        self._update_selection()

    def _on_select_all(self, checked: bool) -> None:
        if self._suppress_select:
            return
        self._syncing = True
        try:
            state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            for _course, _lesson, item in self._checks:
                item.setCheckState(0, state)
            for item in self._course_items.values():
                item.setCheckState(0, state)
        finally:
            self._syncing = False
        self._update_selection()

    # ---------- 下载 ----------

    def _on_download(self) -> None:
        if self._busy:
            self._toast("还在整理讲次，等进度条走完再点下载")
            return
        jobs = [
            (course, lesson)
            for course, lesson, item in self._checks
            if lesson.selectable and item.checkState(0) == Qt.CheckState.Checked
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
        self.result_box.setVisible(False)
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
            self._scan_library(courses, save_root)
            matched = self._matched_count(courses)
            if self._library_files:
                self._status(
                    f"目录里已有 {len(self._library_files)} 个 PDF，其中 {matched} 个会跳过。",
                    pulse=True,
                )

            def on_progress(done: int, total: int, text: str) -> None:
                def apply(done=done, total=total, text=text) -> None:
                    self.status_label.setText(redact(text))
                    self.progress.setValue(int(1000 * done / max(1, total)))

                self._ui(apply)

            def on_page(title: str, done: int, total: int) -> None:
                def apply(title=title, done=done, total=total) -> None:
                    if total:
                        self.status_label.setText(f"{title}：第 {done}/{total} 页")

                self._ui(apply)

            results = download_selected(
                self._fetch,
                jobs,
                save_root,
                on_progress,
                self._stop,
                existing=self._library,
                on_page=on_page,
            )
            summary, detail = format_report(results)

            def finish(summary=summary, detail=detail) -> None:
                self._show_result(results, summary, detail, save_root)
                self._scan_library(courses, save_root)
                self._update_library_label()
                self._set_busy(False)

            self._ui(finish)
        except Exception as exc:
            self._notify_error(str(exc) or "下载失败")

    def _show_result(self, results, summary: str, detail: str, save_root: Path) -> None:
        self.progress.setValue(1000)
        self.status_label.setText(redact(summary))
        self.spinner.setVisible(False)
        self._toast(summary)
        lines = [f"保存位置：{save_root}", ""]
        if detail.strip():
            lines.append(detail.rstrip())
        self.result_label.setText(redact(summary))
        self.result_text.setPlainText("\n".join(lines))
        self.result_text.setVisible(True)
        self.result_box.setVisible(True)

    def _on_stop(self) -> None:
        self._stop.set()
        self._status("正在停止：手上这一讲做完就停。")

    # ---------- 目录 ----------

    def _on_dir_changed(self, _text: str) -> None:
        self._schedule_remember()
        self._forget_library()

    def _forget_library(self) -> None:
        self._library_files = []
        self._library = None
        self._set_library_label("目录里已有：还没扫描")

    def _set_library_label(self, text: str) -> None:
        if self._alive:
            self._ui(lambda: self.library_label.setText(text))

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
            courses = self._visible(self._current_term(), self.all_terms.isChecked())
        self._library = library.Library(target, files).bind(courses)
        return self._library

    def _matched_count(self, courses=None) -> int:
        if self._library is None:
            return 0
        if courses is None:
            courses = self._visible(self._current_term(), self.all_terms.isChecked())
        count = 0
        for course in courses:
            for lesson in course.lessons:
                if lesson.selectable and self._library.matched_ok(course, lesson):
                    count += 1
        return count

    def _update_library_label(self) -> None:
        root = self._save_root()
        if root is None or not root.is_dir():
            self.library_label.setText("目录里已有：还没有选目录")
            return
        visible = self._visible(self._current_term(), self.all_terms.isChecked())
        matched = self._matched_count(visible)
        broken = len(self._library.suspect(visible)) if self._library else 0
        text = f"目录里已有 {len(self._library_files)} 个 PDF，其中 {matched} 个对得上当前讲次（下载会跳过）"
        if broken:
            text += f"；{broken} 个文件疑似不完整，会重新下"
        if self._scan_stats.get("truncated"):
            text += "；文件太多，只扫了前 5000 个"
        self.library_label.setText(text)

    def _on_tidy(self) -> None:
        if self._busy:
            self._toast("先等当前任务结束")
            return
        root = self._save_root()
        if root is None or not root.is_dir():
            self._toast("先选一个已有的保存文件夹")
            return
        self._set_busy(True)
        self._toast("正在扫描目录…")
        # 控件状态先在主线程里取好，工作线程只碰纯数据
        term = self._current_term()
        every = self.all_terms.isChecked()

        def run() -> None:
            try:
                visible = self._visible(term, every)
                self._scan_library(visible, root)
                plan = library.plan_tidy(root, visible, self._library_files)
                self._tidy_plan = plan
                self._ui(lambda: self._show_tidy_plan(plan, root))
            except Exception as exc:
                self._notify_error(str(exc) or "扫描目录失败")
                return
            self._ui(lambda: self._set_busy(False))

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
        for first, _second in plan.duplicates[:5]:
            lines.append(f"内容重复（没动，可自行删一个）：{first.relative}")
        for item in plan.unmatched[:5]:
            lines.append(f"没认出来（没动）：{item.relative}")
        if len(plan.unmatched) > 5:
            lines.append(f"……还有 {len(plan.unmatched) - 5} 个没认出来")
        if not plan.moves:
            self._toast("目录里没有需要整理的文件")
            return
        body = "\n".join(lines)
        answer = QMessageBox.question(
            self,
            "整理目录",
            body + "\n\n开始整理？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._apply_tidy()

    def _apply_tidy(self) -> None:
        if self._tidy_plan is None:
            return
        plan = self._tidy_plan
        self._set_busy(True)

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

            def finish() -> None:
                self._scan_library()
                self._update_library_label()
                self._set_busy(False)

            self._ui(finish)

        threading.Thread(target=run, daemon=True).start()

    def _on_url_changed(self, _text: str) -> None:
        self._schedule_remember()

    def _choose_dir(self) -> None:
        current = self._save_root()
        start = str(current) if current is not None and current.is_dir() else ""
        path = QFileDialog.getExistingDirectory(self, "选择保存目录", start)
        if path:
            self._apply_folder(path)

    def _apply_folder(self, path: str) -> None:
        self.dir_edit.setText(path)
        self._remember()
        pending = self._pending_jobs
        self._pending_jobs = None
        self._forget_library()
        self._tidy_plan = None
        if pending:
            self._on_download()

    def _on_open_dir(self) -> None:
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
        QMessageBox.information(self, "命令行用法", CLI_HELP)

    def _show_about(self) -> None:
        QMessageBox.about(
            self,
            "关于雨课堂课件",
            f"雨课堂课件 {__version__}\n\n"
            "下载本人课程的幻灯片，合成 PDF；命令行可整批下载。\n"
            "仅供本人学习，不要再传播。",
        )

    # ---------- 杂项 ----------

    def _current_term(self) -> str:
        index = self.term_combo.currentIndex()
        if 0 <= index < len(self._terms):
            return self._terms[index]
        return ""

    def _install_terms(self, terms: list[str], selected: str) -> None:
        self._suppress_term = True
        try:
            self._terms = list(terms)
            self.term_combo.clear()
            self.term_combo.addItems(terms)
            index = terms.index(selected) if selected in terms else 0
            self.term_combo.setCurrentIndex(index)
        finally:
            self._suppress_term = False

    def _ui(self, callback) -> None:
        self._bridge.call.emit(callback)

    def closeEvent(self, event: QCloseEvent) -> None:
        self._alive = False
        self._stop.set()
        self._remember()
        event.accept()
