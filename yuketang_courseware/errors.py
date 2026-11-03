"""界面可直接显示的错误。构造时会抹掉网址和 token。"""

from __future__ import annotations

from yuketang_courseware.names import redact


class CoursewareError(Exception):
    def __init__(self, message: str = "") -> None:
        super().__init__(redact(message))


class BrowserMissing(CoursewareError):
    pass


class BrowserError(CoursewareError):
    pass


class LoginRequired(CoursewareError):
    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "请先在弹出的浏览器里扫码或账号登录，登完再点「读取课程」。"
        )


class ApiError(CoursewareError):
    pass
