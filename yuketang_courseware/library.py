"""下载目录的扫描、匹配与整理。

只做三件事：
1. 递归扫描目录里已有的 PDF（不只看目标路径）；
2. 把文件名和讲次名对上（忽略空格、标点、全半角，容忍 `标题-1.pdf` 这种多套命名）；
3. 把对得上的文件挪进 `目录/课程名/讲次名.pdf` 的标准位置。

整理只搬不删：目标已存在就跳过，遇到重名冲突只报告。程序从不删除用户文件。
"""

from __future__ import annotations

import difflib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from yuketang_courseware.names import safe_component
from yuketang_courseware.pdf import count_pages

_MAX_FILES = 5000
_MIN_FILE_SIZE = 256          # 比这还小的「PDF」多半是空壳或坏文件
# 这些目录里的东西不是「该归位的课件」：归档、回收站、字节码缓存。
# 扫进来只会把几十个讲次之外的旧文件当成「没认出来」，还会把 _archive 里
# 早就作废的旧课件当成「另一版本」搬到课件目录里复活一遍。
_SKIP_DIRS = frozenset({"_archive", "_归档", ".trash", "__pycache__"})
_PUNCT = re.compile(
    r"[\s\u3000\-_—–－·・、,，.。:：;；!！?？'\"“”‘’()（）\[\]【】{}｛｝<>《》<>/\\|+*~`&＆#]+"
)
_DIGIT_TAIL = re.compile(r"(\d)$")
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000}
_CN_RUN = re.compile(r"[零〇一二两三四五六七八九十百千]+")


@dataclass(frozen=True)
class FoundFile:
    path: Path
    relative: str
    name: str
    key: str
    size: int
    pages: int | None = None
    mtime: float = 0.0

    @property
    def folder(self) -> str:
        # relative 一律是 `/` 分隔（见 scan），这里别再让 Windows 转回 `\\`
        head, sep, _tail = self.relative.replace("\\", "/").rpartition("/")
        return head if sep else ""


@dataclass
class Move:
    source: FoundFile
    target: Path
    course: str
    lesson: str
    reason: str = ""


@dataclass
class Conflict:
    """同名但内容不同的两份。`other` 是另一份的出处，用来说明页数差在哪。"""

    source: FoundFile
    target: Path
    other: FoundFile | None = None

    def note(self) -> str:
        return conflict_note(self.source, self.target, self.other)


@dataclass
class TidyPlan:
    moves: list[Move] = field(default_factory=list)
    kept: list[FoundFile] = field(default_factory=list)
    conflicts: list[Conflict] = field(default_factory=list)
    duplicates: list[tuple[FoundFile, FoundFile]] = field(default_factory=list)
    unmatched: list[FoundFile] = field(default_factory=list)   # 没认出来的，一律不动
    elsewhere: int = 0      # 在笔记/习题这类非课件目录里、这次不归它管的文件数


def normalize(text: str) -> str:
    """比对文件名和讲次名用的归一化。

    各人起名习惯差得很远，这几步都是为了提高「同一讲」的命中率：
      1. 全半角统一（`１２`、`（`、`Ａ` 都折成半角）；
      2. 去掉扩展名、空白、标点；
      3. 中文数字折成阿拉伯数字：`第十二讲` 与 `第12讲`、`第二章` 与 `第2章` 同一讲；
      4. 大小写不敏感。
    """
    import unicodedata

    stem = unicodedata.normalize("NFKC", str(text or ""))
    lowered = stem.lower()
    for suffix in (".pdf", ".pptx", ".ppt"):
        if lowered.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    stem = _CN_RUN.sub(lambda found: _cn_number(found.group(0)), stem)
    return _PUNCT.sub("", stem).lower()


def _cn_number(text: str) -> str:
    """「十二」→12、「一百零三」→103、「二〇二六」→2026。读不通就原样返回。"""
    if not any(ch in _CN_UNIT for ch in text):
        if all(ch in _CN_DIGIT for ch in text):
            return "".join(str(_CN_DIGIT[ch]) for ch in text)
        return text
    total = 0
    current = 0
    for ch in text:
        if ch in _CN_DIGIT:
            current = _CN_DIGIT[ch]
            continue
        unit = _CN_UNIT.get(ch)
        if unit is None:
            return text
        total += (current or 1) * unit
        current = 0
    return str(total + current)


def lesson_keys(title: str) -> list[str]:
    """一讲可能对应 `标题.pdf`，也可能对应 `标题-1.pdf`、`标题-2.pdf`。"""
    base = normalize(title)
    if not base:
        return []
    return [base] + [f"{base}{digit}" for digit in range(1, 10)]


