"""文件名和目录规则：Windows 的非法字符、保留设备名、长度，以及配置目录位置。"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from yuketang_courseware import names, platforms


class SafeComponentTest(unittest.TestCase):
    def test_illegal_characters_are_removed(self) -> None:
        self.assertEqual(names.safe_component('a\\b/c:d*e?f"g<h>i|j', "x"), "abcdefghij")

    def test_control_characters_are_removed(self) -> None:
        self.assertEqual(names.safe_component("a\x00b\x1fc", "x"), "abc")

    def test_trailing_dot_and_space_are_trimmed(self) -> None:
        # Windows 上「第1讲 绪论.」和「第1讲 绪论 」都建不出来
        self.assertEqual(names.safe_component("第1讲 绪论. ", "x"), "第1讲 绪论")
        self.assertEqual(names.safe_component("  .第1讲.", "x"), "第1讲")

    def test_reserved_device_names_are_escaped_on_windows(self) -> None:
        got = names.safe_component("CON", "x")
        if platforms.IS_WINDOWS:
            self.assertEqual(got, "_CON")
        else:
            self.assertEqual(got, "CON")
        got = names.safe_component("con.pdf", "x")
        self.assertTrue(got.startswith("_") if platforms.IS_WINDOWS else got == "con.pdf")

    def test_empty_result_uses_fallback(self) -> None:
        self.assertEqual(names.safe_component("///", "备用名"), "备用名")
        self.assertEqual(names.safe_component("", ""), "未命名")

    def test_long_name_is_truncated_without_breaking_utf8(self) -> None:
        got = names.safe_component("长" * 200, "")
        self.assertLessEqual(len(got.encode("utf-8")), names._MAX_BYTES)
        self.assertLessEqual(len(got), 51)
        # 截断后不能留下半个汉字造成的乱码
        self.assertNotIn("\ufffd", got)


class DirectoryRuleTest(unittest.TestCase):
    def test_xdg_data_home_wins(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": str(Path("tmp") / "data")}):
            self.assertEqual(
                platforms.data_root(), Path("tmp") / "data" / "yuketang-courseware"
            )

    def test_xdg_cache_home_wins(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(Path("tmp") / "cache")}):
            self.assertEqual(
                platforms.cache_root(), Path("tmp") / "cache" / "yuketang-courseware"
            )

    @unittest.skipUnless(platforms.IS_WINDOWS, "只在 Windows 上检查 APPDATA")
    def test_windows_defaults_are_under_appdata(self) -> None:
        env = {key: value for key, value in os.environ.items()}
        env.pop("XDG_DATA_HOME", None)
        env.pop("XDG_CACHE_HOME", None)
        env["APPDATA"] = str(Path("C:/Users/x/AppData/Roaming"))
        env["LOCALAPPDATA"] = str(Path("C:/Users/x/AppData/Local"))
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                platforms.data_root(),
                Path("C:/Users/x/AppData/Roaming/yuketang-courseware"),
            )
            self.assertEqual(
                platforms.cache_root(),
                Path("C:/Users/x/AppData/Local/yuketang-courseware/cache"),
            )
            self.assertEqual(
                platforms.browser_profile(),
                Path("C:/Users/x/AppData/Roaming/yuketang-courseware/browser"),
            )


@unittest.skipUnless(platforms.IS_WINDOWS, "浏览器路径只在 Windows 上检查")
class BrowserCandidateTest(unittest.TestCase):
    def test_edge_and_chrome_are_both_considered(self) -> None:
        from yuketang_courseware import browser

        candidates = [candidate.lower() for candidate in browser._browser_candidates()]
        self.assertTrue(any(path.endswith("msedge.exe") for path in candidates))
        self.assertTrue(any(path.endswith("chrome.exe") for path in candidates))

    def test_found_browser_exists(self) -> None:
        from yuketang_courseware import browser

        found = browser.find_browser()
        if found is not None:
            self.assertTrue(os.path.isfile(found), found)


@unittest.skipUnless(platforms.IS_WINDOWS, "只在 Windows 上检查浏览器启动参数")
class BrowserLaunchTest(unittest.TestCase):
    def test_launch_uses_own_profile_without_console_window(self) -> None:
        import tempfile
        from unittest import mock

        from yuketang_courseware import browser as browser_mod

        with tempfile.TemporaryDirectory() as tmp:
            api = browser_mod.CoursewareBrowser(data_dir=Path(tmp) / "profile")
            calls: dict = {}

            class FakeProc:
                def poll(self):
                    return None

            def fake_popen(cmd, **kwargs):
                calls["cmd"] = cmd
                calls["kwargs"] = kwargs
                return FakeProc()

            with mock.patch.object(
                browser_mod.CoursewareBrowser, "_discover_port", return_value=None
            ), mock.patch.object(
                browser_mod, "_profile_browser_pids", return_value=[]
            ), mock.patch.object(
                browser_mod, "find_browser", return_value=r"C:\Edge\msedge.exe"
            ), mock.patch.object(
                browser_mod.subprocess, "Popen", side_effect=fake_popen
            ), mock.patch.object(
                browser_mod.CoursewareBrowser, "_wait_until_up", lambda _self: None
            ), mock.patch.dict(
                "os.environ", {"YUKETANG_BROWSER_FLAGS": "--headless=new --no-sandbox"}
            ):
                api._ensure_running("https://x.yuketang.cn/v2/web/index")

        self.assertEqual(calls["kwargs"]["creationflags"], browser_mod._NO_WINDOW)
        self.assertEqual(calls["kwargs"]["cwd"], str(api.data_dir))
        argv = calls["cmd"]
        self.assertTrue(any(arg.startswith("--user-data-dir=") for arg in argv), argv)
        self.assertTrue(any(arg.startswith("--remote-debugging-port=") for arg in argv), argv)
        self.assertIn("https://x.yuketang.cn/v2/web/index", argv)
        # 排查用的附加参数要原样带上，且排在网址前面
        self.assertIn("--no-sandbox", argv)
        self.assertLess(argv.index("--no-sandbox"), argv.index("https://x.yuketang.cn/v2/web/index"))

    def test_process_scan_passes_profile_through_environment(self) -> None:
        """环境变量没传进去时 $p 为空，Contains('') 会匹配所有 Edge —— 真机踩过。

        这是 PowerShell 后备实现的测试；主路径现在用 psutil。
        """
        from unittest import mock

        from yuketang_courseware import browser as browser_mod

        marker = r"C:\Users\x\AppData\Roaming\yuketang-courseware\browser"

        class Completed:
            stdout = b"111|9222\n222|\nnot-a-pid|x\n"

        with mock.patch.object(
            browser_mod, "_run_hidden", return_value=Completed()
        ) as run:
            found = browser_mod._powershell_browser_processes(Path(marker))

        self.assertEqual(found, [(111, 9222), (222, None)])
        env = run.call_args.kwargs["env"]
        self.assertEqual(env["YKT_PROFILE"], marker)
        script = run.call_args.args[0][-1]
        self.assertIn("if (-not $p) { return }", script)

    def test_psutil_scan_matches_profile_and_port(self) -> None:
        from types import SimpleNamespace
        from unittest import mock

        from yuketang_courseware import browser as browser_mod

        marker = r"C:\Users\x\AppData\Roaming\yuketang-courseware\browser"
        procs = [
            SimpleNamespace(
                info={
                    "pid": 111,
                    "name": "msedge.exe",
                    "cmdline": [
                        r"C:\Edge\msedge.exe",
                        f"--user-data-dir={marker}",
                        "--remote-debugging-port=9222",
                    ],
                }
            ),
            SimpleNamespace(
                info={
                    "pid": 222,
                    "name": "msedge.exe",
                    "cmdline": [r"C:\Edge\msedge.exe", "--type=renderer", "about:blank"],
                }
            ),
            SimpleNamespace(
                info={
                    "pid": 333,
                    "name": "chrome.exe",
                    "cmdline": [r"C:\Chrome\chrome.exe", r"--user-data-dir=C:\Other\profile"],
                }
            ),
        ]
        with mock.patch("psutil.process_iter", return_value=procs):
            found = browser_mod._psutil_browser_processes(Path(marker))

        self.assertEqual(found, [(111, 9222)])

    def test_stale_profile_is_restarted_instead_of_blocking(self) -> None:
        import tempfile
        from unittest import mock

        from yuketang_courseware import browser as browser_mod

        with tempfile.TemporaryDirectory() as tmp:
            api = browser_mod.CoursewareBrowser(data_dir=Path(tmp) / "profile")
            calls = {"terminated": 0}

            class FakeProc:
                def poll(self):
                    return None

            def fake_terminate(_self):
                calls["terminated"] += 1

            with mock.patch.object(
                browser_mod.CoursewareBrowser, "_discover_port", return_value=None
            ), mock.patch.object(
                browser_mod,
                "_profile_browser_pids",
                side_effect=lambda _dir: [4321] if not calls["terminated"] else [],
            ), mock.patch.object(
                browser_mod.CoursewareBrowser, "terminate_profile", fake_terminate
            ), mock.patch.object(
                browser_mod, "find_browser", return_value=r"C:\Edge\msedge.exe"
            ), mock.patch.object(
                browser_mod.subprocess, "Popen", return_value=FakeProc()
            ), mock.patch.object(
                browser_mod.CoursewareBrowser, "_wait_until_up", lambda _self: None
            ):
                spawned = api._ensure_running("https://x.yuketang.cn/v2/web/index")

        self.assertTrue(spawned)
        self.assertEqual(calls["terminated"], 1)

    def test_port_file_is_read_back(self) -> None:
        import tempfile
        from unittest import mock

        from yuketang_courseware import browser as browser_mod

        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp)
            (profile / "DevToolsActivePort").write_text(
                "51234\n/devtools/browser/abc\n", encoding="utf-8"
            )
            api = browser_mod.CoursewareBrowser(data_dir=profile)
            with mock.patch.object(
                browser_mod.CoursewareBrowser,
                "_version_ok",
                side_effect=lambda port: port == 51234,
            ):
                self.assertEqual(api._discover_port(), 51234)

    def test_terminate_profile_kills_whole_tree(self) -> None:
        from unittest import mock

        from yuketang_courseware import browser as browser_mod

        api = browser_mod.CoursewareBrowser(data_dir=Path("C:/tmp/ykt-profile"))
        with mock.patch.object(
            browser_mod, "_profile_browser_pids", return_value=[4321]
        ), mock.patch.object(browser_mod, "_run_hidden") as run:
            api.terminate_profile()
        run.assert_called_once()
        command = run.call_args[0][0]
        self.assertEqual(command[:2], ["taskkill", "/PID"])
        self.assertIn("4321", command)
        self.assertIn("/T", command)


if __name__ == "__main__":
    unittest.main()

