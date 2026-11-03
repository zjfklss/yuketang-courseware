"""Windows 界面（PySide6）的选择逻辑：课程/讲次勾选、全选、计数。

不需要显示器：Qt 的 offscreen 平台在后台跑窗口，CI 和本机都能测。
没装 PySide6 的环境（Linux CI、纯命令行用户）自动跳过。
"""

from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    HAS_QT = True
except ImportError:  # pragma: no cover - 取决于环境
    HAS_QT = False


def _course(name: str, titles: list[str]) -> object:
    from yuketang_courseware.catalog import CourseItem, LessonItem

    course = CourseItem("1", "2026-1", name, "01班", "2874")
    course.loaded = True
    course.folder = name
    course.lessons = [
        LessonItem(14, title, f"cw{index}", True) for index, title in enumerate(titles)
    ]
    return course


@unittest.skipUnless(HAS_QT, "没装 PySide6，跳过 Qt 界面测试")
class SelectionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        from unittest import mock

        from yuketang_courseware import app_qt, settings

        self._settings = (settings.load, settings.save)
        settings.load = lambda: {}
        settings.save = lambda *_args, **_kwargs: None
        patcher = mock.patch.object(
            app_qt.MainWindow, "_probe_existing", lambda _self: None
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.window = app_qt.MainWindow()
        self.window._set_busy(False)

    def tearDown(self) -> None:
        from yuketang_courseware import settings

        self.window.close()
        settings.load, settings.save = self._settings

    def _fill(self) -> None:
        courses = [
            _course("自动控制原理", ["第1讲 绪论", "第2讲 传递函数"]),
            _course("计算方法", ["第二章 插值法"]),
        ]
        self.window._start_fill(courses)
        self.window._finish_fill(courses)

    def _lesson_item(self, course_index: int, lesson_index: int):
        top = self.window.tree.topLevelItem(course_index)
        return top.child(lesson_index)

    def test_all_lessons_checked_by_default(self) -> None:
        self._fill()
        self.assertEqual(self.window.tree.topLevelItemCount(), 2)
        self.assertEqual(self.window._chosen_count(), 3)
        self.assertEqual(self.window.selection_label.text(), "已选 3 / 3 讲")
        self.assertTrue(self.window.btn_download.isEnabled())
        top = self.window.tree.topLevelItem(0)
        self.assertEqual(top.checkState(0), Qt.CheckState.Checked)

    def test_uncheck_one_lesson_makes_course_partial(self) -> None:
        self._fill()
        self._lesson_item(0, 0).setCheckState(0, Qt.CheckState.Unchecked)
        top = self.window.tree.topLevelItem(0)
        self.assertEqual(top.checkState(0), Qt.CheckState.PartiallyChecked)
        self.assertEqual(self.window._chosen_count(), 2)
        self.assertEqual(self.window.selection_label.text(), "已选 2 / 3 讲")
        self.assertTrue(self.window.btn_download.isEnabled())

    def test_course_checkbox_toggles_every_lesson(self) -> None:
        self._fill()
        top = self.window.tree.topLevelItem(0)
        top.setCheckState(0, Qt.CheckState.Unchecked)
        self.assertEqual(self.window._chosen_count(), 1)
        self.assertEqual(self._lesson_item(0, 0).checkState(0), Qt.CheckState.Unchecked)
        top.setCheckState(0, Qt.CheckState.Checked)
        self.assertEqual(self.window._chosen_count(), 3)

    def test_select_all_toggles_everything(self) -> None:
        self._fill()
        self.window.select_all.setChecked(False)
        self.assertEqual(self.window._chosen_count(), 0)
        self.assertFalse(self.window.btn_download.isEnabled())
        self.window.select_all.setChecked(True)
        self.assertEqual(self.window._chosen_count(), 3)
        self.assertTrue(self.window.btn_download.isEnabled())

    def test_terms_follow_saved_selection(self) -> None:
        self.window._install_terms(["2026-2", "2026-1"], "2026-1")
        self.assertEqual(self.window.term_combo.currentIndex(), 1)
        self.assertEqual(self.window._current_term(), "2026-1")

    def test_status_text_is_not_html(self) -> None:
        """课程名里带 <b> 时，状态栏不能把它当富文本解析。"""
        self.window._status("课程 <b>不算标签</b>")
        # _status 通过信号排队到主线程，测试里手动跑一次事件循环
        self.app.processEvents()
        self.assertEqual(self.window.status_label.text(), "课程 <b>不算标签</b>")


if __name__ == "__main__":
    unittest.main()