def scan(
    root: Path, limit: int = _MAX_FILES, stats: dict | None = None
) -> list[FoundFile]:
    """递归找出 root 下所有 PDF。超过 limit 就停，避免扫整个家目录。

    stats 传入时会写回 scanned / truncated：文件多到扫不完时界面要说清楚，
    不能假装全都认过了（那样会把没扫到的讲次误判成「没有，得重新下」）。
    """
    root = Path(root)
    if stats is not None:
        stats["scanned"] = 0
        stats["truncated"] = False
    # Path.is_dir() 只吞掉 ENOENT/ENOTDIR/EACCES 里的一部分 errno，
    # 其他 OSError（比如挂载点掉线、权限位异常）会直接抛出来。
    # Python 3.13 起放宽了这个行为，所以这种崩溃在老解释器上才会出现。
    try:
        if not root.is_dir():
            return []
    except OSError:
        return []
    found: list[FoundFile] = []
    for base, dirs, files in os.walk(root, followlinks=False, onerror=None):
        dirs[:] = [
            name
            for name in dirs
            if not name.startswith(".") and name.lower() not in _SKIP_DIRS
        ]
        for name in files:
            if not name.lower().endswith(".pdf"):
                continue
            path = Path(base) / name
            try:
                stat = path.stat()
            except OSError:      # 权限不足、文件刚被删掉、坏符号链接
                continue
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            if stat.st_size < _MIN_FILE_SIZE:
                continue
            try:
                # 统一用 `/`：界面、整理计划、测试都按同一套写法比对，
                # Windows 的 `\\` 只留在真正的 Path 上。
                relative = path.relative_to(root).as_posix()
            except ValueError:
                relative = name
            found.append(
                FoundFile(
                    path=path,
                    relative=relative,
                    name=name,
                    key=normalize(name),
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
            )
            if len(found) >= limit:
                if stats is not None:
                    stats["scanned"] = len(found)
                    stats["truncated"] = True
                return found
    found.sort(key=lambda item: (-item.mtime, item.relative))
    if stats is not None:
        stats["scanned"] = len(found)
    return found


_PAGE_CACHE: dict[str, int | None] = {}
_SHA_CACHE: dict[tuple[str, int, float], str] = {}
_RECORD_TTL = 365 * 24 * 3600


def sha256(path: Path) -> str | None:
    """内容指纹。文件没变（大小、mtime 相同）就复用上次结果。"""
    import hashlib

    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path), stat.st_size, stat.st_mtime)
    if key in _SHA_CACHE:
        return _SHA_CACHE[key]
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    value = digest.hexdigest()
    _SHA_CACHE[key] = value
    return value


def _records_path() -> Path:
    from yuketang_courseware import platforms

    return platforms.cache_root() / "downloaded.json"


