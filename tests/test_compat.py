"""跨机器可运行性：语法/注解兼容、旧版 libadwaita 的兜底、环境自检。

`errors.py` 里一句 `str | None` 忘了加 `from __future__ import annotations`，
在 Python 3.8/3.9 上 import 就抛 TypeError——整个程序起不来，看起来像"Python 的锅"。
这里把这一类问题钉死。
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yuketang_courseware import preflight  # noqa: E402

PACKAGE = ROOT / "yuketang_courseware"


def _gtk_available() -> bool:
    """CI 上没有 Gtk4 的 typelib，界面相关的检查要能跳过而不是报错。"""
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk  # noqa: F401
    except (ImportError, ValueError):
        return False
    return True


HAS_GTK = _gtk_available()


def _has_bitor_annotation(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr):
            return True
    return False


def _annotation_nodes(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = list(node.args.args) + list(node.args.kwonlyargs)
            args += [a for a in (node.args.vararg, node.args.kwarg) if a]
            for arg in args:
                if arg.annotation is not None:
                    yield arg.annotation
            if node.returns is not None:
                yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


class AnnotationCompatTest(unittest.TestCase):
    def test_pep604_annotations_need_future_import(self):
        """`X | None` 这种写法在 3.9 及以前会在定义时求值，必须有 future 导入。"""
        offenders = []
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            if "from __future__ import annotations" in source:
                continue
            tree = ast.parse(source)
            for annotation in _annotation_nodes(tree):
                if _has_bitor_annotation(annotation):
                    offenders.append(f"{path.name}:{annotation.lineno}")
        self.assertEqual(offenders, [], f"这些注解在 Python 3.8/3.9 上会崩：{offenders}")

    def test_syntax_is_python38_compatible(self):
        for path in sorted(PACKAGE.glob("*.py")):
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 8))

    def test_no_python39_only_stdlib(self):
        banned = ("removeprefix(", "removesuffix(", "is_relative_to(", "functools.cache")
        for path in sorted(PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            for token in banned:
                with self.subTest(path=path.name, token=token):
                    self.assertNotIn(token, source)


@unittest.skipUnless(HAS_GTK, "没装 Gtk4 / libadwaita，跳过界面兜底检查")
class AdwFallbackTest(unittest.TestCase):
    """旧版 libadwaita 没有 ToolbarView / Banner，兜底类必须提供同样的方法。"""

    def test_toolbar_fallback_api(self):
        from yuketang_courseware import app_gtk as app

        for name in ("add_top_bar", "set_content"):
            self.assertTrue(callable(getattr(app._ToolbarFallback, name, None)), name)

    def test_banner_shim_api(self):
        from yuketang_courseware import app_gtk as app

        for name in ("set_title", "set_revealed"):
            self.assertTrue(callable(getattr(app._BannerShim, name, None)), name)

    def test_helpers_pick_new_api_when_available(self):
        from yuketang_courseware import app_gtk as app

        # 本机 libadwaita 够新时，走的是官方控件
        if hasattr(app.Adw, "ToolbarView"):
            self.assertTrue(hasattr(app._toolbar_view(), "add_top_bar"))


class PreflightTest(unittest.TestCase):
    def test_python_check_passes_here(self):
        check = preflight._python()
        self.assertTrue(check.ok)
        self.assertIn("Python", check.name)

    def test_ok_only_considers_required(self):
        optional = preflight.Check("Pillow", False, "没装", required=False)
        required = preflight.Check("Gtk 4", False, "没装", required=True)
        self.assertTrue(preflight.ok([preflight.Check("x", True, ""), optional]))
        self.assertFalse(preflight.ok([optional, required]))

    def test_min_version_is_declared(self):
        self.assertLessEqual(preflight.MIN_PYTHON, sys.version_info[:2])

    def test_run_reports_every_component(self):
        checks = preflight.run()
        names = " ".join(item.name for item in checks)
        expected = ("Python", "浏览器")
        if sys.platform == "win32":
            expected += ("PySide6",)
        else:
            expected += ("Gtk 4", "libadwaita")
        for item in expected:
            self.assertIn(item, names)


if __name__ == "__main__":
    unittest.main()
