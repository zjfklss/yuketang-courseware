"""网址、文件名和对外文字。不保存学校或个人身份。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')
_SECRET = re.compile(
    r"https?://\S+"
    r"|token=[^\s&\"']+"
    r"|csrftoken=[^\s&\"']+"
    r"|X-CSRFToken['\"]?\s*[:=]\s*['\"]?[^\s'\",}]+"
    r"|Cookie:\s*\S+",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """抹掉可能出现在异常里的网址和凭据，避免写进界面或终端。"""
    return _SECRET.sub("[已隐藏]", str(text or ""))


def safe_component(name: str, fallback: str) -> str:
    """去掉文件名非法字符。空结果用 fallback，仍空则用「未命名」。"""
    text = _clean(name)
    if not text:
        text = _clean(fallback)
    if not text:
        text = "未命名"
    raw = text.encode("utf-8")
    if len(raw) > 150:
        text = raw[:150].decode("utf-8", errors="ignore").rstrip().strip(".") or "未命名"
    return text


def _clean(name: str) -> str:
    text = _ILLEGAL.sub("", str(name or ""))
    text = re.sub(r"\s+", " ", text).strip().strip(".")
    if text in ("", ".", ".."):
        return ""
    return text


@dataclass(frozen=True)
class CourseUrl:
    origin: str
    host: str
    url: str
    classroom_id: str | None
    university_id: str | None


def parse_course_url(text: str) -> CourseUrl:
    """接受课堂页，或只有域名的课程首页。学校 id、课堂 id 只从查询参数读。"""
    raw = (text or "").strip()
    if not raw:
        raise ValueError("请填写雨课堂网址")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in ("http", "https"):
        raise ValueError("请填写 http 或 https 网址")
    if not host or not (host == "yuketang.cn" or host.endswith(".yuketang.cn")):
        raise ValueError("请填写雨课堂网址（域名以 yuketang.cn 结尾）")
    port = ""
    if parsed.port and parsed.port not in (80, 443):
        port = f":{parsed.port}"
    origin = f"https://{host}{port}"
    path = parsed.path or "/"
    url = origin + path
    if parsed.query:
        url += "?" + parsed.query
    query = {}
    for key, values in _pairs(parsed.query):
        query.setdefault(key, values)
    return CourseUrl(
        origin=origin,
        host=host,
        url=url,
        classroom_id=_first(query, "classroom_id"),
        university_id=_first(query, "university_id"),
    )


def _pairs(query: str):
    from urllib.parse import parse_qs

    return parse_qs(query, keep_blank_values=False).items()


def _first(query: dict, key: str) -> str | None:
    values = query.get(key) or []
    if not values:
        return None
    text = str(values[0]).strip()
    return text or None


def resolve_directory(text: str, base: "Path | None" = None) -> "Path":
    """保存目录：展开 ~，相对路径按基准目录（默认当前目录）解析。

    各人习惯不同——有人写 `~/课件`，有人写 `课件`，有人直接给绝对路径。
    相对路径一律按当前目录算，绝不偷偷落到家目录去。
    """
    from pathlib import Path

    raw = (text or "").strip() or "."
    path = Path(raw).expanduser()
    if not path.is_absolute():
        root = Path(base) if base is not None else Path.cwd()
        path = root / path
    return path


def api_path(path: str, **query: object) -> str:
    from urllib.parse import urlencode

    items = [(key, str(value)) for key, value in query.items() if value is not None]
    if not items:
        return path
    return path + "?" + urlencode(items)


def term_sort_key(term: str) -> tuple:
    """学期大的在前。`2026-1`、`2026`、`2025-2026-1` 都按里面的数字比，纯文字放最后。"""
    numbers = [int(part) for part in re.findall(r"\d+", str(term or ""))]
    if numbers:
        return (0, tuple(-value for value in numbers), str(term or ""))
    return (1, str(term or ""))