def load_records() -> dict[str, dict]:
    """本程序下过的文件：sha256 → {course, lesson, pages, saved}。

    有了它，同一份文件被改名、挪目录后仍然认得出来。
    """
    try:
        data = json.loads(_records_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    now = time.time()
    return {
        key: value
        for key, value in data.items()
        if isinstance(value, dict) and now - float(value.get("saved") or 0) < _RECORD_TTL
    }


def record_download(path: Path, course: str, lesson: str, pages: int | None) -> None:
    digest = sha256(path)
    if not digest:
        return
    try:
        size = Path(path).stat().st_size
    except OSError:
        size = 0
    records = load_records()
    records[digest] = {
        "course": course,
        "lesson": lesson,
        "pages": pages,
        "size": size,          # 先按大小粗筛，省掉大量无谓的哈希
        "saved": time.time(),
    }
    try:
        target = _records_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(".part")
        part.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        os.replace(part, target)
    except OSError:
        pass


def page_count(path: Path) -> int | None:
    """读 PDF 页数（带进程内缓存），用来做内容核对。

    先读对象头里的 /Count；遇到压缩对象流（老工具导出、PDF 1.5+）读不到时，
    退回用系统的 `pdfinfo`（poppler-utils，没有就跳过）。
    """
    key = str(path)
    if key in _PAGE_CACHE:
        return _PAGE_CACHE[key]
    pages = count_pages(Path(path))
    if pages is None:
        pages = _pages_via_pdfinfo(Path(path))
    _PAGE_CACHE[key] = pages
    return pages


def _pages_via_pdfinfo(path: Path) -> int | None:
    import shutil
    import subprocess

    exe = shutil.which("pdfinfo")
    if not exe:
        return None
    try:
        completed = subprocess.run(
            [exe, str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
            env={**os.environ, "LC_ALL": "C", "LANG": "C"},
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError:
                return None
    return None


def similar_names(files: list[FoundFile], title: str, threshold: float = 0.85, limit: int = 3):
    """名称相近的已有文件。名称只做初筛，页数还要另外核对。"""
    base = normalize(title)
    if not base:
        return []
    scored = []
    for item in files:
        if not item.key:
            continue
        ratio = difflib.SequenceMatcher(None, base, item.key).ratio()
        if ratio >= threshold:
            scored.append((ratio, item))
    scored.sort(key=lambda pair: -pair[0])
    return [item for _ratio, item in scored[:limit]]


# 保存结构：<保存目录>/<课程名>/课件/<讲次>.pdf
SUB_FOLDER = "课件"
_MIN_PLAUSIBLE_PAGES = 3


def is_suspect(found: FoundFile) -> bool:
    """怀疑文件不完整：页数小于 3（下一半、被截断的典型样子）。

    只是怀疑：这种文件不当「已下载」，会重新下一遍；下完页数对得上就算数。
    本程序自己下出来的（指纹在记录里）不怀疑——那是我们写完整才落盘的。
    """
    pages = page_count(found.path)
    if pages is None:
        return False
    return pages < _MIN_PLAUSIBLE_PAGES


def course_dir(root: Path, course_folder: str) -> Path:
    return Path(root) / course_folder / SUB_FOLDER


def _too_few_pages(found: FoundFile) -> bool:
    """页数少到不像一份完整课件（没下完、拷到一半的残片）。

    这种文件不能当「同一讲的另一个版本」另存：另存进去就是多一个坏文件。
    """
    pages = page_count(found.path)
    return pages is not None and pages < _MIN_PLAUSIBLE_PAGES


class Library:
    """保存目录的现状：扫到的文件、按讲次的匹配表、页数缓存。

    各人的目录习惯差别很大：有人按课程分文件夹、有人一把全丢在根目录、有人自己
    改过名、有人用别的工具下过。所以识别分三层，越往后越贵、也越保守：
      1. 名字（忽略空格标点全半角，认「标题-1」这种多套命名）—— 最便宜；
      2. 内容指纹（只对「大小对得上」的文件算 sha256）—— 改名、挪位置也认；
      3. 页数核对（名字相近 + 页数一致）—— 只在下载前用，且有歧义就不认。
    绝不因为「猜可能已经有了」而漏掉一讲；拿不准就重新下。
    """

    def __init__(self, root: Path, files: list[FoundFile] | None = None) -> None:
        self.root = Path(root)
        self.stats: dict = {}
        if files is None:
            self.files = scan(self.root, stats=self.stats)
        else:
            self.files = files
        self.index = build_index(self.files)
        self.map: dict[tuple[str, str], FoundFile] = {}
        self.claims: set[Path] = set()
        self.records: dict[str, dict] = load_records()
        self._by_size: dict[int, list[FoundFile]] = {}
        for item in self.files:
            self._by_size.setdefault(item.size, []).append(item)

    def bind(self, courses) -> "Library":
        """把讲次和文件对上号：先按名字，再按内容指纹（下过的文件改名也认）。"""
        self.map = build_map(courses, self.files)
        self.claims = {item.path for item in self.map.values()}
        if not self.records:
            return self
        # 指纹只给「大小也对得上」的文件算：目录下几千个 PDF 时，全量哈希会卡死界面
        by_title: dict[str, list[tuple[str, dict]]] = {}
        for digest, meta in self.records.items():
            if not isinstance(meta, dict):
                continue
            title = str(meta.get("lesson") or "")
            if title:
                by_title.setdefault(title, []).append((digest, meta))
        for course in courses:
            for lesson in course.lessons:
                if not lesson.selectable:
                    continue
                key = (course.classroom_id, lesson.title)
                if key in self.map:
                    continue
                found = self._by_record(lesson.title, course.course_name)
                if found is None:
                    continue
                self.map[key] = found
                self.claims.add(found.path)
        return self

    def _by_record(self, lesson_title: str, course_name: str) -> FoundFile | None:
        """按内容指纹找回自己下过的文件。只对大小相同的候选算 sha256。"""
        wanted: list[tuple[str, int]] = []
        for digest, meta in self.records.items():
            if not isinstance(meta, dict):
                continue
            if str(meta.get("lesson") or "") != lesson_title:
                continue
            if meta.get("course") not in (None, "", course_name):
                continue
            size = meta.get("size")
            # 老版本写的记录没有 size，没法粗筛，只能跳过（重新下一次就有了）
            if not isinstance(size, int) or size <= 0:
                continue
            wanted.append((digest, size))
        for digest, size in wanted:
            for item in self._by_size.get(size, []):
                if item.path in self.claims:
                    continue
                if sha256(item.path) == digest:
                    return item
        return None

    def trusted(self, found: FoundFile) -> bool:
        """这个文件是我们自己写完整的（指纹在记录里），不必再怀疑它不完整。"""
        digest = sha256(found.path)
        return bool(digest) and digest in self.records

    def matched(self, course, lesson) -> FoundFile | None:
        return self.map.get((course.classroom_id, lesson.title))

    def matched_ok(self, course, lesson) -> FoundFile | None:
        """只认「看起来完整」的已有文件；坏文件不当作已下载。"""
        found = self.matched(course, lesson)
        if found is None:
            return None
        if is_suspect(found) and not self.trusted(found):
            return None
        return found

    def suspect(self, courses) -> list[tuple[str, FoundFile]]:
        """当前讲次里那些「对得上但看起来不完整」的文件。"""
        out: list[tuple[str, FoundFile]] = []
        for course in courses:
            for lesson in course.lessons:
                found = self.matched(course, lesson)
                if found is None or not is_suspect(found):
                    continue
                if self.trusted(found):   # 自己下出来的，页数少也是真的少
                    continue
                out.append((lesson.title, found))
        return out

    def near(self, title: str, limit: int = 3) -> list[FoundFile]:
        """名字相近、还没被别的讲认领的已有文件。"""
        out: list[FoundFile] = []
        for item in similar_names(self.files, title, limit=limit * 3):
            if item.path in self.claims:
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def content_match(self, course, lesson, expected_pages: int) -> FoundFile | None:
        """名字相近且页数一致 → 认定同一讲，不再重复下载。

        拿不准就返回 None（宁可重下一次，也不漏内容）：
        - 页数读不出来，或页数太少不足以作证；
        - 有多个候选都符合（可能真是两份不同的课件）；
        - 候选文件已经被别的讲认领。
        """
        if expected_pages < _MIN_PLAUSIBLE_PAGES:
            return None
        hits = [
            item
            for item in self.near(lesson.title)
            if self.same_content(item, expected_pages)
        ]
        if not hits:
            return None
        if len(hits) > 1:
            # 多个都符合时，只信课程目录里那一份；还是多个就不敢认
            folder = normalize(course.folder or course.course_name)
            if folder:
                inside = [item for item in hits if folder in normalize(item.folder)]
                if len(inside) == 1:
                    hits = inside
        if len(hits) != 1:
            return None
        self.claims.add(hits[0].path)
        return hits[0]

    @staticmethod
    def pages_of(found: FoundFile) -> int | None:
        return page_count(found.path)

    def same_content(self, found: FoundFile, expected_pages: int) -> bool:
        """页数对上就算同一份内容。页数读不出来时不作判断。

        文件大小不拿来比对：平台原图每次压缩结果都可能差几个字节，同一个课件
        用别的工具再下一次，大小就不一样了。
        """
        pages = page_count(found.path)
        return pages is not None and pages == expected_pages


def build_index(files: list[FoundFile]) -> dict[str, list[FoundFile]]:
    index: dict[str, list[FoundFile]] = {}
    for item in files:
        index.setdefault(item.key, []).append(item)
    return index


def build_map(courses, files: list[FoundFile]) -> dict[tuple[str, str], FoundFile]:
    """把「讲次」配到「已有文件」。返回 {(classroom_id, 讲次标题): 文件}。

    分两轮：先按名字精确对；剩下的再容忍我们自己的多套命名（`标题-1.pdf`）。
    每个文件只能被一讲认领，且只有没有别的讲次精确同名时才按多套命名认领。
    """
    index = build_index(files)
    lessons = [
        (course, lesson)
        for course in courses
        for lesson in course.lessons
        if lesson.selectable
    ]
    exact_titles = {normalize(lesson.title) for _course, lesson in lessons}
    claimed: set[Path] = set()
    result: dict[tuple[str, str], FoundFile] = {}

    def pick(course, candidates: list[FoundFile]) -> FoundFile | None:
        """同名的有多份时怎么挑。

        内容一样（页数、大小都相同）→ 取最新的一份；
        内容不一样 → 只认课程目录里那份，认不出就返回 None，留给下载时按页数核对。
        """
        if not candidates:
            return None
        if len(candidates) == 1:
            return candidates[0]
        pages = {page_count(item.path) for item in candidates}
        sizes = {item.size for item in candidates}
        if len(pages) == 1 and len(sizes) == 1:
            return max(candidates, key=lambda item: item.mtime)
        folder = normalize(course.folder or course.course_name)
        inside = [item for item in candidates if folder and folder in normalize(item.folder)]
        if len(inside) == 1:
            return inside[0]
        return None

    for course, lesson in lessons:
        base = normalize(lesson.title)
        if not base:
            continue
        candidates = [item for item in index.get(base, []) if item.path not in claimed]
        chosen = pick(course, candidates)
        if chosen is None:
            continue
        result[(course.classroom_id, lesson.title)] = chosen
        claimed.add(chosen.path)

    for course, lesson in lessons:
        key = (course.classroom_id, lesson.title)
        if key in result:
            continue
        base = normalize(lesson.title)
        if not base:
            continue
        for number in range(1, 10):
            suffix_key = f"{base}{number}"
            if suffix_key in exact_titles:  # 这个文件名其实属于另一讲
                continue
            candidates = [item for item in index.get(suffix_key, []) if item.path not in claimed]
            chosen = pick(course, candidates)
            if chosen is None:
                continue
            result[key] = chosen
            claimed.add(chosen.path)
            break
    return result


def render_tree(
    root: Path, folders: list[str] | None = None, max_lines: int = 60
) -> str:
    """把 root 下的 PDF 按文件夹画成一棵树，告诉用户文件到底放哪了。

    传 folders 时只画这些课程文件夹，避免把家目录里别的 PDF 也列出来。
    """
    root = Path(root)
    files = scan(root)
    if folders is not None:
        keep = tuple(str(name) for name in folders if name)
        files = [
            item
            for item in files
            if any(item.folder == name or item.folder.startswith(name + "/") for name in keep)
        ]
    if not files:
        return f"{root} 里还没有 PDF"
    groups: dict[str, list[FoundFile]] = {}
    for item in files:
        groups.setdefault(item.folder or "（就在根目录）", []).append(item)
    lines = [str(root)]
    folders = sorted(groups)
    for index, folder in enumerate(folders):
        last_folder = index == len(folders) - 1
        lines.append(("└── " if last_folder else "├── ") + folder + "/")
        items = sorted(groups[folder], key=lambda item: item.name)
        for position, item in enumerate(items):
            if len(lines) >= max_lines:
                lines.append("    …（还有更多，未列出）")
                return "\n".join(lines)
            last_item = position == len(items) - 1
            prefix = "    " if last_folder else "│   "
            lines.append(prefix + ("└── " if last_item else "├── ") + item.name)
    return "\n".join(lines)


def find_existing(
    index: dict[str, list[FoundFile]], title: str, folder: str = ""
) -> FoundFile | None:
    """按文件名找同一讲。优先本课程目录里的，其次别处。"""
    keys = lesson_keys(title)
    hits: list[FoundFile] = []
    for key in keys:
        hits.extend(index.get(key, []))
    if not hits:
        return None
    want = normalize(folder)

    def score(item: FoundFile) -> tuple:
        inside = 1 if want and want in normalize(item.folder) else 0
        return (-inside, -item.mtime)

    hits.sort(key=score)
    return hits[0]


def _top_folder(item: FoundFile) -> str:
    """文件所在的顶层目录名（`自控/课件/a.pdf` → `自控`）。在根目录下就是空串。"""
    folder = item.folder
    return folder.split("/", 1)[0] if folder else ""


def infer_homes(
    root: Path, courses, mapping: dict[tuple[str, str], FoundFile], files: list[FoundFile]
) -> dict[str, tuple[str, str]]:
    """推断每门课该放进哪个目录。

    各人的目录习惯不一样：有人按平台课程名建目录，有人自己简写成「自控」「计网」，
    有人根本不建子目录。硬按平台名新建，会在同一个保存目录里出现「自动控制原理」
    和「自控」两套并列。所以先看这门课已有的文件都躺在哪儿，多数落在同一个顶层
    目录里就跟着它走。

    返回 {classroom_id: (顶层目录名, 子目录名)}，子目录名可能为空串（直接放课程目录下）。
    """
    from collections import Counter

    counts: dict[str, Counter] = {}
    for course in courses:
        counter: Counter = Counter()
        for lesson in course.lessons:
            if not lesson.selectable:
                continue
            found = mapping.get((course.classroom_id, lesson.title))
            if found is None:
                continue
            top = _top_folder(found)
            if top:
                counter[top] += 1
        counts[course.classroom_id] = counter

    homes: dict[str, tuple[str, str]] = {}
    taken: dict[str, str] = {}
    for course in courses:
        counter = counts.get(course.classroom_id) or Counter()
        total = sum(counter.values())
        base = ""
        if total:
            top, count = counter.most_common(1)[0]
            # 至少两讲、且过半都在同一个顶层目录里，才认为那是这门课的地盘
            if count >= 2 and count * 2 >= total and top not in taken:
                base = top
        if not base:
            base = course.folder or safe_component(course.course_name, course.classroom_id)
        base = _unique_folder(base, taken, course)
        taken[base] = course.classroom_id

        if (root / base / SUB_FOLDER).is_dir():
            sub = SUB_FOLDER
        elif any(item.folder == base for item in files):
            sub = ""              # 用户习惯直接放在课程目录下，就别硬加一层「课件」
        else:
            # 用户自己给这门课起了个存放子目录（`自动控制/自动控制课件/`）：
            # 沿用它，别在旁边再建一个空的「课件/」把人家的目录搬空。
            sub = _adopted_subfolder(root, base, mapping, course) or SUB_FOLDER
        homes[course.classroom_id] = (base, sub)
    return homes


def _adopted_subfolder(root: Path, base: str, mapping, course) -> str:
    """这门课已有的文件全在 base 的同一个子目录里时，用那个子目录名。

    只认「对得上讲次的文件」都在同一个子目录的情况：如果散在好几个子目录
    （例如 `笔记/`、`习题/` 里也有同名 PDF），那还是按标准结构走。
    """
    subs: set[str] = set()
    for lesson in course.lessons:
        found = mapping.get((course.classroom_id, lesson.title))
        if found is None:
            continue
        if found.folder == base:
            return ""                       # 根上就有，说明用户是平铺的
        if not found.folder.startswith(base + "/"):
            continue
        subs.add(found.folder[len(base) + 1:].split("/", 1)[0])
    if len(subs) != 1:
        return ""
    name = next(iter(subs))
    if name in _SKIP_DIRS or name.startswith("."):
        return ""
    return name if (root / base / name).is_dir() else ""


def _unique_folder(base: str, taken: dict[str, str], course) -> str:
    """两门课抢同一个目录名时，后一门加班级/课堂号，别混在一起。"""
    if base not in taken or taken[base] == course.classroom_id:
        return base
    extra = safe_component(f"{base}（{course.class_name}）", course.classroom_id)
    if extra not in taken:
        return extra
    return safe_component(f"{base}-{course.classroom_id}", course.classroom_id)


def _owned_files(
    lesson,
    base_key: str,
    files: list[FoundFile],
    claimed: set[Path],
    foreign_keys: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[FoundFile], list[FoundFile]]:
    """这一讲名下的文件，分成（各套幻灯片，同名多余的）。

    第一份是 `标题.pdf`，后面依次是 `标题-1.pdf`、`标题-2.pdf`…… 同名同键的
    第二份起放进「多余」——那多半是同一讲的另一个版本（页数不同）或者重复下载，
    交给上层判断是报重复还是报冲突，绝不偷偷当成第二套搬走。

    `foreign_keys` 是本课程里**别的讲次**的名字：平台常有「第二章 基本数据结构
    及其运算」和「第二章 基本数据结构及其运算 （2）」两讲，归一化后前者正好对着
    后者的键加个数字。不排除掉，前者就会把后者的文件认成自己的「第 2 套」，
    两边都以为对方那份是「另一个版本」，于是每次整理都堆出一批副本。
    """
    groups: dict[str, list[FoundFile]] = {}
    for item in files:
        if item.path in claimed:
            continue
        key = item.key
        if key == base_key:
            groups.setdefault("", []).append(item)
            continue
        tail = key[len(base_key):] if key.startswith(base_key) else ""
        if tail.isdigit() and 1 <= int(tail) <= 99 and key not in foreign_keys:
            groups.setdefault(tail, []).append(item)
    ordered: list[FoundFile] = []
    extras: list[FoundFile] = []
    for label in sorted(groups, key=lambda name: (name != "", int(name or 0))):
        members = groups[label]
        ordered.append(members[0])
        extras.extend(members[1:])
    return ordered, extras


def plan_tidy(
    root: Path, courses, files: list[FoundFile] | None = None, on_conflict: str = "keep-both"
) -> TidyPlan:
    """算出「哪些文件该挪到哪」。不碰磁盘。

    三条原则：
    1. 跟着用户已有的目录走（`infer_homes`），不另起一个同义目录；
    2. 一讲可能有好几套幻灯片（`标题-1`、`标题-2`），全部归位，不要只搬一个；
    3. 目标已存在时，照 aria2 / Syncthing 的老规矩办——**绝不覆盖**：内容一样算
       重复（不动），内容不一样就把来的这份另存成「标题（另一版本 日期）.pdf」，
       两份都留在原地让人自己看；同名冲突最多留 10 份（Syncthing 的 maxConflicts
       也是 10），再多就只报告不动。
    """
    root = Path(root)
    if files is None:
        files = scan(root)
    plan = TidyPlan()
    lib = Library(root, files).bind(courses)
    mapping = lib.map
    homes = infer_homes(root, courses, mapping, files)
    claimed: set[Path] = set()
    used: set[Path] = set()
    planned_targets: set[Path] = set()
    vacated: set[Path] = set()      # 计划里马上要搬走的源文件，它占的名字算空的

    for course in courses:
        base, sub = homes.get(
            course.classroom_id,
            (course.folder or course.course_name, SUB_FOLDER),
        )
        for lesson in course.lessons:
            if not lesson.selectable:
                continue
            base_key = normalize(lesson.title)
            if not base_key:
                continue
            others = {
                normalize(other.title)
                for other in course.lessons
                if other is not lesson and other.selectable
            }
            owned, extras = _owned_files(
                lesson, base_key, files, claimed, others - {base_key}
            )
            if not owned:
                continue
            safe_title = _safe(lesson.title, lesson.courseware_id)
            first_target = (
                root / base / sub / f"{safe_title}.pdf"
                if sub
                else root / base / f"{safe_title}.pdf"
            )
            # 已经在标准位置的那份永远是主文件：不能把人家从标准名上改名搬走
            candidates = owned + extras
            main = next(
                (item for item in candidates if item.path == first_target), candidates[0]
            )
            for item in candidates:
                claimed.add(item.path)
                used.add(item.path)

            def place(item: FoundFile, target: Path, index: int) -> None:
                """把一份文件放到 target：已在位就留着，目标被占就按策略处理。"""
                if item.path == target:
                    plan.kept.append(item)
                    planned_targets.add(target)
                    return
                if item.path.parent == target.parent and (
                    target.exists() or target in planned_targets
                ):
                    # 已经躺在这门课的课件目录里了，只是文件名跟标准名不一样，而标准名
                    # 那份已经被别人占着。改名换不来好处，只会堆出「另一版本」副本
                    # （平台两讲名字只差个「（2）」，归一化后撞到同一个键就会这样）。
                    # 原地留着，只报告不动。
                    plan.kept.append(item)
                    planned_targets.add(item.path)
                    return
                if target in planned_targets:
                    return
                # 目标上现在有文件，但它可能正是这次要搬走的（比如 `标题-1.pdf` 要改
                # 叫 `标题.pdf`，紧接着第二套又要用 `标题-1.pdf`）——这种不算占位。
                occupied = target.exists() and target not in vacated
                if occupied:
                    holder = _as_found(root, target)
                    if _same_file(item, holder):
                        plan.duplicates.append((item, holder))
                        return
                    if _too_few_pages(item):
                        # 2 页的残片不是「另一个版本」：另存进去只会多一个坏文件，
                        # 报出来让人自己删。
                        plan.conflicts.append(Conflict(item, target, holder))
                        return
                    if on_conflict != "keep-both":
                        plan.conflicts.append(Conflict(item, target, holder))
                        return
                    parked = _conflict_name(target, item.mtime, planned_targets)
                    if parked is None:
                        plan.conflicts.append(Conflict(item, target, holder))
                        return
                    planned_targets.add(parked)
                    vacated.add(item.path)
                    plan.moves.append(
                        Move(
                            source=item,
                            target=parked,
                            course=course.course_name,
                            lesson=lesson.title,
                            reason="同名但内容不同，另存一份（不覆盖）",
                        )
                    )
                    return
                planned_targets.add(target)
                vacated.add(item.path)      # 这个源文件马上腾地方
                plan.moves.append(
                    Move(
                        source=item,
                        target=target,
                        course=course.course_name,
                        lesson=lesson.title,
                        reason=_why(item, base, sub, index, course),
                    )
                )

            # 已经在标准目录里、而且本来就是 `-1`、`-2` 连号的一套，别动它：
            # 改名不会带来任何好处，只会把用户的编号打乱一次。
            if not extras and _serial_in_place(owned, base_key, first_target.parent):
                for item in owned:
                    plan.kept.append(item)
                    planned_targets.add(item.path)
                continue

            place(main, first_target, 0)
            others = [item for item in candidates if item is not main]
            # 键不一样的是「第 N 套幻灯片」（`标题-1`、`标题-2`）；键一样的是同名文件，
            # 内容相同就是重复下载，不同就是同一讲的两个版本。
            decks = [item for item in others if item.key != main.key]
            same_key = [item for item in others if item.key == main.key]
            deck = 1
            for item in decks:
                name = f"{safe_title}-{deck}.pdf"
                target = root / base / sub / name if sub else root / base / name
                place(item, target, deck)
                deck += 1
            # 同名同键的其余文件：内容一样算重复；不一样就是同一讲的两个版本，
            # 开着 keep-both 时也归位，只是带上「（另一版本 日期）」，两份都留在目标
            # 目录里让人一眼看出哪份是后来的。关掉才只报告不动。
            for extra in same_key:
                if _same_file(extra, main):
                    plan.duplicates.append((extra, main))
                    continue
                if extra.path.parent == first_target.parent:
                    # 两份都在这门课的课件目录里、内容不同、名字只差个后缀：
                    # 原样留着就是「两个版本都在」，再另存一份纯属制造副本。
                    plan.kept.append(extra)
                    continue
                if on_conflict != "keep-both":
                    plan.conflicts.append(Conflict(extra, first_target, main))
                    continue
                if _too_few_pages(extra):
                    plan.conflicts.append(Conflict(extra, first_target, main))
                    continue
                parked = _conflict_name(first_target, extra.mtime, planned_targets)
                if parked is None:
                    plan.conflicts.append(Conflict(extra, first_target, main))
                    continue
                planned_targets.add(parked)
                vacated.add(extra.path)
                plan.moves.append(
                    Move(
                        source=extra,
                        target=parked,
                        course=course.course_name,
                        lesson=lesson.title,
                        reason="同一讲的另一个版本，另存一份（不覆盖）",
                    )
                )

    # 认不出来的文件：一律不动，但要如实报出来，别让用户以为「整理完了就都归位了」。
    # 只报「这次管的范围」里的：别的课程的文件夹、上一轮留下的「（另一版本 …）」都不算，
    # 否则一次只看一门课时会冒出上百条无关文件，把真正要紧的提示淹了。
    # 笔记、习题、提交、figs 这些子目录里的 PDF 也不是课件，只记个数，不逐条刷屏。
    home_dirs = {base for base, _sub in homes.values()}
    managed_dirs = set(home_dirs)
    for base, sub in homes.values():
        if sub:
            managed_dirs.add(f"{base}/{sub}")
    for item in files:
        if item.path in used or _is_conflict_copy(item.name):
            continue
        top = _top_folder(item)
        if top and top not in home_dirs:
            continue
        if item.folder in managed_dirs or item.folder == "":
            plan.unmatched.append(item)
        else:
            plan.elsewhere += 1
    return plan


def _serial_in_place(owned: list[FoundFile], base_key: str, folder: Path) -> bool:
    """这一讲的几套幻灯片是不是已经「在目标目录里、按 -1、-2 连号」放着。

    是的话就别改名了：用户自己的编号顺序比统一命名更值钱，改名只会把
    「第 1 套」变成没后缀的那份，排序还乱一次。
    """
    if not owned or any(item.key == base_key for item in owned):
        return False
    numbers: list[int] = []
    for item in owned:
        tail = item.key[len(base_key):] if item.key.startswith(base_key) else ""
        if not tail.isdigit() or item.path.parent != folder:
            return False
        numbers.append(int(tail))
    return sorted(numbers) == list(range(1, len(numbers) + 1))


def _is_conflict_copy(name: str) -> bool:
    return "（另一版本" in str(name or "")


_CONFLICT_CAP = 10        # 同一个目标最多留几份「另一版本」，再多就只报告不动


def _conflict_name(target: Path, mtime: float, taken: set[Path] | None = None) -> Path | None:
    """给内容冲突的那一份起个不会撞车的名字：`标题（另一版本 2026-11-02）.pdf`。

    跟 Syncthing 的 `<名字>.sync-conflict-<日期>-<设备>.<扩展名>` 一个思路：两份
    都留着，用文件名说清哪份是后来者，用户自己看页数决定留哪个。上限 10 份。
    """
    stem = target.stem
    suffix = target.suffix or ".pdf"
    try:
        day = time.strftime("%Y-%m-%d", time.localtime(mtime or time.time()))
    except (OSError, ValueError, OverflowError):
        day = "另一版"
    base = f"{stem}（另一版本 {day}）"

    def free(candidate: Path) -> bool:
        return not candidate.exists() and not (taken and candidate in taken)

    candidate = target.with_name(base + suffix)
    if free(candidate):
        return candidate
    for index in range(2, _CONFLICT_CAP + 1):
        candidate = target.with_name(f"{base}-{index}{suffix}")
        if free(candidate):
            return candidate
    return None


def conflict_note(found: FoundFile, target: Path, other: FoundFile | None = None) -> str:
    """「这份 94 页，另一份 83 页」——判断留哪份要看的就是这个。"""
    mine = page_count(found.path)
    theirs = page_count(other.path) if other is not None else None
    if theirs is None:
        try:
            theirs = page_count(Path(target))
        except OSError:
            theirs = None
    bits = []
    if mine:
        bits.append(f"这份 {mine} 页")
    if theirs:
        label = "另一份" if other is not None else "目标位置"
        bits.append(f"{label} {theirs} 页")
    return "，".join(bits)


def _why(item: FoundFile, base: str, sub: str, index: int, course) -> str:
    if index:
        return "同一讲的第 %d 套幻灯片" % (index + 1)
    if _top_folder(item) and _top_folder(item) != base:
        return "跟着已有的课程目录"
    if item.folder and normalize(item.folder) != normalize(base):
        return "归到课程目录"
    return "改成统一文件名"


def _same_file(left: FoundFile, right) -> bool:
    """两个文件是不是同一份内容。right 可以是 FoundFile，也可以是 Path。"""
    right_path = right.path if isinstance(right, FoundFile) else Path(right)
    try:
        right_size = right.size if isinstance(right, FoundFile) else right_path.stat().st_size
    except OSError:
        return False
    if left.size != right_size:
        return False
    return (sha256(left.path) or "x") == (sha256(right_path) or "y")


def _as_found(root: Path, path: Path) -> FoundFile:
    try:
        stat = path.stat()
        relative = path.relative_to(root).as_posix()
    except (OSError, ValueError):
        stat = None
        relative = path.name
    return FoundFile(
        path=path,
        relative=relative,
        name=path.name,
        key=normalize(path.name),
        size=stat.st_size if stat else 0,
        mtime=stat.st_mtime if stat else 0.0,
    )


def apply_tidy(plan: TidyPlan) -> tuple[list[Move], list[tuple[FoundFile, str]]]:
    """执行整理：只做不覆盖的移动。返回（成功，失败及原因）。

    有几条移动是「先腾位再落位」的（`标题-1.pdf` 要改叫 `标题.pdf`，紧接着
    `标题-2.pdf` 又要用 `标题-1.pdf`），一条条按顺序搬会撞名。所以反复扫几轮，
    每轮只做目标已经空出来的那几条，直到没有进展为止。

    跨分区（保存目录在 U 盘或另一个挂载点）时 `os.replace` 会 EXDEV，退回
    `shutil.move`——先复制再删源，中途失败也不会把原文件弄丢。
    """
    import errno
    import shutil

    done: list[Move] = []
    failed: list[tuple[FoundFile, str]] = []
    pending = list(plan.moves)
    while pending:
        stuck: list[Move] = []
        for move in pending:
            if move.target.exists():
                stuck.append(move)
                continue
            try:
                move.target.parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                failed.append((move.source, "建不了目标目录"))
                continue
            try:
                try:
                    os.replace(move.source.path, move.target)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:
                        raise
                    shutil.move(str(move.source.path), str(move.target))
            except OSError:
                failed.append((move.source, "移动失败（可能没有权限）"))
                continue
            done.append(move)
        if len(stuck) == len(pending):      # 这一轮一条都没搬动，剩下的确实搬不了
            for move in stuck:
                if move.source.path.exists():
                    failed.append((move.source, "目标已存在"))
            break
        pending = stuck
    return done, failed


def summarize(plan: TidyPlan) -> str:
    parts = [f"可整理 {len(plan.moves)}"]
    if plan.kept:
        parts.append(f"已在位 {len(plan.kept)}")
    if plan.conflicts:
        parts.append(f"目标已存在且内容不同 {len(plan.conflicts)}")
    if plan.duplicates:
        parts.append(f"内容重复 {len(plan.duplicates)}")
    if plan.unmatched:
        parts.append(f"没认出来 {len(plan.unmatched)}")
    if plan.elsewhere:
        parts.append(f"另有 {plan.elsewhere} 个不在课件目录里（笔记、习题等，没动）")
    return "，".join(parts)


def _safe(name: str, fallback: str) -> str:
    from yuketang_courseware.names import safe_component

    return safe_component(name, fallback or "未命名")
